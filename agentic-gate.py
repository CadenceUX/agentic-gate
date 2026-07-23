#!/usr/bin/env python3
"""
Agentic-Gate — no skill crosses environments unannounced.

A Claude Code hook that referees calls between "environments" — named groups
of skills, agents, commands, MCP servers, and resource paths (e.g. one
vendor's plugin vs. your own skill pack). Same-environment and shared-tier
calls pass silently. Cross-environment calls are warned about, gated behind
a native permission prompt, or denied, per your manifest policy. Resources
not declared in any environment follow the `unknown` policy.

Runs as three hooks (one script, branches on hook_event_name):
  SessionStart  — arms the guardrail, sets the project's home environment,
                  injects a one-line status report into context.
  PreToolUse    — classifies each Skill / Task(Agent) / Bash / Read / mcp__*
                  call against the manifest and decides allow / warn / ask / deny.
  PostToolUse   — after a Skill tool actually runs, the active environment
                  switches to that skill's environment (an approved crossing
                  IS a switch).

Config: ~/.claude/agentic-gate/environments.json  (see environments.example.json)
Override the config dir with $AGENTIC_GATE_HOME (used by selftest).
A "*" entry in `projects` is a default/fallback environment for sessions
outside every mapped project path — checked last, never shadows a real
project match.

Usage:
  echo '<hook-json>' | agentic-gate.py         # normal hook invocation
  agentic-gate.py install / uninstall [--purge]
  agentic-gate.py status [session_id]          # print active state + how it's armed
  agentic-gate.py environments [query]         # list; exact name/'shared' shows full detail;
                                                # anything else searches name/description/patterns
  agentic-gate.py switch <env> [session_id]    # manually set the active environment
  agentic-gate.py classify <env|shared> [--skill P] [--agent P] [--command P]
                                        [--mcp P] [--path P] [--create "description"]
  agentic-gate.py audit [--roots DIR]          # find installed resources the manifest misses
  agentic-gate.py selftest                     # run embedded fixture tests

MIT License — Darrin Southern, CadenceUX, 2026.
"""

import fnmatch
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

VERSION = "0.2.3"


# --------------------------------------------------------------------------
# Config / state plumbing
# --------------------------------------------------------------------------

def conf_dir() -> Path:
    return Path(os.environ.get("AGENTIC_GATE_HOME",
                               str(Path.home() / ".claude" / "agentic-gate")))


def manifest_path() -> Path:
    return conf_dir() / "environments.json"


def state_path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return conf_dir() / "state" / f"{safe}.json"


def crossings_log() -> Path:
    return conf_dir() / "crossings.log"


