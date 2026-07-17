---
name: mcp-sync
description: Synchronize portable MCP server definitions between Claude Code and Codex while preserving each client's native configuration. Use whenever an MCP server is created, updated, deleted, renamed, or checked at global/user or project scope, or when Claude and Codex MCP configurations have drifted.
---

# MCP Sync

Use each client's native MCP configuration as runtime state. Do not create a third persistent MCP registry. Convert only the named server through an ephemeral in-memory model by running `scripts/mcp_sync.py`.

## Select direction and scope

Treat the client executing the task as the source unless the user explicitly names another source:

- When running in Claude Code, pass `--from claude`.
- When running in Codex, pass `--from codex`.
- Use `--scope global` for Claude user scope and Codex global scope.
- Use `--scope project --project-root <root>` for repository configuration.
- Refuse Claude `local` scope. It has no portable Codex equivalent.

Never infer direction from modification times or merge both sides. A missing source entry is not a deletion instruction.

## Maintain and synchronize

For create or update:

1. Apply the requested definition to the selected source client's native config.
2. Use only the secret-reference forms in [references/portability.md](references/portability.md). Never place a literal secret in either config.
3. Preview the conversion:

   ```console
   python scripts/mcp_sync.py sync --from codex --scope global --name example --dry-run
   ```

4. Run the same command without `--dry-run`.
5. Run `check` and require an `in-sync` result:

   ```console
   python scripts/mcp_sync.py check --scope global --name example
   ```

For an explicit deletion, delete from the source native config first, then propagate the named deletion. The script never treats absence as an implicit delete:

```console
python scripts/mcp_sync.py delete --from claude --scope project --project-root <root> --name example
```

For a rename, rename the source entry first, then perform one target operation:

```console
python scripts/mcp_sync.py rename --from codex --scope project --project-root <root> --old-name old --new-name new
```

Run `--dry-run` before a delete or rename when the request is ambiguous or the native files contain unfamiliar structures.

## Preserve native configuration

Allow the script to patch only the named target entry. It preserves:

- unrelated top-level client settings;
- other MCP servers;
- target-only fields on the named MCP server;
- native JSON/TOML structure outside the replaced server block.

Preserve target-only fields only when they remain valid for the resulting transport. If a target-only field is incompatible with a stdio/HTTP switch, or its compatibility cannot be proven, stop before writing and report it rather than retaining an invalid configuration or silently deleting it.

If the source contains client-only fields, unsupported fields, or client-dependent `${...}` expansion in otherwise portable fields, stop before writing and report the field names. Do not drop fields, partially synchronize, or reinterpret SSE as streamable HTTP. See [references/portability.md](references/portability.md) for the exact v1 field boundary.

The converter writes atomically and validates target syntax plus portable equivalence after a mutation. Authentication and login remain client-local; perform them separately when required.

## Resolve paths safely

Default paths are:

- Claude global/user: `$CLAUDE_CONFIG_DIR/.claude.json` when `CLAUDE_CONFIG_DIR` is set, otherwise `~/.claude.json`
- Codex global: `$CODEX_HOME/config.toml` when `CODEX_HOME` is set, otherwise `~/.codex/config.toml`
- Claude project: `<project-root>/.mcp.json`
- Codex project: `<project-root>/.codex/config.toml`

Use `--claude-config` and `--codex-config` only for an intentional custom layout or isolated testing. Resolve the project root before invoking project-scope operations.
