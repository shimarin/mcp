#!/usr/bin/env python3
"""
exa_mcp.py - MCP server (stdio) that calls the Exa API for web search and page
contents.

Spawned by an MCP client over stdio; exposes two tools:
  web_search - search the web (POST /search), results with LLM picks
  web_fetch  - read one page as markdown-style text (POST /contents)

Auth: API key from the kernel keyring, key name 'exa_api_key', listed in
key_registry.py. If the key is missing the tools return an error result; there
is no interactive prompt.

Configuration (environment variable, or the matching --exa-user-id=VALUE
command line argument; the argument wins):
  EXA_USER_ID  opaque id of the end user this request is made for, sent to Exa
               as userIdentifier so usage is attributed per user instead of to
               the whole team. A LibreChat user id ({{LIBRECHAT_USER_ID}}) fits:
               1-64 characters of letters, digits, '_' and '-'. Never pass a
               name, username or email address here.

api.exa.ai sits behind Cloudflare, which rejects the default Python-urllib
User-Agent with 403 (Error 1010), so a custom User-Agent is always sent.

No third-party dependencies (Python standard library only).

Standalone smoke test (no key needed for tools/list):
  printf '%s\\n' \\
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \\
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \\
    | python3 exa_mcp.py
"""

import json
import re
import urllib.error
import urllib.request

import key_registry
from mcp_stdio import McpServer, keyring_get, keyring_missing_error, setting

API_BASE = "https://api.exa.ai"
USER_AGENT = "mcp-servers/1.0"
KEY_NAME = key_registry.EXA_API_KEY
USER_IDENTIFIER = setting("EXA_USER_ID")
HTTP_TIMEOUT = 60  # seconds; deep search types are slow

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SEARCH_TYPES = ["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"]
CATEGORIES = ["news", "publication", "company", "people", "personal site", "financial report"]

server = McpServer(name="exa", version="1.0")


def _post(path: str, payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body).get("error") or body[:300]
        except json.JSONDecodeError:
            err = body[:300]
        raise RuntimeError(f"HTTP {e.code} from {API_BASE}{path}: {err}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"request to {API_BASE}{path} failed: {e}") from None


def _api_key_or_error() -> str | None:
    return keyring_get(KEY_NAME)


@server.tool(
    "web_search",
    (
        "Search the web with Exa. Use for current events, software releases and "
        "version changes, company and people lookups, or any fact that needs a "
        "live source. Returns ranked pages with title, URL, published date and "
        "the passages Exa judged most relevant. Set full_text to get whole page "
        "text instead of passages. Costs a little per call."
    ),
    schema={
        "query": {
            "type": "string",
            "description": "What to find, as a natural-language query",
        },
        "num_results": {
            "type": "integer",
            "description": "How many pages to return (default 8, max 100)",
        },
        "type": {
            "type": "string",
            "enum": SEARCH_TYPES,
            "description": (
                "Search mode: auto (default, balanced), fast/instant (quicker, "
                "shallower), deep-lite/deep/deep-reasoning (multi-step research, "
                "slower and pricier)"
            ),
        },
        "category": {
            "type": "string",
            "enum": CATEGORIES,
            "description": "Focus on a kind of page (optional). news and publication "
                           "do not work with company/people dates filters",
        },
        "include_domains": {
            "type": "string",
            "description": "Comma-separated hostnames to restrict results to, "
                           "e.g. 'gentoo.org,wiki.gentoo.org' (optional)",
        },
        "exclude_domains": {
            "type": "string",
            "description": "Comma-separated hostnames to drop from results (optional)",
        },
        "start_published_date": {
            "type": "string",
            "description": "Only pages published after this date, YYYY-MM-DD (optional)",
        },
        "end_published_date": {
            "type": "string",
            "description": "Only pages published before this date, YYYY-MM-DD (optional)",
        },
        "full_text": {
            "type": "boolean",
            "description": "Return full page text instead of relevant passages (default false)",
        },
        "max_characters": {
            "type": "integer",
            "description": "Character budget per page for text or passages (default 4000)",
        },
    },
    required=("query",),
)
def web_search(args: dict):
    query = (args.get("query") or "").strip()
    if not query:
        return "Missing 'query'", True

    api_key = _api_key_or_error()
    if api_key is None:
        return keyring_missing_error(KEY_NAME), True

    search_type = (args.get("type") or "auto").strip()
    if search_type not in SEARCH_TYPES:
        return f"Invalid 'type' {search_type!r}: use one of {', '.join(SEARCH_TYPES)}", True

    category = (args.get("category") or "").strip()
    if category and category not in CATEGORIES:
        return f"Invalid 'category' {category!r}: use one of {', '.join(CATEGORIES)}", True

    for name in ("start_published_date", "end_published_date"):
        value = (args.get(name) or "").strip()
        if value and not DATE_RE.match(value):
            return f"Invalid {name} {value!r}: use YYYY-MM-DD", True

    try:
        num_results = int(args.get("num_results", 8))
    except (TypeError, ValueError):
        return "Invalid 'num_results': expected an integer", True
    num_results = max(1, min(num_results, 100))

    try:
        max_chars = int(args.get("max_characters", 4000))
    except (TypeError, ValueError):
        return "Invalid 'max_characters': expected an integer", True
    max_chars = max(200, min(max_chars, 100000))

    payload: dict = {
        "query": query,
        "type": search_type,
        "numResults": num_results,
        "contents": {
            "text": {"maxCharacters": max_chars} if args.get("full_text") else
                    {"highlights": {"maxCharacters": max_chars}},
        },
    }
    if category:
        payload["category"] = category
    for field, name in (("includeDomains", "include_domains"), ("excludeDomains", "exclude_domains")):
        domains = [d.strip() for d in (args.get(name) or "").split(",") if d.strip()]
        if domains:
            payload[field] = domains
    for field, name in (("startPublishedDate", "start_published_date"),
                        ("endPublishedDate", "end_published_date")):
        value = (args.get(name) or "").strip()
        if value:
            payload[field] = value
    if USER_IDENTIFIER and USER_ID_RE.match(USER_IDENTIFIER):
        payload["userIdentifier"] = USER_IDENTIFIER

    try:
        data = _post("/search", payload, api_key)
    except RuntimeError as e:
        return str(e), True

    results = data.get("results") or []
    if not results:
        return f"No results for {query!r}", True

    lines = [f"Exa search ({search_type}, {len(results)} results) for {query!r}:", ""]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "(untitled)").strip()
        url = (r.get("url") or "").strip()
        meta = []
        if r.get("publishedDate"):
            meta.append(str(r["publishedDate"])[:10])
        if r.get("author"):
            meta.append(str(r["author"]).strip())
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if meta:
            lines.append(f"   {' · '.join(meta)}")
        if r.get("highlights"):
            for h in r["highlights"]:
                lines.append(f"   > {(h or '').strip()}")
        text = (r.get("text") or "").strip()
        if text:
            lines.append(f"   {text[:max_chars]}")
        lines.append("")

    cost = (data.get("costDollars") or {}).get("total")
    if cost:
        lines.append(f"[cost] ${cost}")
    return "\n".join(lines).strip(), False