def load_manifest():
    try:
        with open(manifest_path()) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_manifest(manifest: dict) -> None:
    """Write the manifest back, backing up the previous version first —
    same discipline as _write_settings for settings.json."""
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".af-backup"))
    with open(p, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def load_state(session_id: str) -> dict:
    try:
        with open(state_path(session_id)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(session_id: str, state: dict) -> None:
    p = state_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f, indent=1)


def log_crossing(event: dict) -> None:
    try:
        p = crossings_log()
        p.parent.mkdir(parents=True, exist_ok=True)
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(p, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass  # logging must never break the hook


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.expanduser(path or "")


def _match_any(value: str, patterns) -> bool:
    return any(fnmatch.fnmatch(value, p) for p in (patterns or []))


def _match_paths(value: str, patterns) -> bool:
    v = _norm(value)
    return any(fnmatch.fnmatch(v, _norm(p)) for p in (patterns or []))


def _bash_tokens(command: str):
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return [t for t in tokens if t and not t.startswith("-")]


def matches_bucket(bucket: dict, tool: str, tool_input: dict):
    """Does this tool call touch this bucket (an environment or the shared tier)?
    Returns a short human reason string, or None."""
    if not bucket:
        return None
    if tool == "Skill":
        skill = tool_input.get("skill", "")
        if _match_any(skill, bucket.get("skills")):
            return f"skill '{skill}'"
    elif tool in ("Task", "Agent"):
        sub = tool_input.get("subagent_type", "")
        if sub and _match_any(sub, bucket.get("agents")):
            return f"agent '{sub}'"
    elif tool == "Bash":
        cmd = tool_input.get("command", "")
        tokens = _bash_tokens(cmd)
        basenames = {os.path.basename(t) for t in tokens}
        declared = set(bucket.get("commands") or [])
        hit = sorted(basenames & declared)
        if hit:
            return f"command '{hit[0]}'"
        for t in tokens:
            if _match_paths(t, bucket.get("paths")):
                return f"path '{t}' in command"
    elif tool in ("Read", "Write", "Edit", "NotebookEdit"):
        fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if fp and _match_paths(fp, bucket.get("paths")):
            return f"file '{fp}'"
    elif tool.startswith("mcp__"):
        if _match_any(tool, bucket.get("mcp")):
            return f"MCP tool '{tool}'"
    return None


def classify(manifest: dict, tool: str, tool_input: dict):
    """Return (env_name, reason) for the first environment this call touches,
    or (None, None)."""
    for name, env in (manifest.get("environments") or {}).items():
        reason = matches_bucket(env, tool, tool_input)
        if reason:
            return name, reason
    return None, None


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

def boundary_mode(manifest: dict, frm: str, to: str) -> str:
    policy = manifest.get("policy") or {}
    for key, mode in (policy.get("pairs") or {}).items():
        a, _, b = key.partition("|")
        if {a, b} == {frm, to}:
            return mode
    return policy.get("default", "warn")


GOVERNED_UNKNOWN = ("Skill", "Task", "Agent")  # + mcp__* handled inline


def evaluate(manifest: dict, state: dict, tool: str, tool_input: dict):
    """Core decision. Returns dict:
       {decision: allow|warn|ask|deny, reason, env, switch_to}
    'warn' means allow + visible systemMessage."""
    # Shared tier passes everywhere, silently.
    if matches_bucket(manifest.get("shared") or {}, tool, tool_input):
        return {"decision": "allow", "reason": "shared tier", "env": None,
                "switch_to": None}

    env, why = classify(manifest, tool, tool_input)
    active = state.get("active")

    if env is None:
        # Unclassified. Only governed classes trigger the unknown policy —
        # gating every ls/grep would make the guardrail unbearable.
        if tool in GOVERNED_UNKNOWN or tool.startswith("mcp__"):
            unknown = (manifest.get("policy") or {}).get("unknown", "warn")
            if unknown in ("warn", "ask", "deny"):
                label = tool_input.get("skill") or tool_input.get(
                    "subagent_type") or tool
                return {"decision": unknown,
                        "reason": f"'{label}' is not declared in any "
                                  f"environment in the Agentic-Gate manifest",
                        "env": None, "switch_to": None}
        return {"decision": "allow", "reason": "unclassified", "env": None,
                "switch_to": None}

    switch = env if tool == "Skill" else None

    if active is None or env == active:
        return {"decision": "allow", "reason": "same environment",
                "env": env, "switch_to": switch}

    mode = boundary_mode(manifest, active, env)
    reason = (f"Cross-environment call: active environment '{active}' is "
              f"reaching into '{env}' ({why}). Policy: {mode}.")
    if mode not in ("warn", "ask", "deny", "gate"):
        mode = "warn"
    if mode == "gate":
        mode = "ask"
    return {"decision": mode, "reason": reason, "env": env,
            "switch_to": switch}


# --------------------------------------------------------------------------
# Hook event handlers
# --------------------------------------------------------------------------

DEFAULT_PROJECT_KEY = "*"


def home_env_for_cwd(manifest: dict, cwd: str):
    """Specific project-path prefixes win first; a literal "*" entry in
    `projects` is a fallback default for sessions outside every mapped
    project, checked last so it never shadows a real match."""
    projects = manifest.get("projects") or {}
    for prefix, env in projects.items():
        if prefix == DEFAULT_PROJECT_KEY:
            continue
        if _norm(cwd).startswith(_norm(prefix)):
            return env
    return projects.get(DEFAULT_PROJECT_KEY)


def handle_session_start(data: dict) -> dict:
    manifest = load_manifest()
    if manifest is None:
        ctx = ("Agentic-Gate v" + VERSION + " is installed but found no "
               "manifest at " + str(manifest_path()) + ". It is DISARMED for "
               "this session. Copy environments.example.json there to arm it.")
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": ctx}}

    session_id = data.get("session_id", "default")
    home = home_env_for_cwd(manifest, data.get("cwd", ""))
    state = {"active": home, "set_by": "SessionStart(project home)"
             if home else None, "ts": time.time()}
    save_state(session_id, state)

    envs = manifest.get("environments") or {}
    policy = manifest.get("policy") or {}
    lines = [
        f"Agentic-Gate v{VERSION} armed. "
        f"Environments: {', '.join(envs)}. "
        f"Active environment: {home or '(none — set by first skill invoked)'}. "
        f"Policy: default={policy.get('default', 'warn')}, "
        f"unknown={policy.get('unknown', 'warn')}."
    ]
    pairs = policy.get("pairs") or {}
    if pairs:
        lines.append("Gated boundaries: " + ", ".join(
            f"{k}→{v}" for k, v in pairs.items()) + ".")
    lines.append("Rule: work within the active environment; shared-tier "
                 "delivery tools are always fine; crossing into another "
                 "environment triggers this guardrail (warn or permission "
                 "prompt) — prefer asking the developer first.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": " ".join(lines)}}


def handle_pre_tool_use(data: dict) -> dict:
    manifest = load_manifest()
    if manifest is None:
        return {}
    session_id = data.get("session_id", "default")
    state = load_state(session_id)
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}

    verdict = evaluate(manifest, state, tool, tool_input)
    decision, reason = verdict["decision"], verdict["reason"]

    if decision == "allow":
        return {}

    label = tool_input.get("skill") or tool_input.get("subagent_type") or tool
    if decision == "warn":
        log_crossing({"session": session_id, "tool": tool, "label": label,
                      "from": state.get("active"), "to": verdict["env"],
                      "mode": "warn"})
        return {"systemMessage": f"⚠ Agentic-Gate: {reason}"}

    # ask / deny → native permission machinery
    log_crossing({"session": session_id, "tool": tool, "label": label,
                  "from": state.get("active"), "to": verdict["env"],
                  "mode": decision})
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision if decision in ("ask", "deny") else "ask",
        "permissionDecisionReason": f"Agentic-Gate: {reason}"}}


