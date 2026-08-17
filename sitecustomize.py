"""Small Termux-friendly startup enhancements for Tab Assistant."""
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

COMPOSIO_KNOWLEDGE = (
    "\n\nYou are the Discord interface for Tab Assistant. You have access to Composio tools "
    "configured by the owner. When a user asks you to perform an action that requires a "
    "connected app or external service, do NOT claim you lack access. Use the Composio "
    "tool-planning and execution system available to you. Available configured toolkits "
    "are read from the assistant configuration and may include Gmail, web search, and news. "
    "You can also use the local list and note tools. If a requested capability is not "
    "configured or a tool execution fails, explain the actual reason. Never pretend an action "
    "was completed without a successful tool result. For actions that change or send data, "
    "follow the configured approval requirement."
)


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


def _patch_discord(module):
    global _discord_patched
    if _discord_patched or not hasattr(module, "Client"):
        return module
    _discord_patched = True
    original_init = module.Client.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            self.intents.message_content = True
            frame = inspect.currentframe().f_back
            if frame is None:
                return
            cfg = frame.f_locals.get("cfg")
            if not cfg:
                return
            cfg["settings"]["system_prompt"] = cfg["settings"].get("system_prompt", "") + COMPOSIO_KNOWLEDGE
            allowed = str(cfg.get("discord", {}).get("allowed_channel_id", ""))
            if not allowed:
                return
            app_globals = frame.f_globals

            async def normal_channel_message(message):
                if message.author.bot or message.guild is None:
                    return
                if str(message.channel.id) != allowed:
                    return
                allow_users = cfg.get("discord", {}).get("allowed_user_ids", [])
                if allow_users and message.author.id not in allow_users:
                    return
                text = message.content.strip()
                if not text or text.startswith("/"):
                    return
                session = frame.f_locals.get("discord_session")
                send = app_globals.get("send_with_failover")
                add = app_globals.get("add_message")
                short = app_globals.get("short", lambda x: str(x)[:180])
                log_error = app_globals.get("log_error", lambda exc: None)
                build_plan = app_globals.get("build_plan")
                execute_plan = app_globals.get("execute_plan")
                if session is None or send is None or add is None:
                    return
                try:
                    async with message.channel.typing():
                        add(session, "user", text)
                        tool_words = ("composio", "send an email", "email", "gmail", "search the web", "web search", "news", "save a note", "add to list", "read my")
                        wants_tool = any(word in text.lower() for word in tool_words)
                        if wants_tool and build_plan and execute_plan:
                            plan = build_plan(cfg, text)
                            steps = plan.get("steps", [])
                            results = execute_plan(cfg, plan)
                            successful = [str(result) for ok, result in results if ok]
                            failed = [str(result) for ok, result in results if not ok]
                            if successful:
                                answer = "Done. " + " | ".join(successful)
                            elif failed:
                                answer = "I couldn't complete that: " + " | ".join(failed)
                            else:
                                answer = "I couldn't find a tool action for that request."
                            provider = cfg.get("selected_provider", "")
                            model = cfg.get("selected_model", "")
                        else:
                            answer, provider, model = send(cfg, session, stream=False)
                        add(session, "assistant", answer, provider, model)
                        await message.reply(answer[:1900], mention_author=False)
                except Exception as exc:
                    log_error(exc)
                    await message.reply("❌ " + short(exc), mention_author=False)

            self.event(normal_channel_message)
        except Exception:
            pass

    module.Client.__init__ = patched_init
    return module


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    module = _original_import(name, globals, locals, fromlist, level)
    if name == "discord" or name.startswith("discord."):
        root = module if name == "discord" else _original_import("discord")
        _patch_discord(root)
    return module


if _is_tab_assistant():
    builtins.print = _print
    builtins.input = _input
    builtins.__import__ = _import
