#!/usr/bin/env python3
"""
mcp_stdio.py - Minimal MCP (Model Context Protocol) server framework over stdio.

JSON-RPC 2.0, one message per line (NDJSON), Python standard library only.
Designed to be spawned by an MCP client over stdio (LibreChat and similar).
Also runnable standalone by piping JSON-RPC into stdin.

Notes on client behaviour (clients differ in how they manage the child):
  - the client sends its newest protocolVersion in `initialize` and rejects a
    server version it does not support
  - some clients spawn the child once to list tools, close it, then respawn it
    on demand for each tool call; LibreChat keeps one connection per user for
    the session
  - `tools/call` results are normalized: text parts of result.content[] are
    concatenated, and isError: true is surfaced to the model as a tool error
  - the child's stderr is drained into the client's log
  - the `instructions` field of initialize is not reliably injected into the
    system prompt, so tool descriptions must carry all the context the model
    needs

Usage:

    from mcp_stdio import McpServer, keyring_get, keyring_missing_error

    server = McpServer(name="myservice", version="1.0")

    @server.tool(
        "mytool",
        "What it does and WHEN to call it (the model sees this, nothing else).",
        schema={
            "arg1": {"type": "string", "description": "..."},
        },
        required=("arg1",),
    )
    def mytool(args):
        return "result text", False      # (text, is_error)

    server.run()

Settings are read with setting(), which accepts a --NAME=VALUE (or --NAME VALUE)
command line argument and the $NAME environment variable, the argument winning,
so the caller of the server decides which channel to use:

    from mcp_stdio import setting
    root = setting("MEMORY_ROOT")   # --memory-root=... or $MEMORY_ROOT

Credentials come from the kernel keyring, accessed via key_registry.py, the
registry of keys used by this MCP server group. Only key names listed in the
registry can be read.
keyring_get() never prompts: an stdio MCP server has no UI to talk to, so a
missing key is reported as a tool error (keyring_missing_error()).

No third-party dependencies.
"""

import json
import os
import sys
import traceback

import key_registry

PROTOCOL_VERSION = "2024-11-05"


# --- settings (command line argument or environment variable) ------------------
# Every configurable value is accepted through both channels so the caller
# decides which one to use in its config: arguments are visible in `ps`
# (/proc/<pid>/cmdline) while environment variables are readable only by the
# same UID and root. The argument wins when both are given.

def _parse_cli_args(argv: list[str]) -> dict[str, str]:
    """Collect --NAME=VALUE and --NAME VALUE pairs, keyed by NAME uppercased
    with dashes mapped to underscores, so --mail-to matches MAIL_TO."""
    settings: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            i += 1
            continue
        body = arg[2:]
        if "=" in body:
            name, value = body.split("=", 1)
        elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            name, value = body, argv[i + 1]
            i += 1
        else:
            i += 1
            continue
        settings[name.upper().replace("-", "_")] = value
        i += 1
    return settings


_CLI_SETTINGS = _parse_cli_args(sys.argv[1:])


def setting(name: str, default: str | None = None) -> str | None:
    """Read one setting: the --NAME argument first, then $NAME, then default.

    An empty value (an unresolved placeholder, for instance) counts as unset.
    """
    value = (_CLI_SETTINGS.get(name) or os.environ.get(name) or "").strip()
    return value or default


# --- kernel keyring ------------------------------------------------------------
# Credential access is centralized in key_registry.py: only key names listed
# in its REGISTRY can be read (read() refuses anything else).

def keyring_get(key_name: str) -> str | None:
    """Read a user key from the kernel keyring (@u). See key_registry.read().

    Returns the key value, or None if the key is not present in the keyring,
    the name is not in the key registry, or keyctl is unavailable.
    Never blocks on user input.
    """
    try:
        return key_registry.read(key_name)
    except KeyError:
        return None


def keyring_missing_error(key_name: str) -> str:
    """Tool-error text for a key that is not in the keyring."""
    return key_registry.missing_error(key_name)


# --- MCP server -----------------------------------------------------------------

class McpServer:
    """A tiny stdio MCP server: initialize / tools/list / tools/call / ping."""

    def __init__(self, name: str, version: str = "1.0"):
        self.name = name
        self.version = version
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def tool(self, name: str, description: str, schema: dict | None = None,
             required: tuple[str, ...] = ()):
        """Register a tool. Handler signature: fn(args: dict) -> (text, is_error)."""
        def decorator(fn):
            input_schema: dict = {"type": "object", "properties": dict(schema or {})}
            if required:
                input_schema["required"] = list(required)
            self.tools.append({
                "name": name,
                "description": description,
                "inputSchema": input_schema,
            })
            self._handlers[name] = fn
            return fn
        return decorator

    # -- JSON-RPC dispatch -------------------------------------------------------

    def _handle(self, request: dict) -> dict | None:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}
        if req_id is None:  # notification: no response allowed
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self.tools}}
        if method == "tools/call":
            return self._call_tool(params, req_id)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _call_tool(self, params: dict, req_id) -> dict:
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        fn = self._handlers.get(tool_name)
        if fn is None:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"},
            }
        try:
            out = fn(arguments)
            if isinstance(out, tuple):
                text, is_error = out
            else:
                text, is_error = str(out), False
            text = str(text)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            text, is_error = "Internal error (see server log)", True
        result: dict = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    # -- main loop -----------------------------------------------------------------

    def run(self) -> None:
        sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1, encoding="utf-8")
        sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1, encoding="utf-8")
        sys.stderr.write(f"{self.name}: MCP stdio server started (pid {os.getpid()})\n")

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(request, dict):
                continue
            response = self._handle(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    # standalone smoke test: serves a single `echo` tool
    demo = McpServer(name="mcp-stdio-demo", version="1.0")

    @demo.tool(
        "echo",
        "Echo back the input message.",
        schema={"message": {"type": "string", "description": "Message to echo"}},
        required=("message",),
    )
    def echo(args):
        return f"echo: {args.get('message', '')}", False

    demo.run()
