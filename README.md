# mcp-servers

Small stdio MCP servers sharing one minimal framework (`mcp_stdio.py`). Python
standard library only, no third-party dependencies, credentials read from the
Linux kernel keyring.

| server | tools | keyring key |
| --- | --- | --- |
| `grok_mcp.py` | `research` | `xai_api_key` |
| `x_mcp.py` | `user_timeline`, `usage` | `x_bearer_token` |
| `exa_mcp.py` | `web_search`, `web_fetch` | `exa_api_key` |
| `mail_mcp.py` | `send` | `smtp_password` |
| `memory_mcp.py` | `save`, `recall`, `list`, `read`, `delete` | - |

## Credentials

Credentials are never stored in files, config, arguments or logs. They live in
the kernel user keyring (`@u`) and are read with `keyctl`, so they disappear on
reboot or logout and must be re-entered by the key holder:

```sh
keyctl padd user xai_api_key @u      # the payload is read from stdin
```

`key_registry.py` is the single source of truth: `read()` refuses any key name
that is not listed in its `REGISTRY`, so a server cannot be pointed at an
arbitrary key. `python3 key_registry.py` reports which keys are missing.

A stdio MCP server has no UI, so a missing key is reported as a tool error
rather than a prompt.

## Settings

Every configurable value is accepted through both a command line argument and
an environment variable, so the caller decides which channel to use in its
config. The argument wins when both are given. Arguments are visible in `ps`
(`/proc/<pid>/cmdline`); environment variables are readable only by the same
UID and root.

| variable | argument | server | meaning |
| --- | --- | --- | --- |
| `MEMORY_ROOT` | `--memory-root` | memory | storage root (default `~/.local/share/memory_mcp`) |
| `MEMORY_KEY` | `--memory-key` | memory | namespace; one per user when the server is shared |
| `MAIL_TO` | `--mail-to` | mail | recipient |
| `MAIL_FROM` | `--mail-from` | mail | sender |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_TLS` | `--smtp-*` | mail | SMTP endpoint |
| `X_USERNAME` | `--x-username` | x | account read when the model names none |
| `EXA_USER_ID` | `--exa-user-id` | exa | opaque end-user id, sent as `userIdentifier` so Exa attributes usage per user |

## Configuring an MCP client

Any stdio-capable client works. In LibreChat (`librechat.yaml`), stdio servers
are spawned per user, so per-user values can be written as placeholders:

```yaml
mcpServers:
  grok:
    type: stdio
    command: /usr/bin/python3
    args:
      - /opt/mcp/grok_mcp.py
    timeout: 130000
  exa:
    type: stdio
    command: /usr/bin/python3
    args:
      - /opt/mcp/exa_mcp.py
      - '--exa-user-id={{LIBRECHAT_USER_ID}}'
    timeout: 130000
  mail:
    type: stdio
    command: /usr/bin/python3
    args:
      - /opt/mcp/mail_mcp.py
      - '--mail-to={{LIBRECHAT_USER_EMAIL}}'
      - '--mail-from=notify@example.com'
      - '--smtp-host=smtp.example.com'
      - '--smtp-port=587'
      - '--smtp-username=notify'
  x:
    type: stdio
    command: /usr/bin/python3
    args:
      - /opt/mcp/x_mcp.py
      - '--x-username={{X_USERNAME}}'
    customUserVars:
      X_USERNAME:
        title: 'X username'
        description: 'Your X account name (no @)'
        sensitive: false
    timeout: 130000
```

- `{{LIBRECHAT_USER_*}}` and `{{LIBRECHAT_BODY_*}}` resolve per request for
  servers defined by the operator. Servers added through the UI do not get them.
- Values with no matching user field go in `customUserVars`, which renders an
  input field in the client's server settings dialog.
- Servers carrying such placeholders are not initialized at startup; the log
  shows `Tools: undefined` / `Initialized in: 0ms` and the tools appear when a
  user first makes a request. That is expected.
- The default tool-call timeout is 60 s. `grok` and `exa` can run longer, so
  raise `timeout` as above.

## Running one standalone

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 exa_mcp.py
```

## Requirements

- Python 3.10 or newer
- `keyctl` (Linux; on Gentoo `sys-apps/keyutils`)
- Outbound HTTPS for the grok, x and exa servers

## License

MIT
