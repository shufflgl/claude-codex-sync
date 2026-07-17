#!/usr/bin/env python3
"""Synchronize portable MCP server definitions between Claude Code and Codex.

The script deliberately has no persistent intermediate representation. It reads one
native configuration, builds an in-memory portable model, and patches only the named
server in the other native configuration.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit


ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
BEARER_REF_RE = re.compile(
    r"^Bearer[ \t]+\$\{([A-Za-z_][A-Za-z0-9_]*)\}$", re.IGNORECASE
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BARE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

CLAUDE_PORTABLE_FIELDS = {"type", "command", "args", "env", "url", "headers"}
CODEX_PORTABLE_FIELDS = {
    "command",
    "args",
    "env",
    "env_vars",
    "url",
    "http_headers",
    "env_http_headers",
    "bearer_token_env_var",
}
TARGET_COMMON_FIELDS = {
    "claude": {"alwaysAllow", "disabled", "timeout"},
    "codex": {
        "disabled_tools",
        "enabled",
        "enabled_tools",
        "required",
        "startup_timeout_sec",
        "tool_timeout_sec",
    },
}
TARGET_TRANSPORT_FIELDS = {
    "claude": {
        "http": {"headersHelper", "oauth"},
        "stdio": set(),
    },
    "codex": {
        "http": {"oauth", "oauth_resource"},
        "stdio": {"cwd"},
    },
}


class SyncError(RuntimeError):
    """A safe, user-facing error that never contains configuration values."""


class DuplicateJsonKey(ValueError):
    def __init__(self, key: str):
        self.key = key
        super().__init__(key)


class NonStandardJsonConstant(ValueError):
    pass


@dataclass(frozen=True)
class PortableValue:
    kind: str  # literal | env
    value: str
    prefix: str = ""


@dataclass(frozen=True)
class PortableServer:
    transport: str  # stdio | http
    command: str | None = None
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, PortableValue], ...] = ()
    url: str | None = None
    headers: tuple[tuple[str, PortableValue], ...] = ()


@dataclass(frozen=True)
class JsonObjectEntry:
    key: str
    key_start: int
    value_start: int
    value_end: int
    comma_after: int | None


def _sensitive_name(name: str) -> bool:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", separated)
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", separated.lower()) if part]
    compact = "".join(parts)
    if not parts:
        return False
    if set(parts) & {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "passwd",
        "password",
        "secret",
        "token",
    }:
        return True
    return parts[-1] == "key" or compact.endswith(
        (
            "accesskey",
            "accesstoken",
            "apikey",
            "authorization",
            "authkey",
            "cookie",
            "credential",
            "credentials",
            "password",
            "passwd",
            "privatekey",
            "secret",
            "secretkey",
            "serviceaccount",
            "token",
        )
    )


def _portable_literal(value: str, field: str) -> str:
    if "${" in value:
        raise SyncError(
            f"field {field!r} contains a client-dependent environment template and is not portable"
        )
    return value


def _portable_arguments(value: Any, field: str) -> list[str]:
    args = _require_string_list(value, field)
    for index, argument in enumerate(args):
        _portable_literal(argument, f"{field}[{index}]")
        flag = argument.split("=", 1)[0].lstrip("-")
        assignment_is_sensitive = any(
            delimiter in argument
            and _sensitive_name(argument.rsplit(delimiter, 1)[0].lstrip("-"))
            for delimiter in ("=", ":")
        )
        if (flag and _sensitive_name(flag)) or assignment_is_sensitive:
            raise SyncError(
                f"field {field!r} contains a secret-bearing argument; use an environment reference"
            )
    return args


def _portable_url(value: str, field: str) -> str:
    _portable_literal(value, field)
    try:
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise SyncError(f"field {field!r} is not a valid portable URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SyncError(f"field {field!r} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise SyncError(f"field {field!r} contains user information; use authentication references")
    if any(_sensitive_name(key) for key, _ in query):
        raise SyncError(f"field {field!r} contains a secret-bearing query parameter")
    return value


def _require_string(value: Any, field: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise SyncError(f"field {field!r} must be a{' non-empty' if nonempty else ''} string")
    return value


def _require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SyncError(f"field {field!r} must be an array of strings")
    return value


def _require_string_map(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SyncError(f"field {field!r} must be an object/table")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise SyncError(f"field {field!r} must contain only string keys and string values")
    return dict(value)


def _codex_env_var_items(
    value: Any, *, strict_source: bool
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if not isinstance(value, list):
        raise SyncError("field 'env_vars' must be an array")
    names: list[str] = []
    metadata: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = _require_string(item.get("name"), f"env_vars[{index}].name", nonempty=True)
            if strict_source:
                raise SyncError(
                    f"field 'env_vars[{index}]' carries Codex-only source metadata"
                )
            source = item.get("source")
            if source is not None and source not in {"local", "remote"}:
                raise SyncError(f"field 'env_vars[{index}].source' is invalid")
            metadata[name] = copy.deepcopy(item)
        else:
            raise SyncError("field 'env_vars' must contain strings or named metadata objects")
        if not ENV_NAME_RE.fullmatch(name):
            raise SyncError("field 'env_vars' contains an invalid environment variable name")
        if name in seen:
            raise SyncError(f"environment variable {name!r} appears more than once in env_vars")
        seen.add(name)
        names.append(name)
    return names, metadata


def _environment_value(key: str, value: str, field: str) -> PortableValue:
    match = ENV_REF_RE.fullmatch(value)
    if match:
        env_name = match.group(1)
        if env_name != key:
            raise SyncError(
                f"field {field!r} maps {key!r} to a differently named environment reference; "
                "portable stdio secret references must use ${KEY} with the same KEY"
            )
        return PortableValue("env", env_name)
    if "${" in value:
        raise SyncError(f"field {field!r} uses an unsupported environment template")
    if _sensitive_name(key):
        raise SyncError(
            f"field {field!r} appears secret-bearing but contains a literal; use ${{{key}}}"
        )
    return PortableValue("literal", value)


def _codex_environment_literal(key: str, value: str, field: str) -> PortableValue:
    if "${" in value:
        raise SyncError(
            f"field {field!r} uses a Claude-style template inside Codex env; use env_vars instead"
        )
    if _sensitive_name(key):
        raise SyncError(
            f"field {field!r} appears secret-bearing but contains a literal; use env_vars"
        )
    return PortableValue("literal", value)


def _header_value(key: str, value: str, field: str) -> PortableValue:
    match = ENV_REF_RE.fullmatch(value)
    if match:
        return PortableValue("env", match.group(1))
    match = BEARER_REF_RE.fullmatch(value)
    if match:
        if key.lower() != "authorization":
            raise SyncError(f"field {field!r} uses Bearer syntax outside Authorization")
        return PortableValue("env", match.group(1), "Bearer ")
    if "${" in value:
        raise SyncError(f"field {field!r} uses an unsupported environment template")
    if _sensitive_name(key):
        raise SyncError(
            f"field {field!r} appears secret-bearing but contains a literal environment value"
        )
    return PortableValue("literal", value)


def _canonical_header_name(name: str) -> str:
    return "Authorization" if name.lower() == "authorization" else name


def _portable_headers(raw_headers: dict[str, str], field: str) -> tuple[tuple[str, PortableValue], ...]:
    values: dict[str, tuple[str, PortableValue]] = {}
    for key, raw_value in raw_headers.items():
        if not key or any(ord(char) < 0x20 for char in key):
            raise SyncError(f"field {field!r} contains an invalid HTTP header name")
        folded = key.lower()
        if folded in values:
            raise SyncError(f"field {field!r} contains duplicate case-insensitive HTTP headers")
        canonical = _canonical_header_name(key)
        values[folded] = (canonical, _header_value(canonical, raw_value, f"{field}.{key}"))
    return tuple(sorted(values.values(), key=lambda item: item[0].lower()))


def parse_claude_server(entry: Any, *, strict_source: bool) -> PortableServer:
    if not isinstance(entry, dict):
        raise SyncError("Claude MCP entry must be an object")
    if strict_source:
        unsupported = sorted(set(entry) - CLAUDE_PORTABLE_FIELDS)
        if unsupported:
            raise SyncError("unsupported Claude source fields: " + ", ".join(unsupported))

    raw_type = entry.get("type")
    if raw_type is None:
        raw_type = "http" if "url" in entry else "stdio"
    transport = _require_string(raw_type, "type", nonempty=True)
    if transport == "sse":
        raise SyncError("Claude SSE transport is not portable to Codex streamable HTTP")
    if transport not in {"stdio", "http"}:
        raise SyncError(f"unsupported Claude transport {transport!r}")

    if transport == "stdio":
        if "url" in entry or "headers" in entry:
            raise SyncError("Claude stdio entry contains HTTP-only fields")
        command = _portable_literal(
            _require_string(entry.get("command"), "command", nonempty=True), "command"
        )
        args = _portable_arguments(entry.get("args", []), "args")
        raw_env = _require_string_map(entry.get("env", {}), "env")
        env = tuple(
            (key, _environment_value(key, value, f"env.{key}"))
            for key, value in sorted(raw_env.items())
        )
        return PortableServer("stdio", command=command, args=tuple(args), env=env)

    if "command" in entry or "args" in entry or "env" in entry:
        raise SyncError("Claude HTTP entry contains stdio-only fields")
    url = _portable_url(_require_string(entry.get("url"), "url", nonempty=True), "url")
    raw_headers = _require_string_map(entry.get("headers", {}), "headers")
    headers = _portable_headers(raw_headers, "headers")
    return PortableServer("http", url=url, headers=headers)


def parse_codex_server(entry: Any, *, strict_source: bool) -> PortableServer:
    if not isinstance(entry, dict):
        raise SyncError("Codex MCP entry must be a TOML table")
    if strict_source:
        unsupported = sorted(set(entry) - CODEX_PORTABLE_FIELDS)
        if unsupported:
            raise SyncError("unsupported Codex source fields: " + ", ".join(unsupported))

    has_stdio = "command" in entry
    has_http = "url" in entry
    if has_stdio == has_http:
        raise SyncError("Codex MCP entry must contain exactly one of command or url")

    if has_stdio:
        forbidden = {"http_headers", "env_http_headers", "bearer_token_env_var"} & set(entry)
        if forbidden:
            raise SyncError("Codex stdio entry contains HTTP-only fields: " + ", ".join(sorted(forbidden)))
        command = _portable_literal(
            _require_string(entry.get("command"), "command", nonempty=True), "command"
        )
        args = _portable_arguments(entry.get("args", []), "args")
        raw_env = _require_string_map(entry.get("env", {}), "env")
        values: dict[str, PortableValue] = {
            key: _codex_environment_literal(key, value, f"env.{key}")
            for key, value in raw_env.items()
        }
        env_vars, _ = _codex_env_var_items(
            entry.get("env_vars", []), strict_source=strict_source
        )
        for env_name in env_vars:
            if not ENV_NAME_RE.fullmatch(env_name):
                raise SyncError("field 'env_vars' contains an invalid environment variable name")
            if env_name in values:
                raise SyncError(f"environment variable {env_name!r} appears in both env and env_vars")
            values[env_name] = PortableValue("env", env_name)
        return PortableServer(
            "stdio",
            command=command,
            args=tuple(args),
            env=tuple(sorted(values.items())),
        )

    forbidden = {"args", "env", "env_vars"} & set(entry)
    if forbidden:
        raise SyncError("Codex HTTP entry contains stdio-only fields: " + ", ".join(sorted(forbidden)))
    url = _portable_url(_require_string(entry.get("url"), "url", nonempty=True), "url")
    raw_static = _require_string_map(entry.get("http_headers", {}), "http_headers")
    raw_env_headers = _require_string_map(entry.get("env_http_headers", {}), "env_http_headers")
    values: dict[str, tuple[str, PortableValue]] = {}
    for key, value in raw_static.items():
        folded = key.lower()
        if folded in values:
            raise SyncError("field 'http_headers' contains duplicate case-insensitive headers")
        if _sensitive_name(key):
            raise SyncError(f"field 'http_headers.{key}' appears secret-bearing but contains a literal")
        _portable_literal(value, f"http_headers.{key}")
        canonical = _canonical_header_name(key)
        values[folded] = (canonical, PortableValue("literal", value))
    for key, env_name in raw_env_headers.items():
        folded = key.lower()
        if not ENV_NAME_RE.fullmatch(env_name):
            raise SyncError(f"field 'env_http_headers.{key}' contains an invalid environment name")
        if folded in values:
            raise SyncError(f"HTTP header {key!r} appears in both header maps")
        canonical = _canonical_header_name(key)
        values[folded] = (canonical, PortableValue("env", env_name))
    bearer = entry.get("bearer_token_env_var")
    if bearer is not None:
        bearer = _require_string(bearer, "bearer_token_env_var", nonempty=True)
        if not ENV_NAME_RE.fullmatch(bearer):
            raise SyncError("field 'bearer_token_env_var' contains an invalid environment name")
        if "authorization" in values:
            raise SyncError("Authorization is configured both as a header and bearer_token_env_var")
        values["authorization"] = (
            "Authorization",
            PortableValue("env", bearer, "Bearer "),
        )
    return PortableServer(
        "http",
        url=url,
        headers=tuple(sorted(values.values(), key=lambda item: item[0].lower())),
    )


def render_claude_server(server: PortableServer) -> dict[str, Any]:
    if server.transport == "stdio":
        result: dict[str, Any] = {
            "type": "stdio",
            "command": server.command,
            "args": list(server.args),
        }
        if server.env:
            result["env"] = {
                key: (f"${{{value.value}}}" if value.kind == "env" else value.value)
                for key, value in server.env
            }
        else:
            result["env"] = {}
        return result

    headers: dict[str, str] = {}
    for key, value in server.headers:
        headers[key] = (
            f"{value.prefix}${{{value.value}}}" if value.kind == "env" else value.value
        )
    result = {"type": "http", "url": server.url}
    if headers:
        result["headers"] = headers
    return result


def render_codex_server(server: PortableServer) -> dict[str, Any]:
    if server.transport == "stdio":
        result: dict[str, Any] = {"command": server.command, "args": list(server.args)}
        literals = {key: value.value for key, value in server.env if value.kind == "literal"}
        env_vars = [key for key, value in server.env if value.kind == "env"]
        if literals:
            result["env"] = literals
        if env_vars:
            result["env_vars"] = env_vars
        return result

    result = {"url": server.url}
    static_headers: dict[str, str] = {}
    env_headers: dict[str, str] = {}
    for key, value in server.headers:
        if value.kind == "literal":
            static_headers[key] = value.value
        elif key.lower() == "authorization" and value.prefix.lower() == "bearer ":
            result["bearer_token_env_var"] = value.value
        elif not value.prefix:
            env_headers[key] = value.value
        else:
            raise SyncError(f"header {key!r} uses a prefix Codex cannot express portably")
    if static_headers:
        result["http_headers"] = static_headers
    if env_headers:
        result["env_http_headers"] = env_headers
    return result


def _native_transport(client: str, entry: dict[str, Any]) -> str | None:
    if client == "claude":
        raw_type = entry.get("type")
        if raw_type in {"http", "stdio"}:
            return raw_type
        if raw_type == "sse":
            return "sse"
    has_command = "command" in entry
    has_url = "url" in entry
    if has_command != has_url:
        return "stdio" if has_command else "http"
    return None


def _validate_target_extras(
    client: str, existing: dict[str, Any], portable: PortableServer
) -> None:
    portable_fields = CLAUDE_PORTABLE_FIELDS if client == "claude" else CODEX_PORTABLE_FIELDS
    extras = set(existing) - portable_fields
    compatible_transport_fields = TARGET_TRANSPORT_FIELDS[client][portable.transport]
    incompatible_transport_fields = set().union(
        *(
            fields
            for transport, fields in TARGET_TRANSPORT_FIELDS[client].items()
            if transport != portable.transport
        )
    )
    incompatible = sorted(extras & incompatible_transport_fields)
    if incompatible:
        raise SyncError(
            f"target {client} fields are incompatible with {portable.transport}: "
            + ", ".join(incompatible)
        )

    old_transport = _native_transport(client, existing)
    if old_transport is not None and old_transport != portable.transport:
        unknown = sorted(extras - TARGET_COMMON_FIELDS[client] - compatible_transport_fields)
        if unknown:
            raise SyncError(
                f"cannot prove target {client} fields remain valid across "
                f"{old_transport} -> {portable.transport}: "
                + ", ".join(unknown)
            )


def merge_target_entry(client: str, existing: Any, portable: PortableServer) -> dict[str, Any]:
    if existing is None:
        merged: dict[str, Any] = {}
    elif isinstance(existing, dict):
        merged = dict(existing)
        _validate_target_extras(client, merged, portable)
    else:
        raise SyncError(f"existing {client} target entry is not an object/table")
    portable_fields = CLAUDE_PORTABLE_FIELDS if client == "claude" else CODEX_PORTABLE_FIELDS
    for field in portable_fields:
        merged.pop(field, None)
    rendered = render_claude_server(portable) if client == "claude" else render_codex_server(portable)
    merged.update(rendered)
    if client == "codex" and portable.transport == "stdio":
        ref_names = [key for key, value in portable.env if value.kind == "env"]
        if ref_names:
            _, metadata = _codex_env_var_items(
                existing.get("env_vars", []) if isinstance(existing, dict) else [],
                strict_source=False,
            )
            merged["env_vars"] = [copy.deepcopy(metadata.get(name, name)) for name in ref_names]
    return merged


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_dir():
        raise SyncError(f"configuration path is a directory: {path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SyncError(f"cannot read configuration file: {path}") from exc


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.resolve() if path.is_symlink() else path
    mode: int | None = None
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, target)
    except OSError as exc:
        raise SyncError(f"cannot atomically write configuration file: {path}") from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _skip_json_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _scan_json_string(text: str, index: int) -> int:
    if index >= len(text) or text[index] != '"':
        raise SyncError("invalid JSON object key")
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        if ord(char) < 0x20:
            raise SyncError("invalid control character in JSON string")
        index += 1
    raise SyncError("unterminated JSON string")


def _scan_json_value(text: str, index: int) -> int:
    index = _skip_json_ws(text, index)
    if index >= len(text):
        raise SyncError("missing JSON value")
    if text[index] == '"':
        return _scan_json_string(text, index)
    if text[index] in "{[":
        stack = [text[index]]
        index += 1
        while index < len(text) and stack:
            char = text[index]
            if char == '"':
                index = _scan_json_string(text, index)
                continue
            if char in "{[":
                stack.append(char)
            elif char in "}]":
                expected = "{" if char == "}" else "["
                if not stack or stack[-1] != expected:
                    raise SyncError("mismatched delimiter in JSON")
                stack.pop()
            index += 1
        if stack:
            raise SyncError("unterminated JSON container")
        return index
    end = index
    while end < len(text) and text[end] not in ",]} \t\r\n":
        end += 1
    if end == index:
        raise SyncError("invalid JSON scalar")
    return end


def _json_object_entries(text: str, object_start: int) -> tuple[list[JsonObjectEntry], int]:
    if object_start >= len(text) or text[object_start] != "{":
        raise SyncError("expected a JSON object")
    entries: list[JsonObjectEntry] = []
    index = object_start + 1
    while True:
        index = _skip_json_ws(text, index)
        if index >= len(text):
            raise SyncError("unterminated JSON object")
        if text[index] == "}":
            return entries, index
        key_start = index
        key_end = _scan_json_string(text, key_start)
        try:
            key = json.loads(text[key_start:key_end])
        except json.JSONDecodeError as exc:
            raise SyncError("invalid JSON object key") from exc
        index = _skip_json_ws(text, key_end)
        if index >= len(text) or text[index] != ":":
            raise SyncError("missing colon in JSON object")
        value_start = _skip_json_ws(text, index + 1)
        value_end = _scan_json_value(text, value_start)
        index = _skip_json_ws(text, value_end)
        comma_after: int | None = None
        if index < len(text) and text[index] == ",":
            comma_after = index
            index += 1
        elif index >= len(text) or text[index] != "}":
            raise SyncError("missing comma in JSON object")
        entries.append(JsonObjectEntry(key, key_start, value_start, value_end, comma_after))
        if comma_after is None:
            continue


def _format_json_value(value: Any, prefix: str) -> str:
    rendered = json.dumps(value, indent=2, ensure_ascii=False)
    return rendered.replace("\n", "\n" + prefix)


def _line_prefix(text: str, index: int) -> str:
    line_start = text.rfind("\n", 0, index) + 1
    prefix = text[line_start:index]
    return prefix if not prefix.strip() else ""


_DELETE = object()


def _patch_json_object_property(text: str, object_start: int, key: str, value: Any) -> str:
    entries, close_index = _json_object_entries(text, object_start)
    found_index = next((i for i, entry in enumerate(entries) if entry.key == key), None)
    newline = "\r\n" if "\r\n" in text else "\n"
    parent_prefix = _line_prefix(text, object_start)
    child_prefix = (
        _line_prefix(text, entries[0].key_start) if entries else parent_prefix + "  "
    )
    if not child_prefix and entries:
        child_prefix = parent_prefix + "  "

    if found_index is not None:
        entry = entries[found_index]
        if value is not _DELETE:
            rendered = _format_json_value(value, _line_prefix(text, entry.key_start) or child_prefix)
            return text[: entry.value_start] + rendered + text[entry.value_end :]
        if len(entries) == 1:
            return text[: object_start + 1] + text[close_index:]
        if entry.comma_after is not None:
            end = entry.comma_after + 1
            while end < close_index and text[end] in " \t\r\n":
                end += 1
            return text[: entry.key_start] + text[end:]
        previous = entries[found_index - 1]
        if previous.comma_after is None:
            raise SyncError("invalid JSON comma structure")
        return text[: previous.comma_after] + text[entry.value_end :]

    if value is _DELETE:
        return text
    rendered_value = _format_json_value(value, child_prefix)
    rendered_property = f"{child_prefix}{json.dumps(key, ensure_ascii=False)}: {rendered_value}"
    if not entries:
        insertion = newline + rendered_property + newline + parent_prefix
        return text[: object_start + 1] + insertion + text[close_index:]

    whitespace_start = close_index
    while whitespace_start > entries[-1].value_end and text[whitespace_start - 1] in " \t\r\n":
        whitespace_start -= 1
    insertion = "," + newline + rendered_property
    return text[:whitespace_start] + insertion + text[whitespace_start:]


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(_: str) -> Any:
    raise NonStandardJsonConstant()


def _validate_json(text: str, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except DuplicateJsonKey as exc:
        raise SyncError(f"duplicate JSON key {exc.key!r} makes configuration ambiguous: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid JSON configuration syntax: {path}") from exc
    except NonStandardJsonConstant as exc:
        raise SyncError(f"JSON configuration contains a non-standard numeric constant: {path}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"JSON configuration root must be an object: {path}")
    return value


def _claude_entry_from_text(text: str, name: str, path: Path) -> dict[str, Any] | None:
    _validate_json(text, path)
    root_start = _skip_json_ws(text, 0)
    root_entries, _ = _json_object_entries(text, root_start)
    mcp_entry = next((entry for entry in root_entries if entry.key == "mcpServers"), None)
    if mcp_entry is None:
        return None
    if text[mcp_entry.value_start] != "{":
        raise SyncError(f"mcpServers must be an object: {path}")
    server_entries, _ = _json_object_entries(text, mcp_entry.value_start)
    server = next((entry for entry in server_entries if entry.key == name), None)
    if server is None:
        return None
    try:
        value = json.loads(text[server.value_start : server.value_end])
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid MCP entry in JSON configuration: {path}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"Claude MCP entry {name!r} must be an object")
    return value


def _patch_claude_entry(text: str, name: str, value: Any, path: Path) -> str:
    _validate_json(text, path)
    root_start = _skip_json_ws(text, 0)
    root_entries, _ = _json_object_entries(text, root_start)
    mcp_entry = next((entry for entry in root_entries if entry.key == "mcpServers"), None)
    if mcp_entry is None:
        if value is _DELETE:
            return text
        updated = _patch_json_object_property(text, root_start, "mcpServers", {name: value})
    else:
        if text[mcp_entry.value_start] != "{":
            raise SyncError(f"mcpServers must be an object: {path}")
        updated = _patch_json_object_property(text, mcp_entry.value_start, name, value)
    _validate_json(updated, path)
    return updated


def _load_claude_entry(path: Path, name: str) -> tuple[str, dict[str, Any] | None]:
    text = _read_text(path)
    if text is None:
        return "{}\n", None
    return text, _claude_entry_from_text(text, name, path)


def _validate_toml(text: str, path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SyncError(f"invalid TOML configuration syntax: {path}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"TOML configuration root must be a table: {path}")
    return value


def _load_codex_entry(path: Path, name: str) -> tuple[str, dict[str, Any] | None]:
    text = _read_text(path)
    if text is None:
        return "", None
    return text, _codex_entry_from_text(text, name, path)


def _codex_entry_from_text(text: str, name: str, path: Path) -> dict[str, Any] | None:
    root = _validate_toml(text, path)
    servers = root.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise SyncError(f"mcp_servers must be a table: {path}")
    value = servers.get(name)
    if value is not None and not isinstance(value, dict):
        raise SyncError(f"Codex MCP entry {name!r} must be a table")
    return value


def _parse_toml_key_path(content: str) -> list[str]:
    parts: list[str] = []
    index = 0
    while index < len(content):
        while index < len(content) and content[index] in " \t":
            index += 1
        if index >= len(content):
            break
        if content[index] in {'"', "'"}:
            quote = content[index]
            start = index
            index += 1
            while index < len(content):
                if quote == '"' and content[index] == "\\":
                    index += 2
                    continue
                if content[index] == quote:
                    index += 1
                    break
                index += 1
            else:
                raise SyncError("unterminated quoted TOML table key")
            token = content[start:index]
            try:
                parsed = tomllib.loads(f"{token} = 0")
            except tomllib.TOMLDecodeError as exc:
                raise SyncError("invalid quoted TOML table key") from exc
            parts.append(next(iter(parsed)))
        else:
            start = index
            while index < len(content) and content[index] not in ". \t":
                index += 1
            token = content[start:index]
            if not token:
                raise SyncError("invalid TOML table key")
            parts.append(token)
        while index < len(content) and content[index] in " \t":
            index += 1
        if index < len(content):
            if content[index] != ".":
                raise SyncError("invalid TOML dotted table key")
            index += 1
    return parts


def _toml_header_path(line: str) -> list[str] | None:
    stripped = line.lstrip(" \t")
    if not stripped.startswith("["):
        return None
    arrays = stripped.startswith("[[")
    open_len = 2 if arrays else 1
    index = open_len
    quote: str | None = None
    escaped = False
    while index < len(stripped):
        char = stripped[index]
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "]":
            close_len = 2 if arrays else 1
            if stripped[index : index + close_len] == "]" * close_len:
                return _parse_toml_key_path(stripped[open_len:index])
        index += 1
    return None


def _toml_table_starts(text: str) -> list[tuple[int, list[str]]]:
    """Locate real TOML table headers while ignoring strings, comments, and arrays."""
    starts: list[tuple[int, list[str]]] = []
    stack: list[str] = []
    state: str | None = None
    index = 0
    line_leading = True

    while index < len(text):
        char = text[index]

        if state in {"basic", "literal"}:
            if char in "\r\n":
                line_leading = True
                index += 1
                continue
            line_leading = False
            if state == "basic" and char == "\\":
                index += 2
                continue
            if (state == "basic" and char == '"') or (state == "literal" and char == "'"):
                state = None
            index += 1
            continue

        if state in {"multi_basic", "multi_literal"}:
            if char in "\r\n":
                line_leading = True
                index += 1
                continue
            line_leading = False
            quote = '"' if state == "multi_basic" else "'"
            if char == quote:
                run_end = index
                while run_end < len(text) and text[run_end] == quote:
                    run_end += 1
                escaped = False
                if state == "multi_basic":
                    slash = index - 1
                    slash_count = 0
                    while slash >= 0 and text[slash] == "\\":
                        slash_count += 1
                        slash -= 1
                    escaped = slash_count % 2 == 1
                if not escaped and run_end - index >= 3:
                    state = None
                    index = run_end
                    continue
            index += 1
            continue

        if char in "\r\n":
            line_leading = True
            index += 1
            continue
        if line_leading and char in " \t":
            index += 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            continue

        if line_leading and not stack and char == "[":
            newline = text.find("\n", index)
            line_end = len(text) if newline < 0 else newline
            header = _toml_header_path(text[index:line_end])
            if header is not None:
                starts.append((index, header))
                index = line_end
                line_leading = False
                continue

        line_leading = False
        if text.startswith('"""', index):
            state = "multi_basic"
            index += 3
            continue
        if text.startswith("'''", index):
            state = "multi_literal"
            index += 3
            continue
        if char == '"':
            state = "basic"
            index += 1
            continue
        if char == "'":
            state = "literal"
            index += 1
            continue
        if char in "[{":
            stack.append(char)
        elif char in "]}" and stack:
            expected = "[" if char == "]" else "{"
            if stack[-1] == expected:
                stack.pop()
        index += 1
    return starts


