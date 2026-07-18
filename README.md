<div align="center">
  <img src="https://unpkg.com/@lobehub/icons-static-svg@latest/icons/claude-color.svg" width="50" height="50" alt="Claude" />
  &nbsp;&nbsp;&nbsp;
  <img src="https://unpkg.com/@lobehub/icons-static-svg@latest/icons/codex-color.svg" width="50" height="50" alt="Codex" />
</div>

# claude-codex-sync

Rules, skills, and MCP definitions for Claude Code and Codex tend to drift apart because each client keeps its own native format and location. This project defines a single source of truth for both clients and the conventions needed to keep them in sync.

## Contents

- **`AGENTS.md`** — Global rules shared by both clients. Codex reads this file natively at `~/.codex/AGENTS.md`. Claude Code picks it up through `~/.claude/CLAUDE.md`, which imports it via `@../.codex/AGENTS.md` on its first line.
- **`CLAUDE.md`** — Claude-specific entry point. It imports `AGENTS.md` and may only add rules that apply to Claude alone; shared rules always live in `AGENTS.md`, never duplicated here.
- **`mcp-sync/`** — A skill that synchronizes MCP server definitions between Claude Code's and Codex's native configuration files, without introducing a third persistent config format.

## Rule layering

Both clients resolve rules in the same layers:

1. **Global** — `~/.codex/AGENTS.md` (imported by `~/.claude/CLAUDE.md`)
2. **Project** — `<project-root>/AGENTS.md` (imported by `<project-root>/CLAUDE.md`)
3. **Local** — `AGENTS.local.md` in the relevant directory (imported by `CLAUDE.local.md`, and never committed)

Shared rules are added, removed, or modified only in the `AGENTS.md` / `AGENTS.local.md` files. The corresponding `CLAUDE.md` / `CLAUDE.local.md` files stay minimal: an `@`-import line plus, optionally, Claude-only rules.

## Skill maintenance

Skills live once, at:

- Global: `~/.agents/skills/<skill-name>`
- Project: `<project-root>/.agents/skills/<skill-name>`

Codex reads these directories natively. Claude Code gets access to the same skill through a directory symlink:

- Global: `~/.claude/skills/<skill-name>` -> `~/.agents/skills/<skill-name>`
- Project: `<project-root>/.claude/skills/<skill-name>` -> `<project-root>/.agents/skills/<skill-name>`

Whoever creates, modifies, or removes a skill is responsible for keeping its Claude symlink correct and non-dangling.

The `mcp-sync/` directory at the project root is a plain reference copy of the skill for browsing and version control. The live, active copy used by the clients is installed at `~/.agents/skills/mcp-sync` (global), with a Claude symlink at `~/.claude/skills/mcp-sync`.

## MCP synchronization

Claude Code and Codex each keep MCP server definitions in their own native config files (`.claude.json` / `.mcp.json` for Claude, `config.toml` for Codex). Neither file is treated as a source of truth for the other — the `mcp-sync` skill converts a named server definition from one client's native config into the other's, on demand, through an ephemeral in-memory model. See `mcp-sync/SKILL.md` for usage.

Key properties of the sync process:

- Directional: the client executing the task is the source by default; the user can override this explicitly.
- Scoped to global or project MCPs (Claude's `local` scope is not portable and is refused).
- Non-destructive: an entry missing on one side is never treated as an implicit deletion.
- Idempotent: repeating a sync with the same input produces the same result.
- Secret-safe: only opaque secret references are synchronized, never literal secret values.
