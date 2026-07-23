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
  agentic-gate.py switch <env> <session_id> [--allow-default] [--preview [PATH]]
                                                # manually set the active environment;
                                                # session_id is required — pass
                                                # $CLAUDE_CODE_SESSION_ID, or refuses
                                                # (--allow-default overrides); --preview
                                                # also writes an HTML status page (env
                                                # switched from/to, what's now loaded,
                                                # where each piece lives on disk)
  agentic-gate.py classify <env|shared> [--skill P] [--agent P] [--command P]
                                        [--mcp P] [--path P] [--create "description"]
  agentic-gate.py audit [--roots DIR] [--check-updates]
                                                # find installed resources the manifest
                                                # misses; --check-updates also resolves
                                                # each classified resource's installed
                                                # version and checks GitHub for newer
                                                # releases, writing a full report to
                                                # inventory.json
  agentic-gate.py selftest                     # run embedded fixture tests

MIT License — Darrin Southern, CadenceUX, 2026.
"""

import fnmatch
import json
import os
import re
import shlex
import shutil
import sys
import time
from html import escape
from pathlib import Path

VERSION = "0.2.7"


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


def _armed_via_plugin(path: str = None) -> bool:
    """Heuristic: is this copy of the engine running from an installed
    plugin's cache location? (`claude plugin list` is the authoritative
    source but shelling out to it would make status depend on the `claude`
    CLI being on PATH — this path-based check has no such dependency.)
    `path` defaults to this file's own real location; selftest passes a
    synthetic path so the result doesn't depend on where the suite itself
    happens to be invoked from — a real bug found by running the actual
    installed copy, which reasonably (and correctly) said "plugin" while
    the hardcoded-False test still expected the source-folder answer."""
    try:
        target = path if path is not None else str(Path(__file__).resolve())
        return "/.claude/plugins/cache/" in target
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
    non-plugin skill packs may need manual classification. --check-updates
    additionally resolves every *classified* resource's installed version
    and checks GitHub for newer releases, writing a full report to
    inventory.json — deliberately a separate, on-demand flag rather than
    something switch does automatically, since it makes network calls and
    switch must stay instant and offline."""
    roots = []
    if "--roots" in argv:
        roots = [Path(argv[argv.index("--roots") + 1])]
    else:
        roots = [Path.home() / ".claude" / "plugins" / "cache"]
    check_updates = "--check-updates" in argv

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

    result = {"classified": classified, "unassigned": unassigned}
    if check_updates:
        inventory = _build_inventory(manifest, check_updates=True)
        out_path = conf_dir() / "inventory.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
        result["inventory"] = inventory
        if not quiet:
            print(f"\nversion report → {out_path}")
            for env_name, env_data in inventory["environments"].items():
                print(f"  {env_name}:")
                for r in env_data["resources"]:
                    if r["update_channel"] == "n/a":
                        continue
                    installed = r["installed_version"] or "?"
                    latest = r["latest_version"] or "?"
                    print(f"    {r['field']:8} {r['pattern']:28} "
                          f"installed={installed:10} latest={latest:10} "
                          f"{r['status']}")
    return result


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


def _which(cmd: str):
    return shutil.which(cmd)


def _resolve_pattern_location(pattern: str, field: str):
    """Best-effort answer to 'where does the thing behind this declared
    pattern actually live on disk?' — the three kinds of skill source seen
    in practice: hosted account skills (Customize → Skills, no local file),
    plugin-cache skills/agents installed from a marketplace, and a
    project's own .claude/skills/. Never authoritative — a glob is a glob —
    but enough to point a developer at the right folder after a switch."""
    if field == "mcp":
        return {"kind": "mcp", "detail": "MCP server — connectivity comes "
                "from Claude Code's MCP config, not a local skill file."}
    if field == "paths":
        return {"kind": "path", "detail": _norm(pattern)}
    if field == "commands":
        found = _which(pattern) if "*" not in pattern and "?" not in pattern else None
        return ({"kind": "command", "detail": found} if found else
                {"kind": "command", "detail": "not found on PATH right now "
                 "(only resolvable for a literal command name, not a glob)"
                 if ("*" in pattern or "?" in pattern) else "not found on PATH"})

    # skills / agents: patterns are almost always "Prefix:*" style.
    prefix = pattern.split(":", 1)[0] if ":" in pattern else pattern
    if prefix == "anthropic-skills":
        return {"kind": "hosted", "detail": "Claude.ai account skill "
                "(installed via Customize → Skills) — no plugin-cache copy; "
                "the desktop app syncs a per-session working copy under "
                "~/Library/Application Support/Claude/local-agent-mode-"
                "sessions/skills-plugin/*/*/skills/ when it's enabled."}

    cache_root = Path.home() / ".claude" / "plugins" / "cache"
    matches = sorted(cache_root.glob(f"*/{prefix}/*/{field}"))
    if matches:
        latest = matches[-1]
        marketplace = latest.parent.parent.parent.name
        return {"kind": "local-plugin",
                "detail": f"{marketplace} marketplace → {latest}"}

    project_skills = Path.cwd() / ".claude" / field
    if project_skills.is_dir() and any(project_skills.iterdir()):
        return {"kind": "project-local", "detail": str(project_skills)}

    return {"kind": "unresolved",
            "detail": f"no installed match found for '{pattern}' under "
                      f"plugins/cache or {project_skills}"}