def _toml_key(key: str) -> str:
    return key if BARE_TOML_KEY_RE.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise SyncError("non-finite target-only TOML value cannot be rendered safely")
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise SyncError(f"target-only TOML value of type {type(value).__name__} cannot be rendered safely")


def _render_toml_table(path: list[str], values: dict[str, Any]) -> str:
    scalar_items = [(key, value) for key, value in values.items() if not isinstance(value, dict)]
    table_items = [(key, value) for key, value in values.items() if isinstance(value, dict)]
    lines = ["[" + ".".join(_toml_key(part) for part in path) + "]"]
    for key, value in scalar_items:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in table_items:
        if not value:
            lines.append(f"{_toml_key(key)} = {{}}")
    chunks = ["\n".join(lines)]
    for key, value in table_items:
        if value:
            chunks.append(_render_toml_table(path + [key], value))
    return "\n\n".join(chunks)


def _render_codex_block(name: str, entry: dict[str, Any]) -> str:
    return _render_toml_table(["mcp_servers", name], entry) + "\n"


def _toml_section_starts(
    text: str, headers: list[tuple[int, list[str]]]
) -> list[tuple[int, list[str]]]:
    adjusted: list[tuple[int, list[str]]] = []
    for index, (header_start, path) in enumerate(headers):
        start = text.rfind("\n", 0, header_start) + 1
        if index:
            while start > 0:
                previous_line_end = start
                search_end = start - 1 if text[start - 1] == "\n" else start
                previous_line_start = text.rfind("\n", 0, search_end) + 1
                previous_line = text[previous_line_start:previous_line_end]
                stripped = previous_line.strip()
                if stripped and not stripped.startswith("#"):
                    break
                start = previous_line_start
        adjusted.append((start, path))
    return adjusted


