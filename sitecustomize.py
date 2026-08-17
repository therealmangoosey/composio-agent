"""Termux-friendly Discord enhancements for Tab Assistant."""
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
    "do not say you lack access if the capability is configured. Available toolkits may include Gmail, "
    "web search, and news. Local list and note tools are also available. Never claim an action succeeded "
    "without a successful tool result. Follow approval settings for actions that change or send data."
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
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": app_id,
        "scope": "bot applications.commands",
        "permissions": "0",
    })


def _print(*args, **kwargs):
    return _original_print(*args, **kwargs)


def _input(prompt="", *args, **kwargs):
    return _original_input(prompt, *args, **kwargs)


def _channel_ids(cfg):
    return {x.strip() for x in str(cfg.get("discord", {}).get("allowed_channel_id", "")).split(",") if x.strip()}


def _persist_channel_ids(cfg, ids):
    cfg["discord"]["allowed_channel_id"] = ",".join(sorted(set(ids)))
    save = globals().get("_save_config")
    if save:
        save(cfg)
    return cfg["discord"]["allowed_channel_id"]


def _patch_tree(module):
    global _tree_patched
    if _tree_patched or not hasattr(module, "CommandTree"):
        return
    _tree_patched = True
    original_sync = module.CommandTree.sync

    async def patched_sync(self, *args, **kwargs):
        commands_ready = getattr(self, "_tab_channel_commands_ready", False)
        if not commands_ready:
            async def this_channel(interaction):
                if interaction.guild is None:
                    await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
                    return
                perms = getattr(interaction.user, "guild_permissions", None)
                if not perms or not (getattr(perms, "manage_channels", False) or getattr(perms, "administrator", False)):
                    await interaction.response.send_message("You need Manage Channels or Administrator.", ephemeral=True)
                    return
                cfg = _active_cfg
                if cfg is None:
                    await interaction.response.send_message("Bot configuration is unavailable.", ephemeral=True)
                    return
                _persist_channel_ids(cfg, {str(interaction.channel_id)})
                await interaction.response.send_message("✅ This channel is now a bot chat channel.")

            async def add_channel(interaction):
                if interaction.guild is None:
                    await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
                    return
                perms = getattr(interaction.user, "guild_permissions", None)
                if not perms or not (getattr(perms, "manage_channels", False) or getattr(perms, "administrator", False)):
                    await interaction.response.send_message("You need Manage Channels or Administrator.", ephemeral=True)
                    return
                cfg = _active_cfg
                if cfg is None:
                    await interaction.response.send_message("Bot configuration is unavailable.", ephemeral=True)
                    return
                ids = _channel_ids(cfg)
                ids.add(str(interaction.channel_id))
                _persist_channel_ids(cfg, ids)
                await interaction.response.send_message("✅ Added this channel as a bot chat channel.")

            async def remove_channel(interaction):
                if interaction.guild is None:
                    await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
                    return
                perms = getattr(interaction.user, "guild_permissions", None)
                if not perms or not (getattr(perms, "manage_channels", False) or getattr(perms, "administrator", False)):
                    await interaction.response.send_message("You need Manage Channels or Administrator.", ephemeral=True)
                    return
                cfg = _active_cfg
                if cfg is None:
                    await interaction.response.send_message("Bot configuration is unavailable.", ephemeral=True)
                    return
                ids = _channel_ids(cfg)
                ids.discard(str(interaction.channel_id))
                _persist_channel_ids(cfg, ids)
                await interaction.response.send_message("✅ Removed this channel from bot chat channels.")

            try:
                self.command(name="this-channel", description="Make this channel a bot chat channel")(this_channel)
                self.command(name="add-channel", description="Add this channel as a bot chat channel")(add_channel)
                self.command(name="remove-channel", description="Stop the bot using this channel")(remove_channel)
                self._tab_channel_commands_ready = True
                _original_print("Registered Discord channel commands.")
            except Exception as exc:
                _original_print("Discord channel command registration failed: " + str(exc))

        result = await original_sync(self, *args, **kwargs)

        client = getattr(self, "client", None)
        if client is not None:
            for guild in list(getattr(client, "guilds", []) or []):
                try:
                    await original_sync(self, guild=guild)
                except Exception as exc:
                    _original_print("Discord guild command sync failed: " + str(exc))
        return result

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
                            answer, provider, model = await asyncio.to_thread(send, cfg, session, stream=False)
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
        except Exception as exc:
            _original_print("Discord command patch load failed: " + str(exc))
    return module


if _is_tab_assistant():
    builtins.print = _print
    builtins.input = _input
    builtins.__import__ = _import