def handle_post_tool_use(data: dict) -> dict:
    manifest = load_manifest()
    if manifest is None:
        return {}
    tool = data.get("tool_name", "")
    if tool != "Skill":
        return {}
    session_id = data.get("session_id", "default")
    tool_input = data.get("tool_input") or {}
    env, _ = classify(manifest, tool, tool_input)
    if env:
        state = load_state(session_id)
        if state.get("active") != env:
            state["active"] = env
            state["set_by"] = f"Skill:{tool_input.get('skill', '?')}"
            state["ts"] = time.time()
            save_state(session_id, state)
    return {}


HANDLERS = {
    "SessionStart": handle_session_start,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
}


# --------------------------------------------------------------------------
# Lifecycle verbs: install / uninstall / audit
# --------------------------------------------------------------------------

HOOK_TAG = "agentic-gate"

TEMPLATE_MANIFEST = {
    "version": 1,
    "environments": {
        "example-env": {
            "description": "Rename me. One entry per toolset you want fenced.",
            "skills": ["example-pack:*"], "agents": [], "commands": [],
            "mcp": [], "paths": []
        }
    },
    "shared": {"description": "Resources every environment may use silently.",
               "commands": []},
    "policy": {"default": "warn", "unknown": "warn", "pairs": {}},
    "projects": {}
}


def settings_file() -> Path:
    return Path(os.environ.get("AGENTIC_GATE_SETTINGS",
                               str(Path.home() / ".claude" / "settings.json")))


def _read_settings():
    """Returns settings dict, or raises SystemExit on corrupt JSON —
    we must never clobber a settings file we cannot parse."""
    p = settings_file()
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"agentic-gate: {p} is not valid JSON ({exc}); refusing to "
            f"modify it. Fix the file and re-run.")