def _split_trailing_toml_trivia(section: str) -> tuple[str, str]:
    lines = section.splitlines(keepends=True)
    split_at = len(lines)
    while split_at:
        stripped = lines[split_at - 1].strip()
        if stripped and not stripped.startswith("#"):
            break
        split_at -= 1
    return "".join(lines[:split_at]), "".join(lines[split_at:])


def _patch_codex_entry(text: str, name: str, value: Any, path: Path) -> str:
    root = _validate_toml(text, path) if text.strip() else {}
    servers = root.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise SyncError(f"mcp_servers must be a table: {path}")
    existed = name in servers

    starts = _toml_section_starts(text, _toml_table_starts(text))
    sections: list[tuple[list[str], str]] = []
    preamble_end = starts[0][0] if starts else len(text)
    preamble = text[:preamble_end]
    for index, (start, header) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        sections.append((header, text[start:end]))

    matched = False
    inserted = False
    output = preamble
    for header, section in sections:
        is_target = len(header) >= 2 and header[0] == "mcp_servers" and header[1] == name
        if is_target:
            matched = True
            _, trailing_trivia = _split_trailing_toml_trivia(section)
            if value is not _DELETE and not inserted:
                if output and not output.endswith(("\n", "\r")):
                    output += "\n"
                if output and not output.endswith("\n\n"):
                    output += "\n"
                output += _render_codex_block(name, value)
                inserted = True
            output += trailing_trivia
            continue
        output += section

    if existed and not matched:
        raise SyncError(
            f"Codex MCP entry {name!r} uses an inline representation that cannot be patched safely"
        )
    if value is not _DELETE and not inserted:
        if output and not output.endswith(("\n", "\r")):
            output += "\n"
        if output and not output.endswith("\n\n"):
            output += "\n"
        output += _render_codex_block(name, value)
    if output.strip():
        _validate_toml(output, path)
    return output


