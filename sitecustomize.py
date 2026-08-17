"""Small Termux-friendly menu enhancement for Tab Assistant.

Python automatically imports sitecustomize at startup when it is on sys.path.
This keeps the main app lightweight while adding the Discord invite helper to
its existing numbered menu.
"""
import base64
import builtins
import os
import sys
from urllib.parse import urlencode

_original_print = builtins.print
_original_input = builtins.input


def _is_tab_assistant():
    script = os.path.basename(sys.argv[0] or "")
    cwd = os.path.basename(os.getcwd())
    return script == "app.py" and cwd == "composio-agent"


def _application_id_from_token(token):
    try:
        first = token.split(".", 1)[0]
        first += "=" * (-len(first) % 4)
        raw = base64.urlsafe_b64decode(first.encode("ascii"))
        app_id = str(int.from_bytes(raw, "big"))
        return app_id if app_id.isdigit() else None
    except (ValueError, TypeError, UnicodeError, base64.binascii.Error):
        return None


def _invite_url():
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    app_id = _application_id_from_token(token) if token else None
    if not app_id:
        app_id = os.getenv("DISCORD_APPLICATION_ID", "").strip()
    if not app_id:
        return None
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": app_id,
        "scope": "bot applications.commands",
        "permissions": "0",
    })


def _print(*args, **kwargs):
    if _is_tab_assistant() and args:
        text = str(args[0])
        if "5. Manage API keys" in text and "0. Exit" in text and "Invite bot" not in text:
            text += "\n10. Invite bot to a server"
            args = (text, *args[1:])
    return _original_print(*args, **kwargs)


def _input(prompt="", *args, **kwargs):
    value = _original_input(prompt, *args, **kwargs)
    if _is_tab_assistant() and prompt.strip() == "Choose:" and value.strip().lower() in {"10", "invite", "i"}:
        url = _invite_url()
        if url:
            _original_print("\nInvite link:\n" + url + "\n")
            _original_print("Open that link in your browser to add the bot to a server.\n")
        else:
            _original_print("\nCould not generate the invite link. Set DISCORD_BOT_TOKEN in .env first.\n")
        return ""
    return value


if _is_tab_assistant():
    builtins.print = _print
    builtins.input = _input