@server.tool(
    "web_fetch",
    (
        "Read one web page as markdown-style text via Exa /contents. Use after "
        "web_search to read a specific page in full, or whenever the user gives "
        "a URL. Returns the page title and its main text; navigation and "
        "boilerplate are mostly stripped. Costs a little per page."
    ),
    schema={
        "url": {"type": "string", "description": "Page URL to read"},
        "max_characters": {
            "type": "integer",
            "description": "Character limit for the page text (default 10000, max 1000000)",
        },
        "max_age_hours": {
            "type": "integer",
            "description": "Accept cached content up to this many hours old; 0 always "
                           "fetches fresh, -1 always uses the cache (optional)",
        },
    },
    required=("url",),
)
def web_fetch(args: dict):
    url = (args.get("url") or "").strip()
    if not url:
        return "Missing 'url'", True
    if not url.lower().startswith(("http://", "https://")):
        return f"Invalid 'url' {url!r}: expected an http(s) URL", True

    api_key = _api_key_or_error()
    if api_key is None:
        return keyring_missing_error(KEY_NAME), True

    try:
        max_chars = int(args.get("max_characters", 10000))
    except (TypeError, ValueError):
        return "Invalid 'max_characters': expected an integer", True
    max_chars = max(200, min(max_chars, 1000000))

    payload: dict = {"ids": [url], "text": {"maxCharacters": max_chars}}
    if args.get("max_age_hours") is not None:
        try:
            max_age = int(args["max_age_hours"])
        except (TypeError, ValueError):
            return "Invalid 'max_age_hours': expected an integer", True
        if not -1 <= max_age <= 720:
            return "Invalid 'max_age_hours': use -1 to 720", True
        payload["maxAgeHours"] = max_age
    if USER_IDENTIFIER and USER_ID_RE.match(USER_IDENTIFIER):
        payload["userIdentifier"] = USER_IDENTIFIER

    try:
        data = _post("/contents", payload, api_key)
    except RuntimeError as e:
        return str(e), True

    for status in data.get("statuses") or []:
        if status.get("status") == "error":
            err = status.get("error") or {}
            return (
                f"Exa could not read {url}: {err.get('tag', 'unknown')} "
                f"(HTTP {err.get('httpStatusCode', '?')})",
                True,
            )

    for result in data.get("results") or []:
        text = (result.get("text") or "").strip()
        if not text:
            continue
        title = (result.get("title") or "").strip()
        header = f"# {title}\n{url}\n" if title else url
        return f"{header}\n{text[:max_chars]}", False

    return f"Exa returned no page text for {url}", True


if __name__ == "__main__":
    server.run()
