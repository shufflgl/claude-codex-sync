@../.codex/AGENTS.md

# Claude-Specific Rule Maintenance

To keep Claude and Codex on the same shared rule set, the following files are the single source of truth for their respective rule scopes:

- Global rules: `~/.codex/AGENTS.md`
- Project rules: `<project-root>/AGENTS.md`
- Local rules: `AGENTS.local.md` in the corresponding directory

Add, remove, or modify shared rules only in the corresponding `AGENTS.md` or `AGENTS.local.md` file.

`CLAUDE.md` and `CLAUDE.local.md` may be created, but they may contain only:

1. An import of the corresponding AGENTS file on the first line:
   - Global `CLAUDE.md`: `@../.codex/AGENTS.md`
   - Project `CLAUDE.md`: `@AGENTS.md`
   - `CLAUDE.local.md`: `@AGENTS.local.md`
2. Client-specific rules that apply only to Claude.

Do not maintain shared rules in `CLAUDE.md` files unless the user explicitly requests Claude-specific rules.

If the corresponding AGENTS file is already in the effective context through an `@` import, do not read or apply it again.