def resolve_paths(args: argparse.Namespace) -> dict[str, Path]:
    if args.scope == "local":
        raise SyncError("Claude local MCP scope is intentionally unsupported because it is not portable")
    project_root = Path(args.project_root or Path.cwd()).expanduser().resolve()
    if args.scope == "global":
        claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        claude_default = Path(claude_dir).expanduser() / ".claude.json" if claude_dir else Path.home() / ".claude.json"
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        codex_default = codex_home / "config.toml"
    else:
        claude_default = project_root / ".mcp.json"
        codex_default = project_root / ".codex" / "config.toml"
    return {
        "claude": Path(args.claude_config).expanduser().resolve() if args.claude_config else claude_default,
        "codex": Path(args.codex_config).expanduser().resolve() if args.codex_config else codex_default,
    }


def load_native_entry(client: str, path: Path, name: str) -> tuple[str, dict[str, Any] | None]:
    return _load_claude_entry(path, name) if client == "claude" else _load_codex_entry(path, name)


def patch_native_entry(client: str, text: str, name: str, value: Any, path: Path) -> str:
    return (
        _patch_claude_entry(text, name, value, path)
        if client == "claude"
        else _patch_codex_entry(text, name, value, path)
    )


def native_entry_from_text(
    client: str, text: str, path: Path, name: str
) -> dict[str, Any] | None:
    return (
        _claude_entry_from_text(text, name, path)
        if client == "claude"
        else _codex_entry_from_text(text, name, path)
    )


