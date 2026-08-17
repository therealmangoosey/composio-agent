"""Termux-friendly runtime enhancements for Tab Assistant's Discord bot."""
import asyncio
import base64
import builtins
import inspect
import os
import sys
from urllib.parse import urlencode

_original_print = builtins.print
_original_input = builtins.input
_original_import = builtins.__import__
_discord_patched = False
_tree_patched = False
_active_cfg = None

COMPOSIO_KNOWLEDGE = (
    "\n\nYou are the Discord interface for Tab Assistant. Composio tools are available when configured. "
    "When a user asks for an external action, use the configured tool-planning and execution system; "
    "do not say you lack access if the capability is configured. Available toolkits are read from the "
    "assistant configuration and may include Gmail, web search, and news. Local list and note tools are "
    "also available. Never claim an action succeeded without a successful tool result. Follow approval "
    "settings for actions that change or send data."
)


def _is_tab_assistant():
    return os.path.basename(sys.argv[0] or "") == "app.py" and os.path.basename(os.getcwd()) == "composio-agent"


def _application_id_from_token(token):
    try:
        first = token.split(".", 1)[0]
        first += "=" * (-len(first) % 4)
        raw = base64.urlsafe_b64decode(first.encode("ascii"))
        value = str(int.from_bytes(raw, "big"))
        return value if value.isdigit() else None
    except Exception:
        return None


def _invite_url():
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    app_id = _application_id_from_token(token) if token else None
    app_id = app_id or os.getenv("DISCORD_APPLICATION_ID", "").strip()
    if not app_id:
        return None
    return "https://discord.com/oauth2/authorize?" + urlencode({"client_id": app_id, "scope": "bot applications.commands", "permissions": "0"})


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
        _original_print("\nInvite link:\n" + (url or "Could not generate it. Set DISCORD_BOT_TOKEN in .env first.") + "\n")
        return ""
    return value


def _channel_ids(cfg):
    raw = str(cfg.get("discord", {}).get("allowed_channel_id", ""))
    return {x.strip() for x in raw.split(",") if x.strip()}