def _write_settings(settings: dict) -> None:
    p = settings_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".af-backup"))
    with open(p, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def _has_our_hook(entries) -> bool:
    return any(HOOK_TAG in h.get("command", "")
               for e in (entries or []) for h in e.get("hooks", []))


def _armed_via_plugin() -> bool:
    """Heuristic: is this copy of the engine running from an installed
    plugin's cache location? (`claude plugin list` is the authoritative
    source but shelling out to it would make status depend on the `claude`
    CLI being on PATH — this path-based check has no such dependency.)"""
    try:
        return "/.claude/plugins/cache/" in str(Path(__file__).resolve())
    except OSError:
        return False


HOOK_SPEC = {"SessionStart": None, "PreToolUse": "*", "PostToolUse": "Skill"}


def install(argv, quiet=False):
    def say(msg):
        if not quiet:
            print(msg)

    home = conf_dir()
    home.mkdir(parents=True, exist_ok=True)

    src = Path(__file__).resolve()
    engine = home / "agentic-gate.py"
    already_here = engine.exists() and engine.resolve() == src
    if not already_here:
        shutil.copy2(src, engine)
        say(f"engine    → {engine}")

    man = manifest_path()
    if man.exists():
        say(f"manifest  → {man} (existing — left untouched)")
    else:
        example = src.parent / "environments.example.json"
        if example.exists():
            shutil.copy2(example, man)
        else:
            with open(man, "w") as f:
                json.dump(TEMPLATE_MANIFEST, f, indent=2)
        say(f"manifest  → {man} (seeded — EDIT THIS to declare your "
            f"environments)")

    settings = _read_settings()
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event, matcher in HOOK_SPEC.items():
        entries = hooks.setdefault(event, [])
        if _has_our_hook(entries):
            continue
        entry = {"hooks": [{"type": "command",
                            "command": f"python3 {engine}"}]}
        if matcher is not None:
            entry["matcher"] = matcher
        entries.append(entry)
        changed = True
    if changed:
        _write_settings(settings)
        say(f"hooks     → {settings_file()} (registered "
            f"{', '.join(HOOK_SPEC)}; backup written alongside)")
    else:
        say(f"hooks     → {settings_file()} (already registered — no change)")

    if load_manifest() is None:
        say("WARNING: manifest did not load back cleanly; guardrail will "
            "stay disarmed until it parses.")
    say("\nArmed from the next session. Verify:  agentic-gate.py status\n"
        "Selftest:                              agentic-gate.py selftest\n"
        "Undo everything:                       agentic-gate.py uninstall "
        "[--purge]")


def uninstall(argv, quiet=False):
    purge = "--purge" in argv

    def say(msg):
        if not quiet:
            print(msg)

    settings = _read_settings()
    hooks = settings.get("hooks") or {}
    changed = False
    for event in list(hooks.keys()):
        kept = [e for e in hooks[event]
                if not _has_our_hook([e])]
        if kept != hooks[event]:
            changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if changed:
        if not hooks:
            settings.pop("hooks", None)
        _write_settings(settings)
        say(f"removed   hook registrations from {settings_file()} "
            f"(backup written alongside)")
    else:
        say(f"no agentic-gate hooks found in {settings_file()} — nothing "
            f"to remove (already uninstalled, or armed via plugin instead: "
            f"disable the plugin to disarm)")

    state = conf_dir() / "state"
    if state.exists():
        shutil.rmtree(state, ignore_errors=True)
        say(f"removed   session state {state}")

    if purge:
        if conf_dir().exists():
            shutil.rmtree(conf_dir(), ignore_errors=True)
        say(f"purged    {conf_dir()} (manifest, crossings.log, engine copy)")
    else:
        kept = [p.name for p in
                (conf_dir().iterdir() if conf_dir().exists() else [])]
        if kept:
            say(f"kept      {conf_dir()} ({', '.join(sorted(kept))}) — your "
                f"classification work and audit trail. Remove with "
                f"'uninstall --purge'.")
    say("\nDisarmed from the next session.")


def audit(argv, quiet=False):
    """Enumerate installed plugin resources and report anything the manifest
    does not classify. Best-effort filesystem scan — MCP servers and
    non-plugin skill packs may need manual classification."""
    roots = []
    if "--roots" in argv:
        roots = [Path(argv[argv.index("--roots") + 1])]
    else:
        roots = [Path.home() / ".claude" / "plugins" / "cache"]

    manifest = load_manifest() or {}
    envs = manifest.get("environments") or {}
    shared = manifest.get("shared") or {}

    discovered = []  # (kind, identifier)
    for root in roots:
        if not root.is_dir():
            continue
        for skills_dir in root.glob("*/*/*/skills"):
            plugin = skills_dir.parent.parent.name
            for d in sorted(skills_dir.iterdir()):
                if d.is_dir():
                    discovered.append(("skill", f"{plugin}:{d.name}"))
            agents_dir = skills_dir.parent / "agents"
            if agents_dir.is_dir():
                for a in sorted(agents_dir.glob("*.md")):
                    discovered.append(("agent", f"{plugin}:{a.stem}"))
            bin_dir = skills_dir.parent / "bin"
            if bin_dir.is_dir():
                for b in sorted(bin_dir.iterdir()):
                    if b.is_file():
                        discovered.append(("command", b.name))

    field = {"skill": "skills", "agent": "agents", "command": "commands"}
    classified, unassigned = [], []
    for kind, ident in discovered:
        buckets = [n for n, env in envs.items()
                   if _match_any(ident, env.get(field[kind]))]
        if _match_any(ident, shared.get(field[kind])):
            buckets.append("(shared)")
        (classified if buckets else unassigned).append(
            (kind, ident, ", ".join(buckets)))

    if not quiet:
        print(f"scanned: {', '.join(str(r) for r in roots)}")
        print(f"classified: {len(classified)}   unassigned: {len(unassigned)}")
        for kind, ident, env in classified:
            print(f"  ok        {kind:8} {ident}  →  {env}")
        for kind, ident, _ in unassigned:
            print(f"  UNASSIGNED {kind:8} {ident}")
        if unassigned:
            print("\nAdd the unassigned items to an environment (or the "
                  "shared tier) in", manifest_path())
    return {"classified": classified, "unassigned": unassigned}


# --------------------------------------------------------------------------
# environments / switch / classify — discovery and manifest editing
# --------------------------------------------------------------------------

ENV_FIELDS = ("skills", "agents", "commands", "mcp", "paths")
SHARED_TARGET = "shared"
CLASSIFY_FLAGS = {"--skill": "skills", "--agent": "agents",
                   "--command": "commands", "--mcp": "mcp", "--path": "paths"}


def _env_summary_line(name: str, env: dict) -> str:
    counts = [f"{len(env.get(f) or [])} {f}" for f in ENV_FIELDS if env.get(f)]
    tail = f"  [{', '.join(counts)}]" if counts else ""
    return f"{name}: {env.get('description', '(no description)')}{tail}"


def _env_detail_lines(name: str, env: dict):
    """Full contents of one environment — every declared pattern, not just
    counts. Used when a query exactly matches an environment (or 'shared')
    by name, since at that point the user wants to *see* it, not search."""
    lines = [f"{name}: {env.get('description', '(no description)')}"]
    any_patterns = False
    for field in ENV_FIELDS:
        patterns = env.get(field) or []
        if patterns:
            any_patterns = True
            lines.append(f"  {field}:")
            for pattern in patterns:
                lines.append(f"    {pattern}")
    if not any_patterns:
        lines.append("  (no patterns declared)")
    return lines


def _env_search_hits(query: str, name: str, env: dict):
    """Two matching modes, both useful for different questions:
    - case-insensitive substring against name/description/pattern *text*
      ('environments vendorx' finds every pattern that mentions it), and
    - fnmatch of the query *as a concrete identifier* against each glob
      pattern ('environments VendorX:schema-builder' answers 'which
      environment would this exact call land in?', even though the
      manifest only ever declares 'VendorX:*', not that literal name).
    Returns a list of human-readable hit descriptions (empty if no match)."""
    q = query.lower()
    hits = []
    if q in name.lower():
        hits.append("name")
    if q in (env.get("description") or "").lower():
        hits.append("description")
    for field in ENV_FIELDS:
        for pattern in (env.get(field) or []):
            if q in pattern.lower():
                hits.append(f"{field} pattern '{pattern}' (text match)")
            elif fnmatch.fnmatch(query, pattern):
                hits.append(f"{field} pattern '{pattern}' "
                            f"(would classify this identifier)")
    return hits


def environments_cmd(argv, quiet=False):
    """List all environments, or search them by a query string that matches
    against name, description, or any declared skill/agent/command/mcp/path
    pattern — e.g. `environments VendorX:schema-builder` answers 'which
    environment would this exact agent land in?' (matched against the
    declared glob, even though only 'FileMaker:*' is ever written down),
    while `environments vendor-x` or `environments FileMaker` filters by
    name or by pattern text."""
    manifest = load_manifest()
    if manifest is None:
        if not quiet:
            print(f"agentic-gate: no manifest at {manifest_path()} — "
                  f"nothing to list.")
        return {"environments": {}}

    envs = manifest.get("environments") or {}
    shared = manifest.get("shared") or {}
    query = " ".join(argv).strip()

    if not query:
        if not quiet:
            for name, env in envs.items():
                print(_env_summary_line(name, env))
            if shared:
                print(_env_summary_line("(shared)", shared))
        return {"environments": sorted(envs)}

    # Exact name match (including the special "shared" target) shows full
    # contents — at that point the ask is "show me X", not "search for X".
    if query == SHARED_TARGET and shared:
        detail = _env_detail_lines("(shared)", shared)
        if not quiet:
            print("\n".join(detail))
        return {"detail": {"(shared)": detail}}
    if query in envs:
        detail = _env_detail_lines(query, envs[query])
        if not quiet:
            print("\n".join(detail))
        return {"detail": {query: detail}}

    matches = {name: hits for name, env in envs.items()
               if (hits := _env_search_hits(query, name, env))}
    if not quiet:
        if not matches:
            print(f"no environments match '{query}'")
        for name, hits in matches.items():
            print(f"{name}: matched on {', '.join(hits)}")
    return {"matches": matches}


def switch_cmd(argv, quiet=False):
    """Manually set the active environment for a session — the third way an
    active environment changes, alongside SessionStart's project-home lookup
    and PostToolUse's automatic switch-on-skill-run."""
    if not argv:
        print("agentic-gate: switch requires an environment name "
              "(usage: switch <env> [session_id])", file=sys.stderr)
        return 2
    env_name, session_id = argv[0], (argv[1] if len(argv) > 1 else "default")

    manifest = load_manifest()
    if manifest is None:
        print(f"agentic-gate: no manifest at {manifest_path()}.",
              file=sys.stderr)
        return 2
    envs = manifest.get("environments") or {}
    if env_name not in envs:
        print(f"agentic-gate: unknown environment '{env_name}'. "
              f"Known: {', '.join(sorted(envs)) or '(none defined)'}",
              file=sys.stderr)
        return 2

    state = load_state(session_id)
    state["active"] = env_name
    state["set_by"] = "manual switch"
    state["ts"] = time.time()
    save_state(session_id, state)
    if not quiet:
        print(f"active environment for session '{session_id}' → {env_name}")
    return 0



def classify_cmd(argv, quiet=False):
    """Add skill/agent/command/mcp/path patterns to an environment's
    declaration in the manifest — the write-side companion to `audit`
    (which finds what's unassigned) and `environments` (which finds what's
    already assigned where). Creates a new environment with --create when
    it doesn't exist yet; refuses to silently redefine an existing one.
    The special target name `shared` writes into the manifest's shared
    tier instead of a named environment — it always 'exists' implicitly,
    so --create/exists-checks don't apply to it."""
    if not argv:
        print("agentic-gate: classify requires an environment name, or "
              f"'{SHARED_TARGET}' for the shared tier (usage: classify "
              "<env|shared> [--skill P] [--agent P] [--command P] [--mcp P] "
              "[--path P] [--create \"description\"])", file=sys.stderr)
        return 2
    env_name, rest = argv[0], argv[1:]

    additions, create_desc = {}, None
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--create":
            if i + 1 >= len(rest):
                print("agentic-gate: --create requires a description",
                      file=sys.stderr)
                return 2
            create_desc = rest[i + 1]
            i += 2
            continue
        field = CLASSIFY_FLAGS.get(flag)
        if field is None:
            print(f"agentic-gate: unknown flag '{flag}' (expected one of "
                  f"{', '.join(CLASSIFY_FLAGS)}, --create)", file=sys.stderr)
            return 2
        if i + 1 >= len(rest):
            print(f"agentic-gate: {flag} requires a pattern", file=sys.stderr)
            return 2
        additions.setdefault(field, []).append(rest[i + 1])
        i += 2

    manifest = load_manifest()
    if manifest is None:
        print(f"agentic-gate: no manifest at {manifest_path()}. Run "
              f"'install' first.", file=sys.stderr)
        return 2

    created = False
    if env_name == SHARED_TARGET:
        if create_desc is not None:
            print("agentic-gate: --create doesn't apply to 'shared' — it "
                  "always exists implicitly.", file=sys.stderr)
            return 2
        env = manifest.setdefault("shared", {})
    else:
        envs = manifest.setdefault("environments", {})
        if env_name not in envs:
            if create_desc is None:
                print(f"agentic-gate: environment '{env_name}' does not "
                      f"exist. Known: "
                      f"{', '.join(sorted(envs)) or '(none defined)'}, or "
                      f"'{SHARED_TARGET}'. Add --create \"description\" to "
                      f"create a new one.", file=sys.stderr)
                return 2
            envs[env_name] = {"description": create_desc, "skills": [],
                               "agents": [], "commands": [], "mcp": [],
                               "paths": []}
            created = True
            if not quiet:
                print(f"created environment '{env_name}': {create_desc}")
        elif create_desc is not None:
            print(f"agentic-gate: environment '{env_name}' already exists "
                  f"— omit --create to add patterns to it, or edit its "
                  f"description directly in {manifest_path()}.",
                  file=sys.stderr)
            return 2
        env = envs[env_name]

    added, skipped = [], []
    for field, patterns in additions.items():
        bucket = env.setdefault(field, [])
        for pattern in patterns:
            if pattern in bucket:
                skipped.append(f"{field}:{pattern}")
            else:
                bucket.append(pattern)
                added.append(f"{field}:{pattern}")

    if added or created:
        save_manifest(manifest)
    if not quiet:
        if added:
            print(f"added to '{env_name}': {', '.join(added)}")
        if skipped:
            print(f"already present, skipped: {', '.join(skipped)}")
        if not added and not skipped and not created:
            print(f"'{env_name}' unchanged — no patterns given "
                  f"(--skill/--agent/--command/--mcp/--path).")
    return 0


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------

SELFTEST_MANIFEST = {
    "version": 1,
    "environments": {
        "pack-a": {"skills": ["pack-a:*"],
                      "paths": ["*/skills-plugin/*"]},
        "vendor-x": {"skills": ["VendorX:*"], "agents": ["VendorX:*"],
                   "commands": ["vx-build", "vx-patch-runner"],
                   "paths": ["~/.claude/plugins/cache/vendor-x/*"]},
        "vendor-y": {"mcp": ["mcp__vendor-y__*"]},
        "vendor-z": {"mcp": ["mcp__vendor-z__*"]},
    },
    "shared": {"commands": ["vx-clipboard-write", "vx-renderer"]},
    "policy": {"default": "warn", "unknown": "warn",
               "pairs": {"vendor-y|vendor-z": "gate"}},
    "projects": {"/tmp/sgtest-project": "vendor-x", "*": "pack-a"},
}

SELFTEST_CASES = [
    # (description, active_env, tool, tool_input, expected_decision)
    ("same-env skill call", "pack-a", "Skill",
     {"skill": "pack-a:some-skill"}, "allow"),
    ("cross-env agent dispatch (the incident that motivated this tool)", "pack-a", "Task",
     {"subagent_type": "VendorX:schema-builder"}, "warn"),
    ("cross-env reference read (a vendor reference-catalog reach-in)", "pack-a",
     "Read", {"file_path": os.path.expanduser(
         "~/.claude/plugins/cache/vendor-x/VendorX/1.0/tools/"
         "fm-reference/script-steps/SetVariable.md")}, "warn"),
    ("cross-env bash command", "pack-a", "Bash",
     {"command": "vx-build preview --spec - --target project/"}, "warn"),
    ("gated MCP pair (vendor-y → vendor-z)", "vendor-y",
     "mcp__vendor-z__example_tool", {}, "ask"),
    ("shared delivery tool from any env", "pack-a", "Bash",
     {"command": "vx-clipboard-write XMSC /tmp/x.xml"}, "allow"),
    ("unknown skill follows unknown policy", "vendor-x", "Skill",
     {"skill": "mystery-pack:do-things"}, "warn"),
    ("no active env yet — first skill allowed", None, "Skill",
     {"skill": "VendorX:some-tool"}, "allow"),
    ("unclassified everyday bash is untouched", "pack-a", "Bash",
     {"command": "ls -la /tmp"}, "allow"),
    ("cross-env skill invocation warns before switching", "vendor-x", "Skill",
     {"skill": "pack-a:full-feature-skill"}, "warn"),
]


def selftest() -> int:
    import tempfile
    results = []

    def check(desc, ok, extra=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {desc}"
              + ("" if ok else f"  {extra}"))

    prev_home = os.environ.get("AGENTIC_GATE_HOME")
    prev_settings = os.environ.get("AGENTIC_GATE_SETTINGS")
    try:
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td) / "conf"
            conf.mkdir(parents=True)
            os.environ["AGENTIC_GATE_HOME"] = str(conf)
            os.environ["AGENTIC_GATE_SETTINGS"] = str(
                Path(td) / "settings.json")
            with open(manifest_path(), "w") as f:
                json.dump(SELFTEST_MANIFEST, f)
            manifest = load_manifest()

            # --- decision engine ---
            for desc, active, tool, tool_input, expected in SELFTEST_CASES:
                verdict = evaluate(manifest, {"active": active}, tool,
                                   tool_input)
                check(f"{desc}: expected {expected}",
                      verdict["decision"] == expected,
                      f"(got {verdict['decision']}: {verdict['reason']})")

            out = handle_session_start(
                {"session_id": "t1", "cwd": "/tmp/sgtest-project/sub"})
            ctx = out.get("hookSpecificOutput", {}).get(
                "additionalContext", "")
            check("SessionStart resolves project home env",
                  "Active environment: vendor-x" in ctx)

            check("home_env_for_cwd falls back to '*' when no project "
                  "path matches",
                  home_env_for_cwd(SELFTEST_MANIFEST,
                                   "/nowhere/mapped") == "pack-a")
            check("home_env_for_cwd prefers a real project match over '*'",
                  home_env_for_cwd(SELFTEST_MANIFEST,
                                   "/tmp/sgtest-project/sub") == "vendor-x")
            no_default = {"projects": {"/tmp/sgtest-project": "vendor-x"}}
            check("home_env_for_cwd returns None with no '*' and no match",
                  home_env_for_cwd(no_default, "/nowhere/mapped") is None)

            save_state("t2", {"active": "vendor-x"})
            handle_post_tool_use(
                {"session_id": "t2", "tool_name": "Skill",
                 "tool_input": {"skill": "pack-a:full-feature-skill"}})
            check("PostToolUse switches active env after Skill runs",
                  load_state("t2").get("active") == "pack-a")

            # --- install / uninstall round-trip ---
            with open(settings_file(), "w") as f:
                json.dump({"model": "keep-me"}, f)
            install([], quiet=True)
            s = json.load(open(settings_file()))
            check("install registers all three hook events",
                  all(k in s.get("hooks", {}) for k in HOOK_SPEC))
            check("install preserves unrelated settings",
                  s.get("model") == "keep-me")
            check("install keeps an existing manifest untouched",
                  json.load(open(manifest_path())) == SELFTEST_MANIFEST)

            install([], quiet=True)
            s = json.load(open(settings_file()))
            dup = sum(1 for e in s["hooks"]["PreToolUse"]
                      for h in e.get("hooks", [])
                      if HOOK_TAG in h.get("command", ""))
            check("install is idempotent (no duplicate hooks)", dup == 1)

            uninstall([], quiet=True)
            s = json.load(open(settings_file()))
            check("uninstall removes hooks and keeps other settings",
                  "hooks" not in s and s.get("model") == "keep-me")
            check("uninstall keeps manifest and log by default",
                  manifest_path().exists())

            uninstall(["--purge"], quiet=True)
            check("uninstall --purge removes the config dir entirely",
                  not conf_dir().exists())

            # --- audit ---
            fake = Path(td) / "plugcache" / "mkt" / "PluginX" / "1.0"
            (fake / "skills" / "foo").mkdir(parents=True)
            (fake / "agents").mkdir()
            (fake / "agents" / "bar.md").write_text("agent")
            (fake / "bin").mkdir()
            (fake / "bin" / "px-tool").write_text("bin")
            conf.mkdir(parents=True, exist_ok=True)
            with open(manifest_path(), "w") as f:
                json.dump(SELFTEST_MANIFEST, f)
            report = audit(["--roots", str(Path(td) / "plugcache")],
                           quiet=True)
            check("audit reports unclassified plugin resources",
                  len(report["unassigned"]) == 3,
                  f"(got {report['unassigned']})")

            # --- environments: list and search ---
            listing = environments_cmd([], quiet=True)
            check("environments (no query) lists every manifest environment",
                  set(listing["environments"]) ==
                  set(SELFTEST_MANIFEST["environments"]),
                  f"(got {listing['environments']})")

            search = environments_cmd(["VendorX:schema-builder"], quiet=True)
            check("environments <query> reverse-looks-up a concrete "
                  "identifier against a declared glob pattern",
                  "vendor-x" in search["matches"],
                  f"(got {search['matches']})")

            search1b = environments_cmd(["VendorX"], quiet=True)
            check("environments <query> also matches by pattern text",
                  "vendor-x" in search1b["matches"],
                  f"(got {search1b['matches']})")

            search2 = environments_cmd(["vendor-y"], quiet=True)
            check("environments <exact-name> returns full detail, not "
                  "just a search hit",
                  "vendor-y" in search2.get("detail", {}),
                  f"(got {search2})")

            search2b = environments_cmd(["endor-y"], quiet=True)
            check("environments <partial-name> (no exact match) still "
                  "searches instead",
                  list(search2b.get("matches", {})) == ["vendor-y"],
                  f"(got {search2b})")

            search3 = environments_cmd(["no-such-thing-anywhere"], quiet=True)
            check("environments <query> with no hits returns no matches",
                  search3["matches"] == {})

            # --- switch ---
            save_state("t3", {"active": "pack-a"})
            rc = switch_cmd(["vendor-x", "t3"], quiet=True)
            check("switch succeeds for a known environment", rc == 0)
            check("switch actually updates the session's active environment",
                  load_state("t3").get("active") == "vendor-x")
            check("switch records how the change was made",
                  load_state("t3").get("set_by") == "manual switch")

            rc = switch_cmd(["not-a-real-env", "t3"], quiet=True)
            check("switch refuses an unknown environment", rc == 2)
            check("switch leaves state untouched after a refusal",
                  load_state("t3").get("active") == "vendor-x")

            # --- classify: add patterns to an existing environment ---
            rc = classify_cmd(["vendor-x", "--skill", "VendorX:new-tool"],
                              quiet=True)
            check("classify (existing env) succeeds", rc == 0)
            reloaded = load_manifest()
            check("classify persists the new pattern to the manifest file",
                  "VendorX:new-tool" in
                  reloaded["environments"]["vendor-x"]["skills"])
            check("classify's addition is enforced on the next evaluation",
                  evaluate(reloaded, {"active": "pack-a"}, "Skill",
                          {"skill": "VendorX:new-tool"})["decision"] == "warn")

            rc = classify_cmd(["vendor-x", "--skill", "VendorX:new-tool"],
                              quiet=True)
            check("classify is idempotent — re-adding the same pattern "
                  "doesn't duplicate it",
                  load_manifest()["environments"]["vendor-x"]["skills"]
                  .count("VendorX:new-tool") == 1)

            # --- classify: refuse an unknown environment without --create ---
            rc = classify_cmd(["brand-new-env", "--skill", "x:*"], quiet=True)
            check("classify refuses an unknown environment without --create",
                  rc == 2)
            check("classify makes no manifest change on refusal",
                  "brand-new-env" not in load_manifest()["environments"])

            # --- classify: create a new environment ---
            rc = classify_cmd(["brand-new-env", "--create", "Test env",
                               "--skill", "brand-new:*"], quiet=True)
            check("classify --create succeeds", rc == 0)
            created = load_manifest()["environments"].get("brand-new-env")
            check("classify --create adds the environment with its "
                  "description and pattern",
                  created is not None and
                  created["description"] == "Test env" and
                  "brand-new:*" in created["skills"])

            rc = classify_cmd(["brand-new-env", "--create", "Again"],
                              quiet=True)
            check("classify --create refuses to redefine an existing "
                  "environment", rc == 2)

            # --- environments <exact-name> shows the real declared patterns ---
            detail = environments_cmd(["vendor-x"], quiet=True)
            det_lines = detail["detail"]["vendor-x"]
            check("environments <exact-name> detail lists every declared "
                  "field, not just counts",
                  any("VendorX:*" in line for line in det_lines) and
                  any("vx-build" in line for line in det_lines),
                  f"(got {det_lines})")

            # --- environments shared shows the shared tier's real contents ---
            shared_detail = environments_cmd(["shared"], quiet=True)
            check("environments shared shows the shared tier's patterns",
                  any("vx-clipboard-write" in line
                      for line in shared_detail["detail"]["(shared)"]),
                  f"(got {shared_detail})")

            # --- classify shared: writes to the shared tier, not an environment ---
            rc = classify_cmd(["shared", "--command", "vx-new-shared-tool"],
                              quiet=True)
            check("classify shared succeeds", rc == 0)
            reloaded2 = load_manifest()
            check("classify shared persists to manifest['shared'], not "
                  "manifest['environments']",
                  "vx-new-shared-tool" in
                  reloaded2["shared"].get("commands", []) and
                  "shared" not in reloaded2["environments"])
            check("classify shared's addition passes silently from any "
                  "active environment on the next evaluation",
                  evaluate(reloaded2, {"active": "pack-a"}, "Bash",
                          {"command": "vx-new-shared-tool"}
                          )["decision"] == "allow")

            rc = classify_cmd(["shared", "--create", "nope"], quiet=True)
            check("classify shared rejects --create (it always exists "
                  "implicitly)", rc == 2)

            # --- status reports the real arming method (the live-test finding) ---
            check("_armed_via_plugin is false for a script run from a "
                  "temp selftest directory (not a plugin cache path)",
                  _armed_via_plugin() is False)
    finally:
        for var, val in (("AGENTIC_GATE_HOME", prev_home),
                         ("AGENTIC_GATE_SETTINGS", prev_settings)):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    argv = sys.argv[1:]
    if argv:
        verb, rest = argv[0], argv[1:]
        if verb == "selftest":
            return selftest()
        if verb == "install":
            install(rest)
            return 0
        if verb == "uninstall":
            uninstall(rest)
            return 0
        if verb == "audit":
            report = audit(rest)
            return 1 if report["unassigned"] else 0
        if verb == "environments":
            environments_cmd(rest)
            return 0
        if verb == "switch":
            return switch_cmd(rest)
        if verb == "classify":
            return classify_cmd(rest)
        if verb == "status":
            sid = rest[0] if rest else "default"
            settings = {}
            try:
                settings = _read_settings()
            except SystemExit:
                pass
            standalone = any(_has_our_hook((settings.get("hooks") or {}).get(e))
                             for e in HOOK_SPEC)
            via_plugin = _armed_via_plugin()
            armed_via = ("both" if standalone and via_plugin else
                        "standalone" if standalone else
                        "plugin" if via_plugin else "none")
            print(json.dumps({"version": VERSION,
                              "manifest": str(manifest_path()),
                              "manifest_found": load_manifest() is not None,
                              "armed_via": armed_via,
                              "hooks_registered_in_settings": standalone,
                              "state": load_state(sid)}, indent=2))
            return 0
        print(f"agentic-gate: unknown verb '{verb}' (expected: install, "
              f"uninstall, audit, environments, switch, classify, status, "
              f"selftest)", file=sys.stderr)
        return 2

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # never break the session on malformed input

    handler = HANDLERS.get(data.get("hook_event_name", ""))
    if handler is None:
        return 0
    try:
        out = handler(data)
    except Exception as exc:  # a guardrail must fail open, loudly in the log
        log_crossing({"error": repr(exc), "event": data.get("hook_event_name")})
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