def _native_root_from_text(client: str, text: str, path: Path) -> dict[str, Any]:
    if client == "claude":
        return _validate_json(text, path)
    return _validate_toml(text, path) if text.strip() else {}


def _root_without_servers(
    client: str, text: str, path: Path, names: Iterable[str]
) -> dict[str, Any]:
    root = copy.deepcopy(_native_root_from_text(client, text, path))
    container_name = "mcpServers" if client == "claude" else "mcp_servers"
    servers = root.get(container_name)
    if servers is None:
        return root
    if not isinstance(servers, dict):
        raise SyncError(f"{container_name} must be an object/table: {path}")
    for name in names:
        servers.pop(name, None)
    if not servers:
        root.pop(container_name, None)
    return root


def _validate_patch_scope(
    client: str,
    before: str,
    after: str,
    path: Path,
    names: Iterable[str],
) -> None:
    names = tuple(names)
    if _root_without_servers(client, before, path, names) != _root_without_servers(
        client, after, path, names
    ):
        raise SyncError("rendered target would modify configuration outside the named MCP entries")


def _assert_portable_entry(
    client: str,
    text: str,
    path: Path,
    name: str,
    expected: PortableServer,
) -> None:
    entry = native_entry_from_text(client, text, path, name)
    if entry is None:
        raise SyncError("rendered target is missing the named MCP entry")
    actual = parse_native_entry(client, entry, strict_source=False)
    differences = _portable_diff(expected, actual)
    if differences:
        raise SyncError(
            "rendered target failed pre-write consistency for fields: " + ", ".join(differences)
        )


