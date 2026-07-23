# Agentic-Gate

**No skill crosses environments unannounced.**

Agentic-Gate is a Claude Code plugin — its hook referees calls between
*environments* — named groups of skills, agents, commands, MCP servers, and
resource paths, such as one vendor's plugin versus your own skill pack.
Calls inside the active environment pass silently. Calls that cross into
another environment are **warned about**, **gated behind a permission
prompt**, or **denied** — per a policy you declare once, in a manifest.

## The problem

When two or more agentic toolsets share one session — two FileMaker skill
packs, two MCP servers with overlapping domains — the boundaries between
them exist only as prose in their instructions. Prose bends under context
pressure. A skill from pack A quietly pulls reference files from pack B.
An agent from vendor C gets dispatched to do pack D's work. Each toolset
is safe alone; the *composition* is not.

This failure class now has a name in the research literature — **Skill
Composition Risk**: skills "benign in isolation, harmful in composition,"
where "composed skill paths expose risks that are largely absent under
isolated evaluation" ([arXiv:2606.15242](https://arxiv.org/abs/2606.15242);
see also [arXiv:2606.00448](https://arxiv.org/abs/2606.00448)). Both papers
conclude that per-skill vetting is structurally insufficient — and neither
proposes a concrete defense. Skill *scanners* audit artifacts one at a time,
at install time; they cannot see a boundary being crossed at runtime.

Agentic-Gate is a reference implementation of the missing runtime layer:
deterministic boundary enforcement via Claude Code's hook system — the one
mechanism the model cannot rationalize its way around.

## How it works

One stdlib-only Python file, registered as three hooks:

| Hook | What it does |
|---|---|
| `SessionStart` | Arms the guardrail, resolves the project's **home environment**, injects a one-line status report into context (environments, active env, policy, gated pairs). |
| `PreToolUse` | Classifies every `Skill`, `Task` (agent dispatch), `Bash`, `Read`/`Write`/`Edit`, and `mcp__*` call against the manifest, then decides: **allow** (same env / shared tier), **warn** (allow + visible ⚠ message), **ask** (native permission prompt with the reason), or **deny**. |
| `PostToolUse` | When a `Skill` actually runs, the **active environment switches** to that skill's environment — an approved crossing *is* a switch. |

Every warn/ask/deny event is appended to `crossings.log` (JSONL), so you
can audit exactly when and where boundaries were tested.

The guardrail **fails open**: a malformed manifest, a broken state file, or
an internal error never blocks your session — it logs and steps aside.

## Install

### Quick start (new users)

From an interactive Claude Code session:

```
/plugin marketplace add CadenceUX/agentic-gate
/plugin install agentic-gate@cadenceux
```

Enable the plugin when prompted — enabling registers the hooks
automatically. Your next session will report:

> *Agentic-Gate v0.2.5 is installed but found no manifest … It is
> DISARMED for this session.*

That's expected: the guardrail never guesses your boundaries. Ask Claude to
**"set up my Agentic-Gate manifest"** — the bundled `agentic-gate-manage`
skill walks you through declaring your environments (one per toolset you
run), your shared utilities, and any gated pairs. From the following
session, the SessionStart line reads **armed**, and you're done.

Requirement: `python3` on PATH (standard on macOS/Linux; on Windows install
Python 3 and, if needed, change the hook commands to `python`).

### The two arming methods

Two ways to arm it — **pick one, never both** (double-arming fires every
hook twice; harmless but noisy):

**As a Claude Code plugin (recommended).** This repo is plugin-shaped
(`.claude-plugin/plugin.json` + `hooks/hooks.json`): enabling the plugin
registers the hooks automatically, and the bundled `agentic-gate-manage`
skill teaches Claude to configure, audit, and uninstall it for you.
Enabling = armed; disabling = disarmed. Then create your manifest:

```bash
mkdir -p ~/.claude/agentic-gate
cp environments.example.json ~/.claude/agentic-gate/environments.json
# edit it to declare YOUR environments
```

**Standalone, via the engine's own verb:**

```bash
python3 agentic-gate.py install
```

`install` copies the engine to `~/.claude/agentic-gate/`, seeds the
manifest if none exists (it never overwrites one), and registers the three
hooks in user-scope `~/.claude/settings.json` — idempotently, with a backup
of your settings written alongside. Verify with `selftest` (51/51 expected)
and `status` (its `armed_via` field reports `plugin`, `standalone`, `both`,
or `none` — checked independently of which method you actually used).

To make enforcement immutable on a machine, move the hook *registration*
(not the manifest) into `/Library/Application Support/ClaudeCode/managed-settings.json`
— it then requires admin rights to remove, while the manifest stays freely
editable at user scope.

## Uninstall — complete, in one command, nothing orphaned

Complicated uninstalls are how tools like this lose trust, so removal is a
first-class verb with an explicit inventory:

```bash
python3 ~/.claude/agentic-gate/agentic-gate.py uninstall          # disarm
python3 ~/.claude/agentic-gate/agentic-gate.py uninstall --purge  # remove every trace
```

| Path | Created by | Removed by |
|---|---|---|
| Hook entries in `~/.claude/settings.json` | `install` | `uninstall` |
| `~/.claude/agentic-gate/state/` | runtime | `uninstall` |
| `~/.claude/agentic-gate/agentic-gate.py` | `install` | `uninstall --purge` |
| `~/.claude/agentic-gate/environments.json` | `install` (seed) + your edits | `uninstall --purge` |
| `~/.claude/agentic-gate/crossings.log` | runtime | `uninstall --purge` |
| `settings.json.af-backup` | `install`/`uninstall` | you, when satisfied |

The default keeps your manifest (classification work) and crossings log
(audit trail); `--purge` removes everything. If you armed via the plugin,
disable the plugin instead — `uninstall` will tell you so rather than
guessing. Verify removal any time:

```bash
python3 ~/.claude/agentic-gate/agentic-gate.py status
grep -c agentic-gate ~/.claude/settings.json   # expect no matches
```

## Audit

```bash
python3 ~/.claude/agentic-gate/agentic-gate.py audit
```

Scans installed plugins for skills, agents, and commands, and reports
anything your manifest doesn't classify — run it after installing any new
pack, so new toolsets get classified deliberately instead of inheriting
access silently. Exit code 1 when unassigned items exist (CI-friendly).

```bash
python3 ~/.claude/agentic-gate/agentic-gate.py audit --check-updates
```

A separate, on-demand flag — never automatic, never part of `switch`
(which stays instant and offline on purpose). For every *already
classified* pattern across every environment, resolves its installed
version (from Claude Code's own `installed_plugins.json`, no network
needed) and, for GitHub-sourced marketplaces, checks the latest release
via the GitHub REST API (unauthenticated, 60 req/hr, one lookup cached
per repo per run). Writes a full report to
`~/.claude/agentic-gate/inventory.json` — installed/latest version,
marketplace, update channel, GitHub link, and an `up-to-date` /
`outdated` / `no-update-channel` / `unknown` / `n/a` status per resource —
plus a condensed stdout summary. A vendor plugin shipped through a local
directory marketplace rather than GitHub correctly reports
`no-update-channel`, not an error.

## Discovery and classification

```bash
python3 ~/.claude/agentic-gate/agentic-gate.py environments
python3 ~/.claude/agentic-gate/agentic-gate.py environments vendor-x
python3 ~/.claude/agentic-gate/agentic-gate.py environments shared
python3 ~/.claude/agentic-gate/agentic-gate.py environments VendorX:schema-builder
```

`environments` with no argument lists every environment (description, and
how many skills/agents/commands/mcp/paths each declares). A query that
**exactly matches an environment's name** (or the literal `shared`) shows
its *full* declared contents — every pattern, not just a count — since at
that point you're asking to see it, not search for it. Any other query
searches two ways at once: plain substring match against names/
descriptions/pattern *text*, and `fnmatch` of the query *as a concrete
identifier* against each declared glob — so searching a real agent name
answers "which environment would this exact call land in?" even when the
manifest only ever wrote down a wildcard like `VendorX:*`, never that
literal name.

```bash
python3 ~/.claude/agentic-gate/agentic-gate.py switch vendor-x "$CLAUDE_CODE_SESSION_ID"
python3 ~/.claude/agentic-gate/agentic-gate.py switch vendor-x "$CLAUDE_CODE_SESSION_ID" --preview
```

`switch` manually sets the active environment for a session — a third way
the active environment changes, alongside `SessionStart`'s project-home
lookup and `PostToolUse`'s automatic switch when a skill actually runs.
Refuses unknown environment names and lists the real ones instead of
guessing. **A real session id is required.** `switch vendor-x` with
nothing else, or `switch vendor-x default`, exits 2 rather than silently
writing state to a `'default'` bucket no real session reads — a call that
used to look like it worked while actually targeting nothing. Pass
`--allow-default` if you genuinely want that bucket on purpose.
**`$CLAUDE_CODE_SESSION_ID` is set in every Claude Code session** — use it
to target `status`/`switch` at the actual conversation you're in, rather
than a made-up ID: `agentic-gate.py status "$CLAUDE_CODE_SESSION_ID"` shows
this session's real active environment.

Add **`--preview [PATH]`** to also write a self-contained HTML status page
(default `~/.claude/agentic-gate/switch-preview.html`) — a visual answer to
"what did that switch actually change?": the environment switched from and
to, which hooks are actually enforcing right now, the new environment's
full declared surface plus the always-on shared tier, and a best-effort
**location** for every declared skill/agent/command — a Claude.ai account
skill (`anthropic-skills:*`, no local file), an installed plugin skill
(resolved marketplace + path under `~/.claude/plugins/cache/`), a
project's own `.claude/skills/`, or unresolved. Hand the file to whatever
can render it — in Claude Code, ask Claude to publish it as an Artifact.
Only the manual `switch` verb generates this; the automatic
SessionStart/PostToolUse switches stay silent, since a preview per silent
switch during ordinary work would be constant noise, not a useful signal.

```bash
python3 ~/.claude/agentic-gate/agentic-gate.py classify vendor-x --skill "VendorX:*"
python3 ~/.claude/agentic-gate/agentic-gate.py classify new-vendor --create "A new toolset" --skill "NewVendor:*"
python3 ~/.claude/agentic-gate/agentic-gate.py classify shared --command a-tool-everyone-may-use
```

`classify` adds skill/agent/command/mcp/path patterns to an environment's
declaration — the write-side companion to `audit` (finds what's
*unassigned*) and `environments` (finds what's already assigned *where*).
Add `--create "description"` to define a brand-new environment in the same
call; without it, `classify` refuses an unknown environment name rather
than silently creating a typo. The special target name **`shared`** writes
into the shared tier instead of a named environment — it always "exists"
implicitly, so `--create` doesn't apply to it. Adding a pattern that's
already present is a no-op, not a duplicate. The manifest is backed up
before every write, same
discipline as `install`'s settings.json backup.

## The manifest

`~/.claude/agentic-gate/environments.json`:

```json
{
  "version": 1,
  "environments": {
    "my-pack":   { "skills": ["my-pack:*"], "paths": ["*/my-skills/*"] },
    "vendor-x":  { "skills": ["VendorX:*"], "agents": ["VendorX:*"],
                   "commands": ["vx-build"], "mcp": ["mcp__vendorx__*"],
                   "paths": ["~/.claude/plugins/cache/vendorx/*"] }
  },
  "shared":   { "commands": ["a-delivery-tool-everyone-may-use"] },
  "policy":   { "default": "warn", "unknown": "warn",
                "pairs": { "vendor-x|vendor-y": "gate" } },
  "projects": { "/path/to/some/project": "vendor-x", "*": "my-pack" }
}
```

| Field | Meaning |
|---|---|
| `environments.<name>.skills` | Skill-name globs (`Skill` tool calls). |
| `environments.<name>.agents` | `subagent_type` globs (`Task` dispatches). |
| `environments.<name>.commands` | Bare command basenames (`Bash` calls). |
| `environments.<name>.mcp` | MCP tool-name globs (`mcp__server__tool`). |
| `environments.<name>.paths` | File globs (`Read`/`Write`/`Edit`, and paths inside `Bash` commands). |
| `shared` | Same shape — resources every environment may use, silently. Write to it with `classify shared ...` rather than editing this block by hand. |
| `policy.default` | `warn` \| `gate` \| `deny` for cross-environment calls. |
| `policy.unknown` | What happens when a skill/agent/MCP tool matches *no* environment — your protection when a new toolset is installed and not yet classified. |
| `projects.<path>` | Project path prefix → home environment, checked first, most specific wins (first prefix match in declaration order). |
| `projects.*` | Optional default/fallback environment for sessions outside every mapped project — checked last, never overrides a real path match. Omit it and unmapped sessions simply start with no active environment (set by the first skill that runs instead). |
| `policy.pairs` | Per-boundary overrides, e.g. `"a\|b": "gate"`. Unordered. |
| `projects` | Map of project path prefixes → home environment at session start. |

**Modes** — `warn` shows the crossing and lets it pass (lane-departure
warning); `gate`/`ask` halts on a native permission prompt where you approve
or refuse with one click (the agent must effectively "ask permission, and
say why"); `deny` refuses outright.

## What it can and cannot do

- ✅ Catches cross-environment **actions**: skill invocations, agent
  dispatches, vendor commands, reference-file reads, MCP tool calls.
- ✅ Deterministic — enforcement does not depend on the model remembering
  or agreeing.
- ✅ Fails open, logs everything, one file, no dependencies.
- ❌ Cannot stop a plugin's *context injection* (its instructions entering
  the session). That lever is per-project plugin enablement; Agentic-Gate's
  SessionStart report tells you what's loaded so nothing injects silently.
- ❌ Cannot referee knowledge the model already paraphrased from the wrong
  environment's context — it referees actions, which is where the damage
  becomes real.

## For toolset vendors

The manifest is a convention, not just a config file. If you ship a plugin,
publishing your own environment declaration (namespaces, agents, commands,
paths, MCP tools) lets every user's guardrail enforce your boundaries
without guesswork — and protects *your* toolset from being blamed for other
packs' crossings. One JSON block in your repo is enough.

## Roadmap

- Vendor manifest discovery (merge declarations shipped inside plugins).
- Per-environment default modes; time-boxed session grants.
- Crossing-report summary at SessionEnd.
- MCP server enumeration in `audit` (currently filesystem/plugin scan only).

## License

Code: [MIT](LICENSE). Documentation: [CC BY 4.0](LICENSE-docs) — Creative
Commons licences aren't designed for software, so the prose and the code
carry the licence each was designed for.

[Darrin Southern](https://www.linkedin.com/in/darrin-southern/),
[CadenceUX](https://cadenceux.com.au), 2026.
