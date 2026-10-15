#!/usr/bin/env python3
"""
x_mcp.py - MCP server (stdio) that calls the X (Twitter) API v2.

Spawned by an MCP client over stdio; exposes two tools:
  x_user_timeline - fetch a user's posts in a date range (markdown or json)
  x_usage         - current project usage (pay-per-use cost tracking)

user_timeline reads the account configured for the calling user (X_USERNAME,
declared as a customUserVar in the MCP client config) unless the model names
another one. An app-only Bearer cannot resolve "me", so some account must be
named either way.

Auth: app-only Bearer token from the kernel keyring, key name
'x_bearer_token', listed in key_registry.py (the registry of keys used by
this MCP server group). If the key is missing the tools return an error
result; there is no interactive prompt.

App-only Bearer covers: /2/users/by/username/:username, /2/users/:id/tweets,
/2/usage/tweets. Endpoints needing OAuth 2.0 user context (/users/me, posting,
bookmarks, liked tweets) are NOT available here.
API details and pricing: see the X API v2 documentation.

No third-party dependencies (Python standard library only).

Standalone smoke test (no API call, no key needed):
  printf '%s\\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | python3 x_mcp.py
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import key_registry
from mcp_stdio import McpServer, keyring_get, keyring_missing_error, setting

API_BASE = "https://api.twitter.com/2"
KEY_NAME = key_registry.X_BEARER_TOKEN
DEFAULT_USER = setting("X_USERNAME")

TWEET_FIELDS = "created_at,public_metrics,lang,entities,referenced_tweets,note_tweet"
EXPANSIONS = "referenced_tweets.id,referenced_tweets.id.author_id"
USER_FIELDS = "username"

HTTP_TIMEOUT = 30           # seconds per request
MAX_RATE_LIMIT_WAIT = 90    # cap on total 429 retry sleep (the MCP client has its own timeout)
MAX_TWEETS_CAP = 500

server = McpServer(name="x", version="1.0")


# --- X API v2 plumbing ---------------------------------------------------------

def _api_get(path: str, token: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    deadline = time.time() + MAX_RATE_LIMIT_WAIT
    while True:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset = int(e.headers.get("x-rate-limit-reset") or 0)
                wait = max(reset - int(time.time()), 1) if reset else 60
                if time.time() + wait > deadline:
                    raise RuntimeError(
                        f"rate limited: reset in ~{wait}s (giving up after {MAX_RATE_LIMIT_WAIT}s); try later"
                    ) from None
                print(f"[rate limited] sleeping {wait}s", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code} on {path}: {body[:500]}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"request to {path} failed: {e}") from None


def _resolve_user_id(username: str, token: str) -> str:
    data = _api_get(f"/users/by/username/{urllib.parse.quote(username)}", token)
    if "data" not in data or data["data"] is None:
        raise RuntimeError(f"user lookup failed for @{username}: {json.dumps(data)[:300]}")
    return data["data"]["id"]


def _iter_user_tweets(user_id: str, token: str, *, start_time: str | None = None,
                      end_time: str | None = None, exclude: list[str] | None = None,
                      page_size: int = 100):
    """Yield tweet objects; referenced tweets are attached as ref['resolved']."""
    params: dict = {
        "max_results": str(page_size),
        "tweet.fields": TWEET_FIELDS,
        "expansions": EXPANSIONS,
        "user.fields": USER_FIELDS,
    }
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    if exclude:
        params["exclude"] = ",".join(exclude)

    while True:
        data = _api_get(f"/users/{user_id}/tweets", token, params)
        ref_index = {t["id"]: t for t in data.get("includes", {}).get("tweets", [])}
        for tweet in data.get("data", []):
            for ref in tweet.get("referenced_tweets", []):
                if ref["id"] in ref_index:
                    ref["resolved"] = ref_index[ref["id"]]
            yield tweet
        next_token = data.get("meta", {}).get("next_token")
        if not next_token:
            return
        params["pagination_token"] = next_token


def _to_iso8601(date_str: str) -> str:
    """Accept YYYY-MM-DD or full ISO8601; return ISO8601 with Z."""
    if "T" in date_str:
        return date_str
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_markdown(tweets: list[dict], username: str) -> str:
    lines = [f"# @{username} のツイート（{len(tweets)}件）\n"]
    for t in tweets:
        created = t.get("created_at", "")
        tid = t.get("id", "")
        # long posts (>280 chars): full text is in note_tweet.text
        text = t.get("note_tweet", {}).get("text") or t.get("text", "")
        m = t.get("public_metrics", {})
        url = f"https://x.com/{username}/status/{tid}"
        lines.append(f"## {created}  [{tid}]({url})")
        lines.append(
            f"- like:{m.get('like_count', 0)} rt:{m.get('retweet_count', 0)} "
            f"reply:{m.get('reply_count', 0)} quote:{m.get('quote_count', 0)} "
            f"impr:{m.get('impression_count', 0)}"
        )
        for ref in t.get("referenced_tweets", []):
            kind = ref.get("type")
            resolved = ref.get("resolved", {})
            ref_text = resolved.get("text", "(取得不可)")
            lines.append(f"- {kind}: {ref_text[:200]}")
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


# --- tools -----------------------------------------------------------------------

@server.tool(
    "user_timeline",
    (
        "Fetch recent X (Twitter) posts of a user via X API v2. Use for the "
        "user's own posts (e.g. mining article topics from what they posted "
        "recently, checking what they said about something) or for any public "
        "account. Returns posts as readable Markdown by default (full text, "
        "metrics, links, quoted/replied-to context), or raw JSON on request. "
        "Retweets and replies-to-others are excluded by default. "
        "Costs a little per post (pay-per-use; own-account reads are cheaper); "
        "check the monthly total with the 'usage' tool."
    ),
    schema={
        "username": {
            "type": "string",
            "description": (
                "X username without @. Omit to read the account configured for "
                "this user; set it to read someone else's public timeline."
            ),
        },
        "since": {
            "type": "string",
            "description": "Start date YYYY-MM-DD or ISO8601, inclusive (optional)",
        },
        "until": {
            "type": "string",
            "description": "End date YYYY-MM-DD or ISO8601 (optional)",
        },
        "max": {
            "type": "integer",
            "description": "Max posts to return (default 50, cap 500)",
        },
        "include_retweets": {
            "type": "boolean",
            "description": "Include retweets (default false)",
        },
        "include_replies": {
            "type": "boolean",
            "description": "Include replies to other people (default false)",
        },
        "format": {
            "type": "string",
            "enum": ["markdown", "json"],
            "description": "Output format (default markdown)",
        },
    },
)
def user_timeline(args: dict):
    username = (args.get("username") or DEFAULT_USER or "").strip().lstrip("@")
    if not username:
        return (
            "No username given and none configured for this user: set X_USERNAME "
            "in the MCP server settings, or pass 'username' to read a public account",
            True,
        )

    token = keyring_get(KEY_NAME)
    if token is None:
        return keyring_missing_error(KEY_NAME), True

    try:
        max_n = int(args.get("max", 50))
    except (TypeError, ValueError):
        return "Invalid 'max': expected an integer", True
    max_n = max(1, min(max_n, MAX_TWEETS_CAP))

    fmt = (args.get("format") or "markdown").strip()
    if fmt not in ("markdown", "json"):
        return "Invalid 'format': use 'markdown' or 'json'", True

    since = (args.get("since") or "").strip() or None
    until = (args.get("until") or "").strip() or None
    try:
        start_time = _to_iso8601(since) if since else None
        end_time = _to_iso8601(until) if until else None
    except ValueError:
        return "Invalid 'since'/'until': use YYYY-MM-DD or ISO8601", True

    exclude = []
    if not args.get("include_retweets"):
        exclude.append("retweets")
    if not args.get("include_replies"):
        exclude.append("replies")

    try:
        user_id = _resolve_user_id(username, token)
        tweets = []
        for t in _iter_user_tweets(
            user_id, token,
            start_time=start_time, end_time=end_time,
            exclude=exclude,
        ):
            tweets.append(t)
            if len(tweets) >= max_n:
                break
    except RuntimeError as e:
        return str(e), True

    if not tweets:
        range_txt = f" since {since or 'beginning of time'}" + (f" until {until}" if until else "")
        return f"No posts found for @{username}{range_txt}.", False

    if fmt == "json":
        out = json.dumps(
            {"username": username, "user_id": user_id, "count": len(tweets), "tweets": tweets},
            ensure_ascii=False,
        )
    else:
        out = _format_markdown(tweets, username)
    return out, False


@server.tool(
    "usage",
    (
        "Show the current X API project usage (pay-per-use cost tracking). "
        "Call this to check how much the X API has been used this month, e.g. "
        "after fetching many posts with user_timeline."
    ),
    schema={},
)
def usage(args: dict):
    token = keyring_get(KEY_NAME)
    if token is None:
        return keyring_missing_error(KEY_NAME), True
    try:
        data = _api_get("/usage/tweets", token)
    except RuntimeError as e:
        return str(e), True
    return json.dumps(data.get("data", {}), ensure_ascii=False), False


if __name__ == "__main__":
    server.run()