def _assert_absent_entry(client: str, text: str, path: Path, name: str) -> None:
    if native_entry_from_text(client, text, path, name) is not None:
        raise SyncError("rendered target still contains an MCP entry that should be absent")


def parse_native_entry(client: str, entry: dict[str, Any], *, strict_source: bool) -> PortableServer:
    return (
        parse_claude_server(entry, strict_source=strict_source)
        if client == "claude"
        else parse_codex_server(entry, strict_source=strict_source)
    )


def _portable_diff(left: PortableServer, right: PortableServer) -> list[str]:
    left_map = asdict(left)
    right_map = asdict(right)
    left_map["headers"] = tuple((key.lower(), asdict(value)) for key, value in left.headers)
    right_map["headers"] = tuple((key.lower(), asdict(value)) for key, value in right.headers)
    return sorted(key for key in left_map if left_map[key] != right_map[key])


def check_pair(paths: dict[str, Path], name: str, scope: str, *, quiet: bool = False) -> bool:
    entries: dict[str, dict[str, Any] | None] = {}
    for client in ("claude", "codex"):
        _, entries[client] = load_native_entry(client, paths[client], name)
    if entries["claude"] is None and entries["codex"] is None:
        if not quiet:
            print(f"in-sync: {name} ({scope}) is absent from both clients")
        return True
    if entries["claude"] is None or entries["codex"] is None:
        missing = "claude" if entries["claude"] is None else "codex"
        if not quiet:
            print(f"drift: {name} ({scope}) is missing from {missing}")
        return False
    claude = parse_claude_server(entries["claude"], strict_source=False)
    codex = parse_codex_server(entries["codex"], strict_source=False)
    differences = _portable_diff(claude, codex)
    if differences:
        if not quiet:
            print(f"drift: {name} ({scope}); differing portable fields: {', '.join(differences)}")
        return False
    if not quiet:
        print(f"in-sync: {name} ({scope})")
    return True


