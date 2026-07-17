# MCP Sync v1 Portability Contract

## Contents

- Portable transports and fields
- Secret-reference convention
- Direction and merge semantics
- Rejected source fields
- Native path mapping

## Portable transports and fields

The converter supports stdio and streamable HTTP. Claude's `http` maps to Codex streamable HTTP. Claude's legacy `sse` is rejected because treating it as streamable HTTP is not reliable.

| Portable concept | Claude native JSON | Codex native TOML |
| --- | --- | --- |
| stdio executable | `command` | `command` |
| stdio arguments | `args` | `args` |
| non-secret stdio environment values | `env.KEY = "value"` | `env.KEY = "value"` |
| secret stdio environment reference | `env.KEY = "${KEY}"` | `env_vars = ["KEY"]` |
| HTTP endpoint | `url` with `type = "http"` | `url` |
| non-secret HTTP header | `headers.NAME = "value"` | `http_headers.NAME = "value"` |
| environment-backed HTTP header | `headers.NAME = "${ENV}"` | `env_http_headers.NAME = "ENV"` |
| Bearer environment reference | `Authorization = "Bearer ${TOKEN}"` | `bearer_token_env_var = "TOKEN"` |

The stdio secret variable name must equal the child-process environment key. A Claude mapping such as `API_KEY = "${DIFFERENT_NAME}"` cannot be expressed with Codex `env_vars` and is rejected.

Codex may attach `source = "local" | "remote"` metadata to an `env_vars` item. When Codex is the target and the referenced name still exists, that target-only metadata is preserved. When Codex is the source, metadata-bearing `env_vars` items are rejected because Claude cannot express their sourcing semantics.

Claude expands `${VAR}` templates in additional string fields that Codex treats as literals. Therefore `command`, `args`, and `url` are portable only when they contain no `${...}` template in either source direction. Put variable data in the supported environment/header reference fields instead. Secret-bearing command-line arguments are also rejected; secrets must travel through environment references.

## Secret-reference convention

Use environment variable names as opaque references. The converter recognizes only:

- `${NAME}` for a direct environment reference;
- `Bearer ${NAME}` for the HTTP Authorization header.

It never expands environment variables, calls a secret store, prints secret-bearing values, or writes a literal value under a secret-like key. Secret-bearing command-line headers/assignments, URL user information, and secret-like URL query parameters are rejected as well. Populate the referenced environment outside the conversion flow. Client OAuth login and other authentication state are separate from configuration synchronization.

Literal values remain allowed for fields that do not appear secret-bearing, such as `NODE_ENV`, `Content-Type`, or a feature flag. Secret-like environment/header names containing concepts such as token, secret, password, credential, authorization, cookie, or ending in key require an opaque reference.

Direct `op://...` strings are not a cross-client runtime reference. If 1Password is used, inject the result into the named environment variable outside this converter; keep only `${NAME}` / `env_vars` in the native MCP definitions.

## Direction and merge semantics

Every mutation has one explicit source and one derived target. The normalized model exists only in memory.

- `sync` adds or updates a named target entry.
- `delete` requires source direction, scope, and name. Missing source data never triggers it.
- `rename` validates the new source entry, deletes the old target name, and adds the new target name in one atomic target-file write.
- `check` compares portable fields and never writes.

The converter removes the target entry's old portable fields, renders the source portable fields, and retains compatible target-only fields. Other servers and top-level settings remain untouched. Repeating the same operation is idempotent.

Transport-specific target fields must remain valid after conversion. For example, Codex/Claude OAuth metadata is HTTP-only and Codex `cwd` is stdio-only. If the resulting transport is incompatible, the operation fails before writing. During a transport change, an unrecognized target-only field also causes a pre-write failure because its compatibility cannot be proven.

## Rejected source fields

Only the fields in the table above are source-portable. Examples rejected when present on the selected source include:

- Claude: `oauth`, SSE transport, client-specific extensions such as header helpers;
- Codex: `oauth`, `oauth_resource`, `cwd`, `enabled`, `required`, startup/tool timeouts, tool allow/deny lists, and other client policy fields.

Value-level constructs can also be non-portable even inside a field listed as portable. The converter rejects client-dependent `${...}` templates in `command`, `args`, `url`, or Codex literal headers; secret-bearing arguments and URLs; metadata-bearing Codex source `env_vars`; duplicate JSON keys whose effective definition would be ambiguous; and non-standard JSON constants such as `NaN` or `Infinity`.

Those fields may remain on the target and are preserved there. Rejection applies when a client carrying such a field is selected as the source, because silently discarding it would change behavior.

## Native path mapping

| Scope | Claude | Codex |
| --- | --- | --- |
| global/user | `$CLAUDE_CONFIG_DIR/.claude.json` or `~/.claude.json` | `$CODEX_HOME/config.toml` or `~/.codex/config.toml` |
| project | `<root>/.mcp.json` | `<root>/.codex/config.toml` |
| Claude local | rejected | no portable mapping |

Codex CLI MCP add/remove commands currently maintain global configuration only. The converter therefore patches project TOML directly and validates it rather than routing project mutations through the CLI.
