#!/usr/bin/env python3
"""
key_registry.py - Registry (single source of truth) of the kernel keyring keys
used by the MCP servers in this directory.

Rules:
  - Every key an MCP server in this project reads must be listed in REGISTRY
    below (name, purpose, which server file uses it).
  - MCP servers read credentials only through read() (or via
    mcp_stdio.keyring_get, which delegates here). read() refuses any name
    that is not in REGISTRY.
  - To add a key: add a _define() entry here, then use the returned constant
    in the MCP server.

Keys live in the kernel keyring (@u) and are wiped on reboot / logout. This
module is read-only: registering a key is an interactive act performed by the
key holder outside the MCP servers, e.g. as the user that runs the server:

    keyctl padd user xai_api_key @u        # payload is read from stdin
    register-keyring-key xai_api_key       # helper on the LibreChat image

    python3 key_registry.py                # status (exit 1 if a key is missing)
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class KeyEntry:
    name: str
    description: str
    used_by: str


REGISTRY: dict[str, KeyEntry] = {}


def _define(name: str, description: str, used_by: str) -> str:
    """Add a key to the registry; returns the name (use as a constant)."""
    if name in REGISTRY:
        raise ValueError(f"duplicate registry entry: {name}")
    REGISTRY[name] = KeyEntry(name=name, description=description, used_by=used_by)
    return name


# --- the registry: one entry per key used by an MCP server in this project ----

XAI_API_KEY = _define(
    "xai_api_key",
    "Grok (xAI) API key (api.x.ai)",
    "grok_mcp.py",
)
X_BEARER_TOKEN = _define(
    "x_bearer_token",
    "X (Twitter) API app-only Bearer token",
    "x_mcp.py",
)
SMTP_PASSWORD = _define(
    "smtp_password",
    "SMTP password for mail sending",
    "mail_mcp.py",
)
EXA_API_KEY = _define(
    "exa_api_key",
    "Exa (exa.ai) API key for web search and page contents",
    "exa_mcp.py",
)


# --- keyring access (the only path MCP servers may use) ------------------------

def present(name: str) -> bool:
    """Whether the key exists in the user keyring (@u). No registry check."""
    try:
        out = subprocess.run(
            ["keyctl", "search", "@u", "user", name],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out)


def read(name: str) -> str | None:
    """Read a key from the kernel keyring (@u) via keyctl.

    Only names in REGISTRY are allowed (anything else raises KeyError).
    Returns the key value, or None if it is not present in the keyring
    (or keyctl is unavailable). Never blocks on user input.
    """
    if name not in REGISTRY:
        raise KeyError(f"'{name}' is not in the key registry (key_registry.py)")
    try:
        serial = subprocess.run(
            ["keyctl", "search", "@u", "user", name],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    if not serial:
        return None
    try:
        proc = subprocess.run(
            ["keyctl", "pipe", serial],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout.strip() or None


def missing_error(name: str) -> str:
    """Tool-error text for a key that cannot be read."""
    entry = REGISTRY.get(name)
    if entry is None:
        return (
            f"Error: key name '{name}' is not in the key registry (key_registry.py).\n"
            f"Only keys listed in the registry can be read by MCP servers."
        )
    return (
        f"Error: no key named '{name}' is registered in the kernel keyring.\n"
        f"The key holder must register it as the user running this server "
        f"(keyctl padd user {name} @u), then the tool can be called again."
    )


# --- CLI ------------------------------------------------------------------------

def cmd_status() -> int:
    missing = []
    for name, entry in REGISTRY.items():
        ok = present(name)
        mark = "ok     " if ok else "MISSING"
        print(f"[{mark}] {name}  ({entry.used_by})  {entry.description}")
        if not ok:
            missing.append(name)
    if missing:
        print(f"\nmissing: {', '.join(missing)}")
        print("register with: keyctl padd user <name> @u")
        return 1
    print("\nall keys present")
    return 0


def main() -> int:
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