def command_sync(args: argparse.Namespace) -> int:
    paths = resolve_paths(args)
    source = args.source_client
    target = "codex" if source == "claude" else "claude"
    _, source_entry = load_native_entry(source, paths[source], args.name)
    if source_entry is None:
        raise SyncError(
            f"source MCP {args.name!r} does not exist in {source} {args.scope} configuration; "
            "absence is never treated as deletion"
        )
    portable = parse_native_entry(source, source_entry, strict_source=True)
    target_text, target_entry = load_native_entry(target, paths[target], args.name)
    merged = merge_target_entry(target, target_entry, portable)
    updated = patch_native_entry(target, target_text, args.name, merged, paths[target])
    _validate_patch_scope(target, target_text, updated, paths[target], (args.name,))
    _assert_portable_entry(target, updated, paths[target], args.name, portable)
    changed = updated != target_text
    if args.dry_run:
        verb = "would update" if changed else "would keep"
        print(f"dry-run: {verb} {target} MCP {args.name} ({args.scope})")
        return 0
    if changed:
        _atomic_write(paths[target], updated)
    if not check_pair(paths, args.name, args.scope, quiet=True):
        raise SyncError("post-write portable consistency check failed")
    print(f"synced: {args.name} ({args.scope}) {source} -> {target}" + ("" if changed else " (already current)"))
    return 0


