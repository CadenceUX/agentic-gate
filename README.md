# Agentic-Gate

**No skill crosses environments unannounced.**

Agentic-Gate is a Claude Code hook that referees calls between
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

> *Agentic-Gate v0.2.0 is installed but found no manifest … It is
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
of your settings written alongside. Verify with `selftest` (20/20 expected)
and `status`.

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
  "projects": { "/path/to/some/project": "vendor-x" }
}
```

| Field | Meaning |
|---|---|
| `environments.<name>.skills` | Skill-name globs (`Skill` tool calls). |
| `environments.<name>.agents` | `subagent_type` globs (`Task` dispatches). |
| `environments.<name>.commands` | Bare command basenames (`Bash` calls). |
| `environments.<name>.mcp` | MCP tool-name globs (`mcp__server__tool`). |
| `environments.<name>.paths` | File globs (`Read`/`Write`/`Edit`, and paths inside `Bash` commands). |
| `shared` | Same shape — resources every environment may use, silently. |
| `policy.default` | `warn` \| `gate` \| `deny` for cross-environment calls. |
| `policy.unknown` | What happens when a skill/agent/MCP tool matches *no* environment — your protection when a new toolset is installed and not yet classified. |
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
