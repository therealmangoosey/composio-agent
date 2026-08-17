"""Discord invite helper for Tab Assistant."""
from urllib.parse import quote


def bot_invite_url(token):
    """Return an OAuth2 invite URL for the bot represented by TOKEN."""
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
    try:
        import base64
        # Discord bot tokens have the application/user id encoded in the first segment.
        app_id = base64.b64decode(token.split('.', 1)[0] + '===').decode('ascii')
    except Exception as exc:
        raise RuntimeError("Could not determine the Discord application ID from the bot token") from exc
    return (
        "https://discord.com/oauth2/authorize?client_id="
        + quote(app_id, safe="")
        + "&scope=bot%20applications.commands&permissions=0"
    )


def show_invite(token):
    print("\n=== Invite bot to a server ===")
    print(bot_invite_url(token))
    print("\nOpen that link in your browser, choose your server, then authorize the bot.")
    input("\nPress Enter to return to the menu...")