def command_delete(args: argparse.Namespace) -> int:
    paths = resolve_paths(args)
    source = args.source_client
    target = "codex" if source == "claude" else "claude"
    _, source_entry = load_native_entry(source, paths[source], args.name)
    if source_entry is not None:
        raise SyncError(
            f"source MCP {args.name!r} still exists in {source}; delete it there before propagating"
        )
    target_text, target_entry = load_native_entry(target, paths[target], args.name)
    if target_entry is None:
        print(f"delete: {target} MCP {args.name} ({args.scope}) is already absent")
        return 0
    updated = patch_native_entry(target, target_text, args.name, _DELETE, paths[target])
    _validate_patch_scope(target, target_text, updated, paths[target], (args.name,))
    _assert_absent_entry(target, updated, paths[target], args.name)
    if args.dry_run:
        print(f"dry-run: would delete {target} MCP {args.name} ({args.scope})")
        return 0
    _atomic_write(paths[target], updated)
    _, remaining = load_native_entry(target, paths[target], args.name)
    if remaining is not None or not check_pair(paths, args.name, args.scope, quiet=True):
        raise SyncError("post-write deletion check failed")
    print(f"deleted: {target} MCP {args.name} ({args.scope})")
    return 0


def command_rename(args: argparse.Namespace) -> int:
    if args.old_name == args.new_name:
        raise SyncError("old and new MCP names must differ")
    paths = resolve_paths(args)
    source = args.source_client
    target = "codex" if source == "claude" else "claude"
    _, source_old = load_native_entry(source, paths[source], args.old_name)
    if source_old is not None:
        raise SyncError(
            f"old source MCP {args.old_name!r} still exists in {source}; rename it there first"
        )
    _, source_new = load_native_entry(source, paths[source], args.new_name)
    if source_new is None:
        raise SyncError(f"renamed source MCP {args.new_name!r} does not exist in {source}")
    portable = parse_native_entry(source, source_new, strict_source=True)
    target_text, old_entry = load_native_entry(target, paths[target], args.old_name)
    _, new_entry = load_native_entry(target, paths[target], args.new_name)
    if old_entry is not None and new_entry is not None:
        raise SyncError("both old and new MCP names exist in target; refusing an ambiguous rename")
    base_entry = old_entry if old_entry is not None else new_entry
    merged = merge_target_entry(target, base_entry, portable)
    updated = patch_native_entry(target, target_text, args.old_name, _DELETE, paths[target])
    updated = patch_native_entry(target, updated, args.new_name, merged, paths[target])
    _validate_patch_scope(
        target, target_text, updated, paths[target], (args.old_name, args.new_name)
    )
    _assert_absent_entry(target, updated, paths[target], args.old_name)
    _assert_portable_entry(target, updated, paths[target], args.new_name, portable)
    if args.dry_run:
        print(f"dry-run: would rename {target} MCP {args.old_name} -> {args.new_name} ({args.scope})")
        return 0
    if updated != target_text:
        _atomic_write(paths[target], updated)
    _, old_after = load_native_entry(target, paths[target], args.old_name)
    if old_after is not None or not check_pair(paths, args.new_name, args.scope, quiet=True):
        raise SyncError("post-write rename consistency check failed")
    print(f"renamed: {target} MCP {args.old_name} -> {args.new_name} ({args.scope})")
    return 0


def command_check(args: argparse.Namespace) -> int:
    paths = resolve_paths(args)
    return 0 if check_pair(paths, args.name, args.scope) else 1


def _add_location_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", required=True, choices=("global", "project", "local"))
    parser.add_argument("--project-root", help="Project root; defaults to the current directory")
    parser.add_argument("--claude-config", help="Override the resolved Claude native config path")
    parser.add_argument("--codex-config", help="Override the resolved Codex native config path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize one portable MCP definition between Claude Code and Codex"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Add or update the target from an existing source entry")
    sync.add_argument("--from", dest="source_client", required=True, choices=("claude", "codex"))
    sync.add_argument("--name", required=True)
    sync.add_argument("--dry-run", action="store_true")
    _add_location_arguments(sync)
    sync.set_defaults(handler=command_sync)

    delete = subparsers.add_parser("delete", help="Explicitly delete a named entry from the other client")
    delete.add_argument("--from", dest="source_client", required=True, choices=("claude", "codex"))
    delete.add_argument("--name", required=True)
    delete.add_argument("--dry-run", action="store_true")
    _add_location_arguments(delete)
    delete.set_defaults(handler=command_delete)

    rename = subparsers.add_parser("rename", help="Atomically delete the old target name and add the new one")
    rename.add_argument("--from", dest="source_client", required=True, choices=("claude", "codex"))
    rename.add_argument("--old-name", required=True)
    rename.add_argument("--new-name", required=True)
    rename.add_argument("--dry-run", action="store_true")
    _add_location_arguments(rename)
    rename.set_defaults(handler=command_rename)

    check = subparsers.add_parser("check", help="Compare portable fields without modifying either config")
    check.add_argument("--name", required=True)
    _add_location_arguments(check)
    check.set_defaults(handler=command_check)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    names = [getattr(args, field, None) for field in ("name", "old_name", "new_name")]
    if any(name is not None and (not name or any(ord(char) < 0x20 for char in name)) for name in names):
        parser.error("MCP names must be non-empty and contain no control characters")
    try:
        return args.handler(args)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