def _plugin_root_from(path: Path):
    """Walk up from any file/dir under ~/.claude/plugins/cache to the
    <version> directory — the exact 'installPath' installed_plugins.json
    records — e.g. .../cache/fmaiac/FileMaker/0.8.16/bin/fm-build or
    .../cache/fmaiac/FileMaker/0.8.16/skills both resolve to
    .../cache/fmaiac/FileMaker/0.8.16. Returns None if path isn't under
    the plugin cache at all."""
    cache_root = Path.home() / ".claude" / "plugins" / "cache"
    try:
        rel = path.resolve().relative_to(cache_root.resolve())
    except (OSError, ValueError):
        return None
    if len(rel.parts) < 3:
        return None
    return cache_root / rel.parts[0] / rel.parts[1] / rel.parts[2]


def _installed_plugins_index():
    """installPath (str) -> {plugin, marketplace, version, gitCommitSha}
    for every installed plugin, read from Claude Code's own
    installed_plugins.json. Best-effort: returns {} on any read/parse
    failure — this is enrichment data for `audit --check-updates`, never
    something we write, so a bad read degrades gracefully rather than
    blocking the command that asked for it."""
    p = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    index = {}
    for key, entries in (data.get("plugins") or {}).items():
        plugin, _, marketplace = key.partition("@")
        for entry in entries:
            install_path = entry.get("installPath")
            if install_path:
                index[install_path] = {
                    "plugin": plugin, "marketplace": marketplace,
                    "version": entry.get("version"),
                    "gitCommitSha": entry.get("gitCommitSha")}
    return index


def _known_marketplaces():
    """marketplace name -> its 'source' dict ({"source": "github", "repo":
    "OWNER/REPO"} or {"source": "directory", "path": ...} etc), read from
    Claude Code's known_marketplaces.json. Same best-effort contract as
    _installed_plugins_index — a vendor's plugin distributed via a local
    directory simply has no upstream repo to check, and that is an
    expected, reportable state, not an error."""
    p = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {name: (info.get("source") or {}) for name, info in data.items()}


_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


def _version_status(installed: str, latest: str) -> str:
    """Best-effort comparison of an installed version against a latest
    release/tag string. Extracts the first dotted-number run from each
    (handles a bare '0.2.6' and a prefixed tag like 'agentic-gate--v0.2.6'
    alike) and compares as tuples of ints. 'unknown' when either side has
    nothing version-shaped in it — not every tag is semver."""
    def extract(v):
        m = _VERSION_RE.search(v or "")
        return tuple(int(p) for p in m.group(1).split(".")) if m else None
    a, b = extract(installed), extract(latest)
    if a is None or b is None:
        return "unknown"
    return "up-to-date" if a >= b else "outdated"


