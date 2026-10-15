#!/usr/bin/env python3
"""
memory_mcp.py - A persistent-memory MCP server over stdio (JSON-RPC 2.0,
line-delimited). Lets a local LLM store and later recall curated memories
when the user says "remember this".

Spawned by an MCP client over stdio; exposes five tools:
  memory_save   - persist a memory (content, optional title/tags) -> returns id
  memory_recall - keyword search over title/tags/body, returns id + snippets
  memory_list   - list memories (id, created, title), newest first
  memory_read   - read one memory in full by id
  memory_delete - delete one memory by id

Design:
  - NOT a passive log: the tool cannot see the conversation, so the model
    must call save proactively and pass a self-contained summary.
  - Single-user model: the namespace is fixed at spawn time (env MEMORY_KEY or
    --memory-key=VALUE). It is a namespace selector, not an auth boundary, so a
    server shared by several users needs one namespace per user (in LibreChat:
    MEMORY_KEY={{LIBRECHAT_USER_ID}}); inside one process there is no
    per-caller access control and no cross-namespace access.
  - Storage is plain files, kept outside any notes folder that gets synced or
    shared: one memory == one Markdown file with frontmatter under
    <MEMORY_ROOT>/<namespace>/<id>.md
    (root: env MEMORY_ROOT or --memory-root=VALUE, else
    ~/.local/share/memory_mcp)

No third-party dependencies (Python standard library only).

Standalone test (isolated storage):
  printf '%s\\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"save","arguments":{"content":"覚える内容","title":"題","tags":"a,b"}}}' \
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"recall","arguments":{"query":"覚える"}}}' \
    | MEMORY_ROOT=/tmp/memtest MEMORY_KEY=test python3 memory_mcp.py
"""

import os
import re
import sys
import time
import random

from mcp_stdio import McpServer, setting

# --- configuration -------------------------------------------------------------

def _default_root() -> str:
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(xdg, "memory_mcp")

ROOT = os.path.realpath(setting("MEMORY_ROOT") or _default_root())

def _sanitize_component(s: str, fallback: str) -> str:
    """Reduce an arbitrary string to a safe single path component."""
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s)
    s = s.strip("._")
    return s or fallback

NAMESPACE = _sanitize_component(setting("MEMORY_KEY", "default"), "default")

if setting("MEMORY_KEY") is None:
    sys.stderr.write(
        "memory: MEMORY_KEY is not set; using the shared namespace 'default'. "
        "When several users share one server, set it per user "
        "(e.g. MEMORY_KEY={{LIBRECHAT_USER_ID}}).\n"
    )

MAX_READ_BYTES = 256 * 1024

server = McpServer(name="memory", version="1.0")

# --- storage ---------------------------------------------------------------------

def _ns_dir() -> str:
    d = os.path.join(ROOT, NAMESPACE)
    os.makedirs(d, exist_ok=True)
    return d

def _id_path(mem_id: str) -> str | None:
    """Resolve a memory id to its file path iff it stays inside the namespace dir."""
    safe = _sanitize_component(mem_id, "")
    if not safe:
        return None
    d = _ns_dir()
    p = os.path.realpath(os.path.join(d, safe + ".md"))
    if p == os.path.join(d, safe + ".md") and (p == d or p.startswith(os.path.realpath(d) + os.sep)):
        return p
    return None

def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + "%04x" % random.randrange(0x10000)

def _normalize_tags(tags) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        parts = re.split(r"[,\s]+", tags.strip())
    elif isinstance(tags, list):
        parts = [str(t) for t in tags]
    else:
        parts = [str(tags)]
    return [t.strip() for t in parts if t and t.strip()]