def _set_channel(cfg, channel_id, add=False):
    current = _channel_ids(cfg)
    if add:
        current.add(str(channel_id))
    else:
        current = {str(channel_id)}
    value = ",".join(sorted(current))
    cfg["discord"]["allowed_channel_id"] = value
    save = globals().get("_save_config")
    if save:
        save(cfg)
    else:
        try:
            env = os.path.join(os.getcwd(), ".env")
            lines = []
            if os.path.exists(env):
                lines = open(env, encoding="utf-8").read().splitlines()
            found = False
            for i, line in enumerate(lines):
                if line.startswith("DISCORD_ALLOWED_CHANNEL_ID="):
                    lines[i] = "DISCORD_ALLOWED_CHANNEL_ID=" + value
                    found = True
            if not found:
                lines.append("DISCORD_ALLOWED_CHANNEL_ID=" + value)
            with open(env, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass
    return value


def _patch_tree(module):
    global _tree_patched
    if _tree_patched or not hasattr(module, "CommandTree"):
        return
    _tree_patched = True
    original_sync = module.CommandTree.sync
    registered = set()

    async def patched_sync(self, *args, **kwargs):
        if id(self) not in registered:
            try:
                async def this_channel(interaction):
                    if interaction.guild is None:
                        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
                        return
                    perms = getattr(interaction.user, "guild_permissions", None)
                    if not perms or not (getattr(perms, "manage_channels", False) or getattr(perms, "administrator", False)):
                        await interaction.response.send_message("You need Manage Channels or Administrator to use this.", ephemeral=True)
                        return
                    cfg = _active_cfg
                    if cfg is None:
                        await interaction.response.send_message("Bot configuration is unavailable.", ephemeral=True)
                        return
                    value = _set_channel(cfg, interaction.channel_id, add=False)
                    await interaction.response.send_message(f"✅ This channel is now the bot channel.\nChannel ID: `{interaction.channel_id}`")
                    _original_print("Discord channel set to " + value)

                async def add_channel(interaction):
                    if interaction.guild is None:
                        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
                        return
                    perms = getattr(interaction.user, "guild_permissions", None)
                    if not perms or not (getattr(perms, "manage_channels", False) or getattr(perms, "administrator", False)):
                        await interaction.response.send_message("You need Manage Channels or Administrator to use this.", ephemeral=True)
                        return
                    cfg = _active_cfg
                    if cfg is None:
                        await interaction.response.send_message("Bot configuration is unavailable.", ephemeral=True)
                        return
                    value = _set_channel(cfg, interaction.channel_id, add=True)
                    await interaction.response.send_message(f"✅ Added this channel to the bot channels.\nChannel ID: `{interaction.channel_id}`")
                    _original_print("Discord channels: " + value)

                self.add_command(module.Command(name="this-channel", description="Use this channel for the bot", callback=this_channel), override=True)
                self.add_command(module.Command(name="add-channel", description="Add this channel for the bot", callback=add_channel), override=True)
                registered.add(id(self))
            except Exception as exc:
                _original_print("Discord channel command setup failed: " + str(exc))
        return await original_sync(self, *args, **kwargs)

    module.CommandTree.sync = patched_sync


def _patch_discord(module):
    global _discord_patched, _active_cfg
    if _discord_patched or not hasattr(module, "Client"):
        return module
    _discord_patched = True
    original_init = module.Client.__init__

    def patched_init(self, *args, **kwargs):
        global _active_cfg
        original_init(self, *args, **kwargs)
        try:
            self.intents.message_content = True
            frame = inspect.currentframe().f_back
            if frame is None:
                return
            cfg = frame.f_locals.get("cfg")
            if not cfg:
                return
            _active_cfg = cfg
            globals()["_save_config"] = frame.f_globals.get("save_config")
            cfg["settings"]["system_prompt"] = cfg["settings"].get("system_prompt", "") + COMPOSIO_KNOWLEDGE
            app_globals = frame.f_globals
            send = app_globals.get("send_with_failover")
            add = app_globals.get("add_message")
            build_plan = app_globals.get("build_plan")
            execute_plan = app_globals.get("execute_plan")
            log_error = app_globals.get("log_error", lambda exc: None)
            short = app_globals.get("short", lambda x, n=180: str(x)[:n])

            async def normal_channel_message(message):
                if message.author.bot or message.guild is None:
                    return
                if str(message.channel.id) not in _channel_ids(cfg):
                    return
                allow_users = cfg.get("discord", {}).get("allowed_user_ids", [])
                if allow_users and message.author.id not in allow_users:
                    return
                text = message.content.strip()
                if not text or text.startswith("/"):
                    return
                session = frame.f_locals.get("discord_session")
                if session is None or send is None or add is None:
                    return
                try:
                    async with message.channel.typing():
                        add(session, "user", text)
                        tool_words = ("composio", "send an email", "email", "gmail", "search the web", "web search", "news", "save a note", "add to list", "read my")
                        wants_tool = any(word in text.lower() for word in tool_words)
                        if wants_tool and build_plan and execute_plan:
                            plan = await asyncio.to_thread(build_plan, cfg, text)
                            results = await asyncio.to_thread(execute_plan, cfg, plan)
                            successful = [str(result) for ok, result in results if ok]
                            failed = [str(result) for ok, result in results if not ok]
                            answer = ("Done. " + " | ".join(successful)) if successful else ("I couldn't complete that: " + " | ".join(failed) if failed else "I couldn't find a tool action for that request.")
                            provider = cfg.get("selected_provider", "")
                            model = cfg.get("selected_model", "")
                        else:
                            answer, provider, model = await asyncio.to_thread(send, cfg, session, False)
                        add(session, "assistant", answer, provider, model)
                        await message.reply(str(answer)[:1900], mention_author=False)
                except Exception as exc:
                    log_error(exc)
                    try:
                        await message.reply("❌ " + short(exc), mention_author=False)
                    except Exception:
                        pass

            self.event(normal_channel_message)
        except Exception as exc:
            _original_print("Discord message handler setup failed: " + str(exc))

    module.Client.__init__ = patched_init
    return module


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    module = _original_import(name, globals, locals, fromlist, level)
    if name == "discord" or name.startswith("discord."):
        root = module if name == "discord" else _original_import("discord")
        _patch_discord(root)
        try:
            _patch_tree(root.app_commands)
        except Exception:
            pass
    return module


if _is_tab_assistant():
    builtins.print = _print
    builtins.input = _input
    builtins.__import__ = _import