def _latest_github_release(owner_repo: str, cache: dict):
    """Latest release (or, lacking any releases, latest tag) for a
    GitHub-sourced marketplace repo, via the public REST API. Network
    call — only ever invoked from `audit --check-updates`, never from
    switch or any other hot path. Caches per owner/repo within a single
    run (a marketplace's plugins usually share one repo — no reason to
    hit the API twice for the same lookup, and unauthenticated GitHub REST
    is rate-limited to 60 req/hr). Degrades to None on any failure
    (offline, rate-limited, repo not found, no releases and no tags)
    rather than raising — one bad lookup must not fail the whole report."""
    if owner_repo in cache:
        return cache[owner_repo]
    import urllib.error
    import urllib.request
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "agentic-gate"}
    result = None
    for path, is_list in (("releases/latest", False), ("tags", True)):
        url = f"https://api.github.com/repos/{owner_repo}/{path}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError, OSError, ValueError):
            continue
        if not is_list and isinstance(data, dict) and data.get("tag_name"):
            result = {"tag": data["tag_name"], "url": data.get("html_url")}
            break
        if is_list and isinstance(data, list) and data:
            name = data[0].get("name")
            result = {"tag": name,
                      "url": f"https://github.com/{owner_repo}/releases/tag/{name}"}
            break
    cache[owner_repo] = result
    return result


def _enrich_with_version(loc: dict, check_updates: bool, gh_cache: dict,
                          installed_index: dict, marketplaces: dict) -> dict:
    """Adds installed_version / marketplace / update_channel / github_repo
    / latest_version / status to a location dict already produced by
    _resolve_pattern_location — the same resolver `switch --preview` uses,
    so this never drifts from what the preview page reports. Only the
    'local-plugin' kind (skills/agents) and a PATH-resolved 'command' that
    happens to live under the plugin cache have an actual installed
    artifact to version; everything else (hosted, mcp, path, project-local,
    unresolved) gets update_channel: 'n/a' since there is nothing a
    marketplace installed for us to check."""
    out = dict(loc, installed_version=None, marketplace=None,
               update_channel="n/a", github_repo=None,
               latest_version=None, status="n/a")

    plugin_root = None
    if loc["kind"] == "local-plugin":
        detail_path = loc["detail"].split("→", 1)[-1].strip()
        plugin_root = _plugin_root_from(Path(detail_path))
    elif loc["kind"] == "command" and loc["detail"] and Path(loc["detail"]).exists():
        plugin_root = _plugin_root_from(Path(loc["detail"]))
    if plugin_root is None:
        return out

    info = installed_index.get(str(plugin_root))
    if not info:
        return out
    out["installed_version"] = info["version"]
    out["marketplace"] = info["marketplace"]

    source = marketplaces.get(info["marketplace"], {})
    if source.get("source") == "github" and source.get("repo"):
        out["update_channel"] = "github"
        out["github_repo"] = source["repo"]
        if check_updates:
            release = _latest_github_release(source["repo"], gh_cache)
            if release:
                out["latest_version"] = release["tag"]
                out["status"] = _version_status(info["version"], release["tag"])
            else:
                out["status"] = "unknown"
        else:
            out["status"] = "unknown"
    elif source.get("source"):
        out["update_channel"] = source["source"]
        out["status"] = "no-update-channel"
    return out


def _build_inventory(manifest, check_updates):
    """Resolve every declared pattern (across every environment + shared)
    to installed-version / update-freshness info, for `audit
    --check-updates`. Reuses _resolve_pattern_location and ENV_FIELDS —
    the same resolution `switch --preview` already does — plus the new
    version-enrichment helpers above. Network calls (GitHub release
    lookups) only happen when check_updates is True, and are cached
    per-repo within this single call via gh_cache."""
    envs = manifest.get("environments") or {}
    shared = manifest.get("shared") or {}
    installed_index = _installed_plugins_index()
    marketplaces = _known_marketplaces()
    gh_cache = {}

    def build_bucket(bucket):
        resources = []
        for field in ENV_FIELDS:
            for pattern in (bucket.get(field) or []):
                loc = _resolve_pattern_location(pattern, field)
                enriched = _enrich_with_version(
                    loc, check_updates, gh_cache, installed_index, marketplaces)
                resources.append({"field": field, "pattern": pattern, **enriched})
        return resources

    report = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "check_updates": check_updates,
              "environments": {}, "shared": {}}
    for name, bucket in envs.items():
        report["environments"][name] = {
            "description": bucket.get("description", ""),
            "resources": build_bucket(bucket)}
    report["shared"] = {"description": shared.get("description", ""),
                         "resources": build_bucket(shared)}
    return report


