#!/usr/bin/env python3
"""Core runtime for the Termux/Discord assistant."""
import asyncio
import copy
import datetime as dt
import json
import os
import re
import threading
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote_plus
from html import unescape

import requests

try:
    import discord
except ImportError:
    discord = None

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_FILE = DATA / "config.json"
MEMORY_FILE = DATA / "memory.json"
ERROR_FILE = DATA / "errors.log"
ENV_FILE = ROOT / ".env"

PROVIDERS = {
    "GROQ": ("https://api.groq.com/openai/v1", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], True),
    "Google Gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", ["gemini-2.5-flash"], True),
    "OpenRouter": ("https://openrouter.ai/api/v1", ["openrouter/free"], True),
    "Cerebras": ("https://api.cerebras.ai/v1", ["gpt-oss-120b"], True),
    "OpenAI": ("https://api.openai.com/v1", ["gpt-4o-mini"], False),
    "DeepSeek": ("https://api.deepseek.com", ["deepseek-chat"], False),
    "xAI Grok": ("https://api.x.ai/v1", ["grok-4.1-mini"], False),
}
ENV_KEYS = {k: f"{k.upper().replace(' ', '_')}_API_KEYS" for k in PROVIDERS}
ENV_KEYS["Google Gemini"] = "GEMINI_API_KEYS"
ENV_KEYS["OpenRouter"] = "OPENROUTER_API_KEYS"
ENV_KEYS["Cerebras"] = "CEREBRAS_API_KEYS"
ENV_KEYS["xAI Grok"] = "XAI_API_KEYS"


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def short(value, limit=900):
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def log_error(exc):
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        with ERROR_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{now()}]\n{traceback.format_exc()}\n")
    except OSError:
        pass


def load_dotenv():
    if not ENV_FILE.exists():
        return
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


