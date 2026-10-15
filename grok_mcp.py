#!/usr/bin/env python3
"""
grok_mcp.py - MCP server (stdio) that calls the Grok (xAI) Responses API.

Spawned by an MCP client over stdio; exposes one tool:
  grok_research - real-time research (X search and/or web search) via Grok

Protocol: POST https://api.x.ai/v1/responses (NOT /v1/chat/completions).
API details, models and pricing: see the xAI Responses API documentation.

API key: key name 'xai_api_key' in the kernel keyring, listed in
key_registry.py (the registry of keys used by this MCP server group).
If the key is missing the tool returns an error result; there is no
interactive prompt (an stdio server has no UI to talk to).

No third-party dependencies (Python standard library only).

Standalone smoke test (no API call, no key needed):
  printf '%s\\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | python3 grok_mcp.py
"""

import json
import re
import urllib.error
import urllib.request

import key_registry
from mcp_stdio import McpServer, keyring_get, keyring_missing_error

API_BASE = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.7"
KEY_NAME = key_registry.XAI_API_KEY
HTTP_TIMEOUT = 120  # seconds; search-heavy responses can be slow

MODELS = [
    "grok-4.7",                        # default: latest, $2.00/$6.00 per 1M tokens, 500k context
    "grok-4.6",                        # $2.00/$6.00 per 1M tokens, 500k context
    "grok-4.5",                        # $2.00/$6.00 per 1M tokens, 500k context
    "grok-4.3",                        # cheaper for bulk work, $1.25/$2.50 per 1M tokens, 1M context
    "grok-4.20-0309-reasoning",        # $1.25/$2.50 per 1M tokens, 1M context
    "grok-4.20-0309-non-reasoning",    # low latency, no reasoning, $1.25/$2.50, 1M context
    "grok-4.20-multi-agent-0309",      # multi-agent oriented, $1.25/$2.50, 1M context
]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_SYSTEM = (
    "あなたは最新情報の調査アシスタントです。"
    "提供された検索ツールを積極的に使い、最新の情報を収集して回答してください。"
    "情報源（URLや投稿者）を明示し、日本語で回答してください。"
)

server = McpServer(name="grok", version="1.0")


def _post(payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {API_BASE}/responses: {body[:500]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"request to {API_BASE}/responses failed: {e}") from None


@server.tool(
    "research",
    (
        "Real-time research via Grok (xAI). Use for anything that needs fresh, "
        "current information: latest news, software/library releases and version "
        "changes, reactions and discussion on X (Twitter), prices, events, or any "
        "fact your training data may not cover. Grok searches X and the web and "
        "returns a Japanese answer with sources (inline citations). "
        "Costs a few yen per call."
    ),
    schema={
        "query": {
            "type": "string",
            "description": "The question or research topic (Japanese or English)",
        },
        "tools": {
            "type": "string",
            "enum": ["both", "x", "web", "none"],
            "description": (
                "Search scope: both = X + web (default), x = X only, "
                "web = web only, none = no search (plain Grok answer, cheapest)"
            ),
        },
        "model": {
            "type": "string",
            "enum": MODELS,
            "description": f"Grok model (default {DEFAULT_MODEL})",
        },
        "from_date": {
            "type": "string",
            "description": "X search start date YYYY-MM-DD (optional)",
        },
        "to_date": {
            "type": "string",
            "description": "X search end date YYYY-MM-DD (optional)",
        },
        "system": {
            "type": "string",
            "description": "Optional system prompt override for Grok",
        },
    },
    required=("query",),
)
def research(args: dict):
    query = (args.get("query") or "").strip()
    if not query:
        return "Missing 'query'", True

    api_key = keyring_get(KEY_NAME)
    if api_key is None:
        return keyring_missing_error(KEY_NAME), True

    spec = (args.get("tools") or "both").strip()
    if spec not in ("both", "x", "web", "none"):
        return f"Invalid 'tools' value: {spec!r} (use one of both/x/web/none)", True

    for d in (args.get("from_date"), args.get("to_date")):
        if d and not DATE_RE.match(d):
            return f"Invalid date {d!r}: use YYYY-MM-DD", True

    tools: list[dict] = []
    if spec in ("x", "both"):
        x_tool: dict = {"type": "x_search"}
        if args.get("from_date"):
            x_tool["from_date"] = args["from_date"]
        if args.get("to_date"):
            x_tool["to_date"] = args["to_date"]
        tools.append(x_tool)
    if spec in ("web", "both"):
        tools.append({"type": "web_search"})

    system = (args.get("system") or "").strip() or DEFAULT_SYSTEM
    payload = {
        "model": args.get("model") or DEFAULT_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        "inline_citations": True,
    }
    if tools:
        payload["tools"] = tools

    try:
        data = _post(payload, api_key)
    except RuntimeError as e:
        return str(e), True

    texts = []
    for item in data.get("output", []) or []:
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text":
                    texts.append(part["text"])
    result = "\n".join(texts).strip()
    if not result:
        return "Grok returned an empty response", True

    usage = data.get("usage") or {}
    if usage:
        result += (
            f"\n\n[usage] input={usage.get('input_tokens', 0)} "
            f"output={usage.get('output_tokens', 0)} tokens"
        )
    return result, False


if __name__ == "__main__":
    server.run()