def _hook_wiring():
    """What's actually enforcing right now — plugin, standalone, both, or
    none — and which hook events. Same signals `status` reports, reused
    here so the preview page doesn't drift from what `status` would say."""
    via_plugin = _armed_via_plugin()
    try:
        settings = _read_settings()
    except SystemExit:
        settings = {}
    standalone_events = [e for e in HOOK_SPEC
                         if _has_our_hook((settings.get("hooks") or {}).get(e))]
    lines = []
    if via_plugin:
        lines.append(("plugin", "SessionStart, PreToolUse, PostToolUse "
                                 "(bundled hooks.json)"))
    if standalone_events:
        lines.append(("standalone", f"{', '.join(standalone_events)} "
                                     f"(registered in {settings_file()})"))
    if not lines:
        lines.append(("none", "no hooks currently registered — the "
                               "guardrail is disarmed"))
    return lines


_ENV_PALETTE = ["#0e7490", "#a16207", "#7c3aed", "#be123c",
                "#15803d", "#1d4ed8", "#c2410c", "#0f766e"]


def _env_color(name):
    if not name:
        return "#71717a"
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return _ENV_PALETTE[h % len(_ENV_PALETTE)]


def _env_dot(name):
    label = escape(name) if name else "(none)"
    return (f"<span class='envtag'><span class='dot' "
            f"style='--dot:{_env_color(name)}'></span>{label}</span>")


