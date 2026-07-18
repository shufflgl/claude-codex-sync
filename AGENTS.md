# Local Rules

When working in a project, load applicable `AGENTS.local.md` files after the normal `AGENTS.md` instruction chain:

1. Load the file at the project root, if present.
2. If the current working directory differs from the project root, load the file in the current working directory, if present.

Resolve files to canonical paths and load each distinct file at most once. If a file is already present in the effective context, either directly or through a `CLAUDE.local.md` import, do not read or apply it again.

Treat `AGENTS.local.md` and `CLAUDE.local.md` as private, user-specific rules and never commit either file.

If a local rule conflicts with repository rules, follow the local rule unless the conflict involves architectural red lines, security constraints, or a higher-priority instruction. In those cases, tell the user before proceeding.

# Skill Maintenance and Synchronization

The single source of truth for shared skills is:

- Global skills: `~/.agents/skills/<skill-name>`
- Project skills: `<project-root>/.agents/skills/<skill-name>`

Codex reads these directories natively. Do not create additional symbolic links for Codex, and do not modify or overwrite `~/.codex/skills/.system`.

To make the same skills available to Claude, create a directory symbolic link for each individual skill:

- Global: `~/.claude/skills/<skill-name>` -> `~/.agents/skills/<skill-name>`
- Project: `<project-root>/.claude/skills/<skill-name>` -> `<project-root>/.agents/skills/<skill-name>`

Create links for individual skill directories only. Do not link or replace the entire `skills` directory.

Skill maintenance follows an ownership model: whoever creates, modifies, deletes, or renames a skill is responsible for maintaining its directory and Claude link, and for verifying that the link exists, points to the correct target, and is not dangling. Maintenance is complete only when both the skill and its link are in the expected state.

Unless the user explicitly requests a global skill, create a project-level skill by default.

# MCP Maintenance and Synchronization

Claude and Codex native MCP configurations are runtime configuration. Do not create a third persistent MCP configuration as a single source of truth.

Use the global `mcp-sync` skill for every MCP creation, modification, deletion, rename, or consistency check. MCP synchronization follows an ownership model: whoever maintains an MCP in one client is responsible for converting and synchronizing the same change to the other client, then verifying the result.

Synchronization must be a directional conversion:

- By default, use the client currently executing the task as the source.
- The user or caller may explicitly designate Claude or Codex as the source.
- Do not perform an undirected bidirectional merge.
- Do not infer the source from file modification times.
- Do not automatically interpret an entry that exists on the target but not on the source as a deletion.

Use the following conversion process:

1. Read the source client's native configuration.
2. Convert it in memory to a temporary, normalized portable model.
3. Merge portable fields into, and render them in, the target client's native configuration.
4. Validate the target configuration syntax and conversion result.

Do not persist the temporary normalized model as a third configuration.

The initial synchronization scope includes global and project MCPs only. Claude `local` scope is not portable; explicitly refuse it rather than guessing a target location.

During synchronization:

- Convert only portable fields that both clients can express.
- Preserve target configuration unrelated to this MCP, other MCPs, and target-client-specific fields.
- If a target-client-specific field is incompatible with the converted transport, or its compatibility cannot be established after a transport change, stop and report before writing. Do not retain an invalid field or silently delete it.
- If the source uses client-specific fields that cannot be converted reliably, report the specific fields and stop before writing. Do not silently drop fields or perform a partial write.
- A deletion must explicitly include the MCP name and scope. An MCP absent from the source is not itself a deletion instruction.
- Handle a rename as one operation: delete the old name, then add the new name.
- `check` verifies consistency only and must not modify configuration.
- Repeating synchronization with the same input must be idempotent.

Secrets may use only the shared opaque-reference convention. During synchronization, never resolve, print, log, or materialize actual secret values. Client authentication and login state are outside MCP format conversion and must be completed separately by each client.

# Retrieve Secrets Through 1Password

If a task requires a token, API key, password, or other secret, use the 1Password CLI (`op`) to retrieve it rather than asking the user to paste it or searching plaintext files, unless the user explicitly requests another method.

Do not print secrets in outputs or logs. Use them only as needed for the requested operation. If `op` is unavailable or authentication cannot be completed, stop and report the missing prerequisite rather than falling back to plaintext.