def save_dotenv(values):
    old = {}
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    old[key.strip()] = value
        except OSError:
            pass
    old.update({k: str(v) for k, v in values.items() if v is not None})
    ENV_FILE.write_text("# Secrets; do not commit.\n" + "\n".join(f"{k}={v}" for k, v in sorted(old.items()) if v) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass


def defaults():
    return {
        "providers": {k: {"base_url": v[0], "models": list(v[1]), "keys": []} for k, v in PROVIDERS.items()},
        "selected_provider": "GROQ",
        "selected_model": "openai/gpt-oss-20b",
        "mode": "free",
        "composio": {"api_key": "", "user_id": "tab-owner", "toolkit_version": "latest"},
        "discord": {"token": "", "channel_ids": [], "allowed_user_ids": [], "autostart": True},
        "settings": {"system_prompt": "You are Tab Assistant. Be concise, accurate and context-aware.", "max_history": 8, "temperature": 0.4},
    }


def load_config():
    DATA.mkdir(parents=True, exist_ok=True)
    load_dotenv()
    cfg = defaults()
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                for section in ("providers", "composio", "discord", "settings"):
                    if isinstance(stored.get(section), dict):
                        cfg[section].update(stored[section])
                for key in ("selected_provider", "selected_model", "mode"):
                    if key in stored:
                        cfg[key] = stored[key]
        except (OSError, json.JSONDecodeError):
            pass
    for name, info in cfg["providers"].items():
        info.setdefault("base_url", PROVIDERS[name][0])
        info.setdefault("models", list(PROVIDERS[name][1]))
        info.setdefault("keys", [])
        raw = os.getenv(ENV_KEYS[name], "")
        if raw:
            info["keys"] = [{"key": x.strip(), "free": PROVIDERS[name][2]} for x in raw.split(",") if x.strip()]
    cfg["composio"]["api_key"] = os.getenv("COMPOSIO_API_KEY", cfg["composio"].get("api_key", ""))
    cfg["discord"]["token"] = os.getenv("DISCORD_BOT_TOKEN", cfg["discord"].get("token", ""))
    legacy = os.getenv("DISCORD_ALLOWED_CHANNEL_ID", "")
    if legacy and not cfg["discord"].get("channel_ids"):
        cfg["discord"]["channel_ids"] = [x.strip() for x in legacy.split(",") if x.strip()]
    return cfg


def persist(cfg):
    DATA.mkdir(parents=True, exist_ok=True)
    env = {ENV_KEYS[n]: ",".join(x.get("key", "") for x in info.get("keys", [])) for n, info in cfg["providers"].items()}
    env.update({
        "COMPOSIO_API_KEY": cfg["composio"].get("api_key", ""),
        "DISCORD_BOT_TOKEN": cfg["discord"].get("token", ""),
        "DISCORD_ALLOWED_CHANNEL_ID": ",".join(cfg["discord"].get("channel_ids", [])),
    })
    save_dotenv(env)
    public = copy.deepcopy(cfg)
    for info in public["providers"].values():
        info["keys"] = [{"free": bool(item.get("free"))} for item in info.get("keys", [])]
    public["composio"]["api_key"] = ""
    public["discord"]["token"] = ""
    CONFIG_FILE.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")


def new_session(session_key=None):
    return {
        "id": uuid.uuid4().hex,
        "session_key": session_key,
        "messages": [],
        "memory": "",
        "turns_since_memory": 0,
        "created": now(),
        "last_active": now(),
    }


def add(session, role, content):
    session["messages"].append({"role": role, "content": str(content), "time": now()})
    session["last_active"] = now()
    if role == "user":
        session["turns_since_memory"] += 1


def load_session(session_key):
    if not MEMORY_FILE.exists():
        return new_session(session_key)
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        matches = [x for x in data.get("sessions", []) if x.get("session_key") == session_key]
        if not matches:
            return new_session(session_key)
        session = max(matches, key=lambda x: x.get("last_active", x.get("created", "")))
        session.setdefault("messages", [])
        session.setdefault("memory", "")
        session.setdefault("turns_since_memory", 0)
        return session
    except (OSError, json.JSONDecodeError):
        return new_session(session_key)


def save_session(session):
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8")) if MEMORY_FILE.exists() else {"sessions": []}
    except (OSError, json.JSONDecodeError):
        data = {"sessions": []}
    sessions = [x for x in data.get("sessions", []) if x.get("id") != session["id"]]
    sessions.append(session)
    data["sessions"] = sessions[-100:]
    DATA.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def provider_models(cfg, provider, key):
    try:
        response = requests.get(cfg["providers"][provider]["base_url"].rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=8)
        models = [x.get("id") for x in response.json().get("data", []) if x.get("id")]
        if provider == "OpenRouter":
            free = [m for m in models if m == "openrouter/free" or m.endswith(":free")]
            models = free or ["openrouter/free"]
        return models or cfg["providers"][provider]["models"]
    except Exception:
        return cfg["providers"][provider]["models"]


def send(cfg, session, system=None, temperature=None, json_mode=False):
    candidates = []
    for provider, info in cfg["providers"].items():
        for item in info.get("keys", []):
            if cfg["mode"] == "free" and not item.get("free", False):
                continue
            for model in provider_models(cfg, provider, item["key"]):
                if cfg["mode"] == "free" and provider == "OpenRouter" and model != "openrouter/free" and not model.endswith(":free"):
                    continue
                candidates.append((provider, model, item["key"]))
    if not candidates:
        raise RuntimeError("No usable API keys. Configure a free provider key first.")
    messages = [{"role": "system", "content": system or cfg["settings"]["system_prompt"]}]
    if session.get("memory"):
        messages.append({"role": "system", "content": "CONVERSATION MEMORY:\n" + session["memory"]})
    messages.extend(session.get("messages", [])[-max(4, int(cfg["settings"].get("max_history", 8))):])
    last_error = None
    for provider, model, key in candidates:
        payload = {"model": model, "messages": messages, "temperature": float(temperature if temperature is not None else cfg["settings"].get("temperature", 0.4))}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        variants = [payload, {k: v for k, v in payload.items() if k != "response_format"}, {k: v for k, v in payload.items() if k not in {"response_format", "temperature"}}]
        for body in variants:
            try:
                response = requests.post(cfg["providers"][provider]["base_url"].rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body, timeout=45)
                if not response.ok:
                    raise RuntimeError(f"{provider} {response.status_code}: {short(response.text, 300)}")
                text = str(response.json()["choices"][0]["message"].get("content", "")).strip()
                if not text:
                    raise RuntimeError("Model returned an empty response")
                return text
            except Exception as exc:
                last_error = exc
                log_error(exc)
                break
    raise last_error or RuntimeError("All providers failed")


def refresh_memory(cfg, session):
    if session.get("turns_since_memory", 0) < 3:
        return
    prompt = "Summarize important facts, decisions, preferences, current tasks and unresolved items from this conversation. Never include secrets. Keep it concise.\n\n" + "\n".join(f"{m['role']}: {m['content']}" for m in session["messages"][-12:])
    temp = new_session("memory-refresh")
    add(temp, "user", prompt)
    session["memory"] = send(cfg, temp, system="You are a memory summarizer. Return only factual concise memory.", temperature=0.1)[:4000]
    session["turns_since_memory"] = 0
    save_session(session)


def web_search(query, limit=6):
    try:
        response = requests.get("https://html.duckduckgo.com/html/?q=" + quote_plus(query), headers={"User-Agent": "Mozilla/5.0 (Tab Assistant)"}, timeout=12)
        response.raise_for_status()
        results = []
        for match in re.finditer(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', response.text, re.I | re.S):
            url = unescape(match.group(1))
            title = unescape(re.sub(r"<.*?>", " ", match.group(2))).strip()
            if not url.startswith("http") or any(item["url"] == url for item in results):
                continue
            results.append({"title": short(title, 220), "url": url})
            if len(results) >= limit:
                break
        return results
    except Exception as exc:
        log_error(exc)
        return []


def composio_research(request):
    results = []
    seen = set()
    for query in [
        f"Composio {request} action tool",
        f"site:composio.dev/docs {request} Composio",
        f"site:composio.dev/tools {request} Composio",
        f"site:github.com/ComposioHQ {request} Composio",
    ]:
        for item in web_search(query, 5):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            results.append(item)
            if len(results) >= 12:
                return results
    return results


def plan_composio_action(cfg, request):
    if not cfg["composio"].get("api_key"):
        raise RuntimeError("Composio API key is not configured")
    evidence = composio_research(request)
    evidence_text = "\n".join(f"- {x['title']} — {x['url']}" for x in evidence) or "No web results found."
    planner = new_session("composio-planner")
    add(planner, "user", f"USER WANTS AN API/ACTION EXECUTED:\n{request}\n\nLIVE COMPOSIO RESEARCH:\n{evidence_text}\n\nReturn JSON only: {{\"steps\":[{{\"tool\":\"EXACT_COMPOSIO_ACTION_SLUG\",\"description\":\"what it does\",\"args\":{{}}}}]}}\nDo not invent an action slug. If you cannot verify one from the research, return {{\"steps\":[]}}. Never execute anything.")
    raw = send(cfg, planner, system="You resolve Composio actions from live evidence. The goal is to execute the user's requested action, not to write a research report.", temperature=0.1, json_mode=True)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise RuntimeError("Composio planner returned invalid JSON")
    plan = json.loads(match.group(0))
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not plan["steps"]:
        raise RuntimeError("I couldn't verify a suitable Composio action, so I won't guess")
    return plan


def execute_composio(cfg, plan):
    try:
        from composio import Composio
    except ImportError as exc:
        raise RuntimeError("Install Composio with: python -m pip install -U composio") from exc
    client = Composio(api_key=cfg["composio"]["api_key"])
    results = []
    for step in plan.get("steps", []):
        tool = step.get("tool")
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        try:
            try:
                result = client.tools.execute(tool, version=cfg["composio"].get("toolkit_version", "latest"), arguments=args, user_id=cfg["composio"].get("user_id", "tab-owner"))
            except TypeError:
                result = client.tools.execute(tool, arguments=args, user_id=cfg["composio"].get("user_id", "tab-owner"))
            results.append((True, str(result)))
        except Exception as exc:
            results.append((False, short(exc)))
    return results


def looks_like_composio_action(text):
    t = text.lower()
    explicit = any(p in t for p in ("composio", "api call", "api request", "call the api", "use the api", "execute an api", "send an email", "create a calendar", "save a note", "add to list"))
    research_only = any(p in t for p in ("research", "look up", "search the web", "find information", "investigate")) and not explicit
    return explicit and not research_only


def looks_like_web_research(text):
    t = text.lower().strip()
    return bool(re.match(r"^/?research\b", t)) or any(p in t for p in ("search the web", "look up", "research this", "latest", "current prices", "find businesses"))


def approval_view(owner_id):
    class Approval(discord.ui.View):
        def __init__(self, cfg, plan):
            super().__init__(timeout=300)
            self.cfg = cfg
            self.plan = plan
            self.used = False

        async def check(self, interaction):
            if self.used:
                await interaction.response.send_message("This approval has expired.", ephemeral=True)
                return False
            if interaction.user.id != owner_id:
                await interaction.response.send_message("Not your approval.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
        async def approve(self, interaction, button):
            if not await self.check(interaction):
                return
            self.used = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            results = await asyncio.to_thread(execute_composio, self.cfg, self.plan)
            text = "\n".join(("✅" if ok else "❌") + " " + result for ok, result in results) or "No result"
            await interaction.followup.send(text[:1900])

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
        async def deny(self, interaction, button):
            if not await self.check(interaction):
                return
            self.used = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="❌ Cancelled — nothing was executed.", view=self)

    return Approval


def build_approval_view(cfg, plan, owner_id):
    return approval_view(owner_id)(cfg, plan)


def discord_channel_ok(cfg, channel_id):
    return str(channel_id) in {str(x) for x in cfg["discord"].get("channel_ids", [])}


def allowed_user(cfg, user_id):
    allowed = cfg["discord"].get("allowed_user_ids", [])
    return not allowed or str(user_id) in {str(x) for x in allowed}


async def handle_discord_message(cfg, message):
    if message.author.bot or message.guild is None or not discord_channel_ok(cfg, message.channel.id) or not allowed_user(cfg, message.author.id):
        return
    text = message.content.strip()
    if not text or text.startswith("/"):
        return
    session = load_session(f"discord:{message.guild.id}:{message.channel.id}:{message.author.id}")
    thinking = await message.reply("💭 Thinking...", mention_author=False)
    try:
        add(session, "user", text)
        if looks_like_composio_action(text):
            plan = await asyncio.to_thread(plan_composio_action, cfg, text)
            preview = "\n".join(f"{i}. {step.get('description', step.get('tool', 'unknown'))}" for i, step in enumerate(plan.get("steps", []), 1))
            await thinking.delete()
            await message.reply("🛠️ **API action ready**\n" + preview + "\n\nNothing will execute until you approve.", view=build_approval_view(cfg, plan, message.author.id), mention_author=False)
            save_session(session)
            return
        if looks_like_web_research(text):
            topics = [text]
            results = await asyncio.to_thread(lambda: composio_research(text))
            answer = "\n".join(f"• {x['title']}\n{x['url']}" for x in results[:8]) or "No useful web results found."
            await thinking.delete()
            await message.reply(answer[:1900], mention_author=False)
            add(session, "assistant", answer)
            save_session(session)
            return
        answer = await asyncio.to_thread(send, cfg, session)
        add(session, "assistant", answer)
        save_session(session)
        await asyncio.to_thread(refresh_memory, cfg, session)
        await thinking.delete()
        await message.reply(answer[:1900], mention_author=False)
    except Exception as exc:
        log_error(exc)
        try:
            await thinking.delete()
        except Exception:
            pass
        await message.reply("❌ " + short(exc), mention_author=False)


def start_discord(cfg):
    if discord is None:
        return
    token = cfg["discord"].get("token")
    if not token:
        return
    intents = discord.Intents.default()
    intents.message_content = True
    bot = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(bot)

    @tree.command(name="this-channel", description="Set this channel for the bot")
    async def this_channel(interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if not perms or not (perms.manage_channels or perms.administrator):
            await interaction.response.send_message("Manage Channels or Administrator required.", ephemeral=True)
            return
        cfg["discord"]["channel_ids"] = [str(interaction.channel_id)]
        persist(cfg)
        await interaction.response.send_message("✅ This is now the bot channel.")

    @tree.command(name="status", description="Show bot status")
    async def status(interaction):
        await interaction.response.send_message(f"{cfg['selected_provider']} / {cfg['selected_model']} | {cfg['mode']}", ephemeral=True)

    @tree.command(name="help", description="Show commands")
    async def help_cmd(interaction):
        await interaction.response.send_message("Normal messages work here. Explicit API/Composio requests are researched only to resolve the action, then require approval before execution.", ephemeral=True)

    @bot.event
    async def on_ready():
        try:
            await tree.sync()
        except Exception as exc:
            log_error(exc)

    @bot.event
    async def on_message(message):
        await handle_discord_message(cfg, message)

    bot.run(token)


def console_chat(cfg):
    session = new_session("console")
    while True:
        try:
            text = input(f"you@tab [{cfg['selected_provider']} {cfg['selected_model']} | {cfg['mode']}] > ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if not text:
            continue
        if text == "/quit":
            return
        if text == "/new":
            session = new_session("console")
            continue
        if looks_like_composio_action(text):
            try:
                plan = plan_composio_action(cfg, text)
                for i, step in enumerate(plan.get("steps", []), 1):
                    print(f"{i}. {step.get('description', step.get('tool', 'unknown'))}")
                if input("Approve API action? [y/N] ").strip().lower() == "y":
                    for ok, result in execute_composio(cfg, plan):
                        print(("✅" if ok else "❌") + " " + result)
            except Exception as exc:
                print("❌ " + short(exc))
            continue
        add(session, "user", text)
        try:
            answer = send(cfg, session)
            add(session, "assistant", answer)
            save_session(session)
            refresh_memory(cfg, session)
            print("assistant> " + answer)
        except Exception as exc:
            print("❌ " + short(exc))


def main():
    cfg = load_config()
    if cfg["discord"].get("token") and cfg["discord"].get("autostart", True):
        threading.Thread(target=start_discord, args=(cfg,), daemon=True).start()
    console_chat(cfg)

if __name__ == "__main__":
    main()