def _write_memory(mem_id, created, title, tags, content) -> str:
    path = _id_path(mem_id)
    fm = ["---", f"id: {mem_id}", f"created: {created}"]
    if title:
        fm.append(f"title: {title}")
    if tags:
        fm.append("tags: " + ", ".join(tags))
    fm.append("---")
    body = fm[:] + ["", content.rstrip("\n"), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    return path

def _parse_memory(path: str) -> dict:
    """Return {id, created, title, tags, body}. Tolerant of hand-edited files."""
    meta = {"id": os.path.splitext(os.path.basename(path))[0], "created": "", "title": "", "tags": [], "body": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_READ_BYTES + 1)
    except OSError:
        return meta
    lines = text.split("\n")
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
            m = re.match(r"([A-Za-z_]+):\s*(.*)", lines[i])
            if m:
                k, v = m.group(1).lower(), m.group(2).strip()
                if k == "tags":
                    meta["tags"] = _normalize_tags(v)
                elif k in meta:
                    meta[k] = v
    meta["body"] = "\n".join(lines[body_start:]).strip()
    return meta

def _all_memories() -> list[dict]:
    d = _ns_dir()
    out = []
    for name in os.listdir(d):
        if name.endswith(".md"):
            out.append(_parse_memory(os.path.join(d, name)))
    out.sort(key=lambda m: m["id"], reverse=True)  # id is timestamp-prefixed
    return out

# --- tools ------------------------------------------------------------------------

@server.tool(
    "save",
    (
        "Save a memory for later recall. Call this when the user asks to "
        "remember something. Summarize what should be remembered into "
        "'content' (the raw conversation is not visible to this tool). "
        "Returns the new memory id."
    ),
    schema={
        "content": {"type": "string", "description": "The text to remember (a self-contained summary)"},
        "title": {"type": "string", "description": "Short title for the memory (optional)"},
        "tags": {"type": "string", "description": "Comma-separated tags (optional)"},
    },
    required=("content",),
)
def save(args: dict):
    content = (args.get("content") or "").strip()
    if not content:
        return "Missing 'content'", True
    title = (args.get("title") or "").strip()
    tags = _normalize_tags(args.get("tags"))
    mem_id = _new_id()
    created = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        _write_memory(mem_id, created, title, tags, content)
    except OSError as e:
        return f"Save failed: {e}", True
    return ("Saved memory '" + mem_id + "'" + (f" (title: {title})" if title else "")), False


@server.tool(
    "recall",
    (
        "Search saved memories by title, tags and body text. Use this to "
        "recall what the user asked you to remember earlier. Returns matching "
        "memory ids with snippets; use 'read' for the full text."
    ),
    schema={
        "query": {"type": "string", "description": "Words to search for"},
        "max_results": {"type": "integer", "description": "Max memories to return (default 10)"},
    },
    required=("query",),
)
def recall(args: dict):
    query = (args.get("query") or "").strip()
    if not query:
        return "Missing 'query'", True
    try:
        max_results = int(args.get("max_results", 10))
    except (TypeError, ValueError):
        return "Invalid 'max_results': expected an integer", True
    terms = [t.lower() for t in re.split(r"\s+", query) if t]

    scored = []
    for m in _all_memories():
        hay = (m["title"] + "\n" + " ".join(m["tags"]) + "\n" + m["body"]).lower()
        score = sum(hay.count(t) for t in terms)
        matched = sum(1 for t in terms if t in hay)
        if matched:
            scored.append((matched, score, m))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    if not scored:
        return f"No memories match {query!r}", False

    out = [f"{len(scored)} memory(ies) matched (showing up to {max_results}):"]
    for _, _, m in scored[:max_results]:
        head = f"- [{m['id']}]"
        if m["title"]:
            head += f" {m['title']}"
        if m["tags"]:
            head += f"  ({', '.join(m['tags'])})"
        out.append(head)
        snippet = " ".join(m["body"].split())
        if len(snippet) > 200:
            snippet = snippet[:200] + " …"
        out.append(f"    {snippet}")
    return "\n".join(out), False


@server.tool(
    "list",
    "List saved memories (id, created time, title), newest first.",
    schema={},
)
def list(args: dict):
    mems = _all_memories()
    if not mems:
        return "(no memories saved yet)", False
    out = [f"{len(mems)} memory(ies):"]
    for m in mems:
        line = f"- [{m['id']}]"
        if m["created"]:
            line += f" {m['created']}"
        if m["title"]:
            line += f" — {m['title']}"
        out.append(line)
    return "\n".join(out), False


@server.tool(
    "read",
    "Read one saved memory in full, by its id.",
    schema={
        "id": {"type": "string", "description": "Memory id (from list/recall)"},
    },
    required=("id",),
)
def read(args: dict):
    mem_id = (args.get("id") or "").strip()
    path = _id_path(mem_id)
    if path is None:
        return f"Invalid id: {mem_id!r}", True
    if not os.path.isfile(path):
        return f"Memory not found: {mem_id!r}", True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(MAX_READ_BYTES), False
    except OSError as e:
        return f"Read error: {e}", True


@server.tool(
    "delete",
    "Delete one saved memory by its id.",
    schema={
        "id": {"type": "string", "description": "Memory id (from list/recall)"},
    },
    required=("id",),
)
def delete(args: dict):
    mem_id = (args.get("id") or "").strip()
    path = _id_path(mem_id)
    if path is None:
        return f"Invalid id: {mem_id!r}", True
    if not os.path.isfile(path):
        return f"Memory not found: {mem_id!r}", True
    try:
        os.remove(path)
    except OSError as e:
        return f"Delete failed: {e}", True
    return f"Deleted memory '{mem_id}'", False


if __name__ == "__main__":
    server.run()
