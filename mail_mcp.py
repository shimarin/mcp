#!/usr/bin/env python3
"""
mail_mcp.py - MCP server (stdio) that sends email over SMTP.

Spawned by an MCP client over stdio; exposes one tool:
  mail_send - send a plain-text email to the configured recipient

The recipient comes from configuration (the model cannot change who it mails),
which keeps this a narrow notification channel. Set it per user to keep
notifications separate, e.g. MAIL_TO={{LIBRECHAT_USER_EMAIL}} in LibreChat.

Configuration (environment variables, or the matching --mail-to=VALUE style
command line arguments; the argument wins):
  MAIL_TO        recipient address
  MAIL_FROM      sender address
  SMTP_HOST      SMTP server host
  SMTP_PORT      SMTP server port
  SMTP_USERNAME  SMTP auth username
  SMTP_TLS       starttls (default) | ssl | off

Credential:
  SMTP_PASSWORD  key name 'smtp_password' in the kernel keyring (see
                 key_registry.py); never appears in config, args or logs

No third-party dependencies (Python standard library only: smtplib, email).

Standalone smoke test (no send, no key needed):
  printf '%s\\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | python3 mail_mcp.py
"""

import smtplib
from email.message import EmailMessage

import key_registry
from mcp_stdio import McpServer, keyring_get, keyring_missing_error, setting

SMTP_TIMEOUT = 30  # seconds, per connection

server = McpServer(name="mail", version="1.0")


@server.tool(
    "send",
    (
        "Send a plain-text email to the recipient configured for the current user "
        "(you cannot change the recipient). Use to notify the user of "
        "completed work, e.g. a periodic check that found updates. "
        "Keep the body short: it may be read on a phone."
    ),
    schema={
        "subject": {"type": "string", "description": "Email subject (one line)"},
        "body": {"type": "string", "description": "Email body (plain text)"},
    },
    required=("subject", "body"),
)
def send(args: dict):
    subject = (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    if not subject:
        return "Missing 'subject'", True
    if not body:
        return "Missing 'body'", True

    to       = setting("MAIL_TO")
    frm      = setting("MAIL_FROM")
    host     = setting("SMTP_HOST")
    port_s   = setting("SMTP_PORT")
    username = setting("SMTP_USERNAME")
    tls      = (setting("SMTP_TLS") or "starttls").lower()

    missing = [n for n, v in (
        ("MAIL_TO", to), ("MAIL_FROM", frm), ("SMTP_HOST", host),
        ("SMTP_PORT", port_s), ("SMTP_USERNAME", username),
    ) if v is None]
    if missing:
        return "mail_mcp is not configured: missing env " + ", ".join(missing), True
    if tls not in ("starttls", "ssl", "off"):
        return f"Invalid SMTP_TLS {tls!r}: use starttls, ssl or off", True
    try:
        port = int(port_s)
    except ValueError:
        return f"Invalid SMTP_PORT {port_s!r}: expected an integer", True

    for label, value in (("MAIL_TO", to), ("MAIL_FROM", frm), ("subject", subject)):
        if "\r" in value or "\n" in value:
            return f"Invalid {label} {value!r}: must be a single line", True
    for label, value in (("MAIL_TO", to), ("MAIL_FROM", frm)):
        if "@" not in value or any(c.isspace() for c in value):
            return f"Invalid {label} {value!r}: expected a single email address", True

    password = keyring_get(key_registry.SMTP_PASSWORD)
    if password is None:
        return keyring_missing_error(key_registry.SMTP_PASSWORD), True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg.set_content(body)

    try:
        if tls == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT) as s:
                s.login(username, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as s:
                if tls == "starttls":
                    s.starttls()
                s.login(username, password)
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        return f"SMTP send failed: {e}", True

    return f"Sent email '{subject}' to {to}", False


if __name__ == "__main__":
    server.run()