def _build_switch_preview_html(manifest, prev_state, new_env_name):
    """Deterministic HTML fragment (no <html>/<head>/<body> — the Artifact
    tool supplies that) for a status page after a manual environment
    switch: what changed, what's now loaded for the new environment
    (including the shared tier every environment gets silently), where
    each declared item actually lives on disk, and which hooks are the
    ones enforcing it. Generated by this script from the real manifest —
    not something the calling model should hand-author or paraphrase."""
    envs = manifest.get("environments") or {}
    shared = manifest.get("shared") or {}
    new_env = envs.get(new_env_name, {})
    prev_env_name = prev_state.get("active")

    def section_rows(bucket, fields=ENV_FIELDS):
        rows = ""
        for field in fields:
            for pattern in (bucket.get(field) or []):
                loc = _resolve_pattern_location(pattern, field)
                kind_label = {"hosted": "hosted (account)",
                              "local-plugin": "local plugin",
                              "project-local": "project-local",
                              "unresolved": "unresolved",
                              "mcp": "MCP", "command": "command",
                              "path": "path"}.get(loc["kind"], loc["kind"])
                rows += (f"<tr><td class='mono'>{escape(field)}</td>"
                         f"<td class='mono'>{escape(pattern)}</td>"
                         f"<td><span class='kind kind-{loc['kind']}'>"
                         f"{escape(kind_label)}</span></td>"
                         f"<td class='mono muted'>{escape(str(loc['detail']))}</td></tr>")
        return rows or ("<tr><td colspan='4' class='muted'>nothing declared"
                        "</td></tr>")

    hook_rows = "".join(
        f"<tr><td><span class='kind kind-{k}'>{escape(k)}</span></td>"
        f"<td>{escape(v)}</td></tr>" for k, v in _hook_wiring())

    def _field_counts(bucket):
        counts = [f"{len(bucket.get(f) or [])} {f}" for f in ENV_FIELDS
                  if bucket.get(f)]
        return ", ".join(counts) or "nothing declared"

    new_env_row = (f"<tr><td>{_env_dot(new_env_name)}</td>"
                   f"<td class='muted'>{escape(new_env.get('description', '(no description)'))}</td>"
                   f"<td class='mono muted'>{escape(_field_counts(new_env))}</td></tr>")

    other_env_rows = "".join(
        f"<tr><td>{_env_dot(name)}</td>"
        f"<td class='muted'>{escape(env.get('description', '(no description)'))}</td>"
        f"<td class='mono muted'>{escape(_field_counts(env))}</td></tr>"
        for name, env in sorted(envs.items()) if name != new_env_name
    ) or "<tr><td colspan='3' class='muted'>no other environments declared</td></tr>"

    prev_env = envs.get(prev_env_name) if prev_env_name else None
    if prev_env_name and prev_env is not None:
        prev_env_row = (f"<tr><td>{_env_dot(prev_env_name)}</td>"
                        f"<td class='muted'>{escape(prev_env.get('description', '(no description)'))}</td>"
                        f"<td class='mono muted'>{escape(_field_counts(prev_env))}</td></tr>")
    else:
        prev_env_row = ("<tr><td colspan='3' class='muted'>no prior active "
                        "environment this session</td></tr>")

    return f"""<style>
.ag-wrap {{
  --bg: #fbfbfa; --surface: #f1f3f4; --border: #dde1e3;
  --text: #1c2024; --muted: #5b6470; --accent: #0e7490; --accent-tint: #e3f2f5;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  max-width: 900px; margin: 0 auto; color: var(--text);
}}
@media (prefers-color-scheme: dark) {{
  .ag-wrap {{ --bg: #161819; --surface: #1f2225; --border: #33383d;
    --text: #e8eaec; --muted: #9aa4ad; --accent: #22b8d8; --accent-tint: #123138; }}
}}
:root[data-theme="dark"] .ag-wrap {{ --bg: #161819; --surface: #1f2225; --border: #33383d;
  --text: #e8eaec; --muted: #9aa4ad; --accent: #22b8d8; --accent-tint: #123138; }}
:root[data-theme="light"] .ag-wrap {{ --bg: #fbfbfa; --surface: #f1f3f4; --border: #dde1e3;
  --text: #1c2024; --muted: #5b6470; --accent: #0e7490; --accent-tint: #e3f2f5; }}
.ag-wrap .mono {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .85em; }}
.ag-wrap h1 {{ font-size: 1.3rem; margin: 0 0 .2rem; text-wrap: balance; }}
.ag-wrap h2 {{ font-size: .78rem; margin: 1.6rem 0 .5rem; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); font-weight: 600; }}
.ag-wrap .muted {{ color: var(--muted); }}
.ag-wrap .envtag {{ display: inline-flex; align-items: center; gap: .4rem; font-weight: 600; }}
.ag-wrap .dot {{ display: inline-block; width: .55rem; height: .55rem; border-radius: 50%;
  background: var(--dot); flex: none; }}
.ag-wrap .banner {{ display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
  padding: .8rem 1.1rem; border-radius: .6rem; background: var(--surface);
  border: 1px solid var(--border); margin-bottom: 1.1rem; }}
.ag-wrap .arrow {{ color: var(--muted); }}
.ag-wrap table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
.ag-wrap th, .ag-wrap td {{ text-align: left; padding: .35rem .6rem;
  border-bottom: 1px solid var(--border); }}
.ag-wrap th {{ color: var(--muted); font-weight: 600; font-size: .72rem;
  text-transform: uppercase; letter-spacing: .04em; }}
.ag-wrap .kind {{ display: inline-block; padding: .1rem .5rem; border-radius: 999px;
  font-size: .75rem; font-weight: 600; background: var(--accent-tint); color: var(--accent); }}
.ag-wrap .table-scroll {{ overflow-x: auto; }}
.ag-wrap .desc {{ margin: .2rem 0 .6rem; }}
</style>
<div class="ag-wrap">
  <h1>Agentic-Gate — environment switch</h1>
  <div class="banner">
    {_env_dot(prev_env_name)}
    <span class="arrow">→</span>
    {_env_dot(new_env_name)}
    <span class="muted">· switched {escape(time.strftime('%Y-%m-%d %H:%M:%S'))}</span>
  </div>

  <h2>Hooks enforcing this</h2>
  <div class="table-scroll"><table>
    <tr><th>Armed via</th><th>Events</th></tr>
    {hook_rows}
  </table></div>

  <h2>Now active: {escape(new_env_name)}</h2>
  <div class="table-scroll"><table>
    <tr><th>Environment</th><th>Description</th><th>Declares</th></tr>
    {new_env_row}
  </table></div>
  <div class="table-scroll"><table>
    <tr><th>Field</th><th>Pattern</th><th>Location</th><th>Detail</th></tr>
    {section_rows(new_env)}
  </table></div>

  <h2>Shared tier (available regardless of active environment)</h2>
  <div class="desc muted">{escape(shared.get('description', '(no description)'))}</div>
  <div class="table-scroll"><table>
    <tr><th>Field</th><th>Pattern</th><th>Location</th><th>Detail</th></tr>
    {section_rows(shared, fields=("commands", "mcp", "paths"))}
  </table></div>

  <h2>Switched out of</h2>
  <div class="table-scroll"><table>
    <tr><th>Environment</th><th>Description</th><th>Declares</th></tr>
    {prev_env_row}
  </table></div>

  <h2>Other declared environments</h2>
  <div class="table-scroll"><table>
    <tr><th>Environment</th><th>Description</th><th>Declares</th></tr>
    {other_env_rows}
  </table></div>
</div>"""


def switch_cmd(argv, quiet=False):
    """Manually set the active environment for a session — the third way an
    active environment changes, alongside SessionStart's project-home lookup
    and PostToolUse's automatic switch-on-skill-run. --preview additionally
    writes an HTML status page (what changed, what's now loaded, where it
    lives) for the caller to publish — e.g. Claude publishing it as an
    Artifact — so a deliberate switch is visually confirmable, not just a
    JSON state write.

    A real session id is required: a bare `switch <env>` with nothing else
    used to fall back to a literal 'default' bucket, silently writing state
    for no actual conversation while looking like it worked. That's refused
    now unless --allow-default is passed explicitly — always prefer passing
    $CLAUDE_CODE_SESSION_ID, which every Claude Code session sets."""
    if not argv:
        print("agentic-gate: switch requires an environment name "
              "(usage: switch <env> <session_id> [--allow-default] "
              "[--preview [PATH]])", file=sys.stderr)
        return 2

    positional, preview_requested, preview_path, allow_default = [], False, None, False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--preview":
            preview_requested = True
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                preview_path = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok == "--allow-default":
            allow_default = True
            i += 1
            continue
        positional.append(tok)
        i += 1

    if not positional:
        print("agentic-gate: switch requires an environment name "
              "(usage: switch <env> <session_id> [--allow-default] "
              "[--preview [PATH]])", file=sys.stderr)
        return 2
    env_name = positional[0]
    session_id = positional[1] if len(positional) > 1 else "default"

    if session_id == "default" and not allow_default:
        print("agentic-gate: switch requires a real session id — pass "
              '"$CLAUDE_CODE_SESSION_ID" (set in every Claude Code session) '
              "so this actually targets your conversation. Refusing to "
              "write to the shared 'default' bucket, which no real session "
              "reads. Pass --allow-default to do this on purpose.",
              file=sys.stderr)
        return 2

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

    prev_state = dict(load_state(session_id))

    state = dict(prev_state)
    state["active"] = env_name
    state["set_by"] = "manual switch"
    state["ts"] = time.time()
    save_state(session_id, state)
    if not quiet:
        print(f"active environment for session '{session_id}' → {env_name}")

    if preview_requested:
        html = _build_switch_preview_html(manifest, prev_state, env_name)
        out_path = (Path(preview_path) if preview_path
                   else conf_dir() / "switch-preview.html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        if not quiet:
            print(f"preview   → {out_path}")

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

            # --- audit --check-updates: version status, pure logic ---
            check("_version_status: equal versions are up-to-date",
                  _version_status("0.2.6", "0.2.6") == "up-to-date")
            check("_version_status: installed ahead of latest is up-to-date",
                  _version_status("0.3.0", "0.2.6") == "up-to-date")
            check("_version_status: installed behind latest is outdated",
                  _version_status("0.2.4", "0.2.6") == "outdated")
            check("_version_status: strips a non-numeric tag prefix",
                  _version_status("0.2.4", "agentic-gate--v0.2.6") == "outdated")
            check("_version_status: unknown when nothing version-shaped",
                  _version_status("0.2.4", "not-a-version") == "unknown")

            cache_root = Path.home() / ".claude" / "plugins" / "cache"
            check("_plugin_root_from resolves a bin/ path to its version dir",
                  _plugin_root_from(cache_root / "mkt" / "PluginX" / "1.0" /
                                    "bin" / "tool")
                  == cache_root / "mkt" / "PluginX" / "1.0")
            check("_plugin_root_from resolves a skills/ path to its version dir",
                  _plugin_root_from(cache_root / "mkt" / "PluginX" / "1.0" /
                                    "skills")
                  == cache_root / "mkt" / "PluginX" / "1.0")
            check("_plugin_root_from returns None outside the plugin cache",
                  _plugin_root_from(Path(td) / "elsewhere" / "file") is None)

            # --- audit --check-updates: enrichment with synthetic data,
            # no real disk or network involved ---
            installed_index = {
                str(cache_root / "cadenceux" / "agentic-gate" / "0.2.4"): {
                    "plugin": "agentic-gate", "marketplace": "cadenceux",
                    "version": "0.2.4", "gitCommitSha": "abc123"},
                str(cache_root / "fmaiac" / "FileMaker" / "0.8.16"): {
                    "plugin": "FileMaker", "marketplace": "fmaiac",
                    "version": "0.8.16", "gitCommitSha": None},
            }
            marketplaces = {
                "cadenceux": {"source": "github", "repo": "CadenceUX/agentic-gate"},
                "fmaiac": {"source": "directory", "path": "/some/local/path"},
            }
            gh_cache = {"CadenceUX/agentic-gate":
                        {"tag": "v0.2.6", "url": "https://example.com"}}

            github_loc = {"kind": "local-plugin",
                          "detail": f"cadenceux marketplace → "
                                    f"{cache_root / 'cadenceux' / 'agentic-gate' / '0.2.4' / 'skills'}"}
            enriched = _enrich_with_version(github_loc, True, gh_cache,
                                            installed_index, marketplaces)
            check("_enrich_with_version resolves installed version for a "
                  "github-sourced local plugin",
                  enriched["installed_version"] == "0.2.4")
            check("_enrich_with_version resolves the update channel as github",
                  enriched["update_channel"] == "github")
            check("_enrich_with_version reports outdated when latest is newer",
                  enriched["status"] == "outdated"
                  and enriched["latest_version"] == "v0.2.6")

            directory_loc = {"kind": "local-plugin",
                             "detail": f"fmaiac marketplace → "
                                       f"{cache_root / 'fmaiac' / 'FileMaker' / '0.8.16' / 'skills'}"}
            enriched2 = _enrich_with_version(directory_loc, True, gh_cache,
                                             installed_index, marketplaces)
            check("_enrich_with_version reports no-update-channel for a "
                  "directory-sourced marketplace",
                  enriched2["update_channel"] == "directory"
                  and enriched2["status"] == "no-update-channel")

            hosted_loc = {"kind": "hosted", "detail": "some account skill"}
            enriched3 = _enrich_with_version(hosted_loc, True, gh_cache,
                                             installed_index, marketplaces)
            check("_enrich_with_version leaves non-plugin kinds as n/a",
                  enriched3["update_channel"] == "n/a"
                  and enriched3["installed_version"] is None)

            empty_home = Path(td) / "no-plugins-json-here"
            empty_home.mkdir()
            orig_home_for_missing = Path.home
            try:
                Path.home = staticmethod(lambda: empty_home)
                check("_installed_plugins_index degrades to {} when the "
                      "file is missing", _installed_plugins_index() == {})
                check("_known_marketplaces degrades to {} when the file "
                      "is missing", _known_marketplaces() == {})
            finally:
                Path.home = orig_home_for_missing

            # --- audit --check-updates: end-to-end with a fake plugin cache
            # and a temporarily sandboxed Path.home() ---
            fake_home = Path(td) / "fakehome"
            fake_cache = fake_home / ".claude" / "plugins" / "cache"
            plugin_dir = fake_cache / "mkt" / "PluginY" / "2.0"
            (plugin_dir / "skills" / "PluginY-skill").mkdir(parents=True)
            (plugin_dir / "bin").mkdir()
            (plugin_dir / "bin" / "py-tool").write_text("bin")
            plugins_meta = fake_home / ".claude" / "plugins"
            plugins_meta.mkdir(parents=True, exist_ok=True)
            (plugins_meta / "installed_plugins.json").write_text(json.dumps({
                "version": 2,
                "plugins": {"PluginY@mkt": [{
                    "scope": "user", "installPath": str(plugin_dir),
                    "version": "2.0"}]}}))
            (plugins_meta / "known_marketplaces.json").write_text(json.dumps({
                "mkt": {"source": {"source": "github", "repo": "acme/pluginy"}}}))

            update_manifest = {
                "version": 1,
                "environments": {"env-y": {
                    "description": "test env",
                    "skills": ["PluginY:*"]}},
                "shared": {},
            }
            orig_home = Path.home
            orig_release = _latest_github_release
            try:
                Path.home = staticmethod(lambda: fake_home)
                globals()["_latest_github_release"] = (
                    lambda owner_repo, cache: {"tag": "v2.0", "url": "x"}
                    if owner_repo == "acme/pluginy" else None)
                with open(manifest_path(), "w") as f:
                    json.dump(update_manifest, f)
                rc_result = audit(["--check-updates"], quiet=True)
            finally:
                Path.home = orig_home
                globals()["_latest_github_release"] = orig_release
                with open(manifest_path(), "w") as f:
                    json.dump(SELFTEST_MANIFEST, f)

            check("audit --check-updates returns an inventory report",
                  "inventory" in rc_result)
            inv_resources = (rc_result["inventory"]["environments"]
                             .get("env-y", {}).get("resources", []))
            check("audit --check-updates resolves the fake plugin's "
                  "installed version",
                  any(r.get("installed_version") == "2.0" for r in inv_resources),
                  f"(got {inv_resources})")
            check("audit --check-updates matches it up-to-date against the "
                  "stubbed latest release",
                  any(r.get("status") == "up-to-date" for r in inv_resources),
                  f"(got {inv_resources})")
            check("audit --check-updates writes inventory.json",
                  (conf_dir() / "inventory.json").exists())

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

            # --- switch: session id is required, 'default' is refused ---
            default_state_before = dict(load_state("default"))
            rc = switch_cmd(["pack-a"], quiet=True)
            check("switch with no session id is refused", rc == 2)
            check("switch with no session id leaves the 'default' bucket untouched",
                  dict(load_state("default")) == default_state_before)

            rc = switch_cmd(["pack-a", "default"], quiet=True)
            check("switch with explicit session id 'default' is refused", rc == 2)
            check("switch to explicit 'default' leaves state untouched",
                  dict(load_state("default")) == default_state_before)

            rc = switch_cmd(["pack-a", "default", "--allow-default"], quiet=True)
            check("switch --allow-default succeeds", rc == 0)
            check("switch --allow-default actually writes the 'default' bucket",
                  load_state("default").get("active") == "pack-a")

            check("switch with a real session id is unaffected by the refusal logic",
                  switch_cmd(["vendor-x", "t3"], quiet=True) == 0)

            # --- switch --preview: writes an HTML status page ---
            rc = switch_cmd(["pack-a", "t3", "--preview"], quiet=True)
            check("switch --preview succeeds", rc == 0)
            preview_default = conf_dir() / "switch-preview.html"
            check("switch --preview writes to the default path when none given",
                  preview_default.exists())
            preview_html = preview_default.read_text(encoding="utf-8")
            check("switch --preview HTML shows the new active environment",
                  "pack-a" in preview_html)
            check("switch --preview HTML shows the environment switched out of",
                  "vendor-x" in preview_html)
            check("switch --preview HTML shows 'Now active' in the same "
                  "Environment/Description/Declares table format as "
                  "'Switched out of' and 'Other declared environments'",
                  "1 skills, 1 paths" in preview_html)
            check("switch --preview HTML has no forbidden top-level tags",
                  not any(tag in preview_html.lower() for tag in
                          ("<!doctype", "<html", "<head", "<body")))

            custom_preview = Path(td) / "custom-preview.html"
            rc = switch_cmd(["vendor-x", "t3", "--preview", str(custom_preview)],
                            quiet=True)
            check("switch --preview PATH succeeds", rc == 0)
            check("switch --preview PATH writes to the given path",
                  custom_preview.exists())

            check("_resolve_pattern_location flags anthropic-skills as hosted",
                  _resolve_pattern_location("anthropic-skills:*", "skills")
                  ["kind"] == "hosted")
            check("_resolve_pattern_location falls back to unresolved for an "
                  "unknown prefix",
                  _resolve_pattern_location("TotallyMadeUpVendor:*", "skills")
                  ["kind"] == "unresolved")

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
            # Synthetic paths, not this file's real location — the location-
            # dependent version of this test passed from the source folder
            # and failed from the real installed plugin cache, which is
            # exactly the false negative a location-independent test exists
            # to prevent.
            check("_armed_via_plugin detects a real plugin cache path",
                  _armed_via_plugin(
                      "/Users/x/.claude/plugins/cache/cadenceux/"
                      "agentic-gate/0.2.3/agentic-gate.py") is True)
            check("_armed_via_plugin is false for a non-plugin path",
                  _armed_via_plugin("/Users/x/Desktop/agentic-gate/"
                                    "agentic-gate.py") is False)
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
