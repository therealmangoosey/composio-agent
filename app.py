#!/usr/bin/env python3
"""Tab Assistant: a Termux terminal assistant and an optional locked Discord bot.

Only standard library, requests, openai, and (optionally) discord.py/Composio are used.
Keys and conversations stay in data/ beside this file.
"""
import readline  # Must be first: Termux input history / arrow keys.
import argparse
import csv
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

import requests
from openai import (OpenAI, APIConnectionError, APIStatusError,
                    AuthenticationError, RateLimitError)

ROOT = Path(os.path.dirname(os.path.abspath(__file__)))
DATA = ROOT / "data"
CONFIG_FILE, MEMORY_FILE, ERROR_FILE = (DATA / "config.json", DATA / "memory.json",
                                        DATA / "errors.log")
ENV_FILE = ROOT / ".env"
PROVIDERS = {
    "GROQ": {"base_url": "https://api.groq.com/openai/v1", "free": True,
             "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                        "meta-llama/llama-4-scout-17b-16e-instruct", "openai/gpt-oss-120b",
                        "qwen/qwen3-32b"]},
    "Google Gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "free": True,
                       "models": ["gemini-2.5-flash", "gemini-2.5-flash-lite"]},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "free": False,
               "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"]},
    "OpenRouter": {"base_url": "https://openrouter.ai/api/v1", "free": True,
                   "models": ["meta-llama/llama-3.3-70b-instruct:free", "openai/gpt-oss-120b:free",
                              "deepseek/deepseek-r1:free", "google/gemma-3-27b-it:free",
                              "qwen/qwen3-32b:free", "openai/gpt-5.4-nano:free"]},
    "Cerebras": {"base_url": "https://api.cerebras.ai/v1", "free": True,
                 "models": ["llama3.1-8b", "gpt-oss-120b"]},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "free": False,
                 "models": ["deepseek-chat", "deepseek-v4-flash"]},
    "xAI Grok": {"base_url": "https://api.x.ai/v1", "free": False, "models": ["grok-4.6"]},
}
DEFAULT_SYSTEM = ("You are Tab Assistant, a careful personal AI assistant. Be concise and helpful. "
                  "Never claim an external action happened unless a tool result confirms it.")

def p(text=""):
    print(text, flush=True)

def clear():
    print("\033[2J\033[H", end="", flush=True)

def now(): return dt.datetime.now().isoformat(timespec="seconds")
def mask(key): return "(none)" if not key else (key[:4] + "…" + key[-4:] if len(key) > 9 else "…")
def short(text, n=120): return re.sub(r"\s+", " ", str(text))[:n]

def load_dotenv():
    """Tiny dependency-free .env reader: secrets never go in config.json."""
    if not ENV_FILE.exists(): return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))

def save_dotenv(values):
    old = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1); old[name.strip()] = value
    old.update({k: str(v) for k, v in values.items()})
    ENV_FILE.write_text("# Tab Assistant secrets — do not commit this file.\n" +
                        "\n".join("%s=%s" % x for x in sorted(old.items())) + "\n", encoding="utf-8")
    os.environ.update({k: str(v) for k, v in values.items()})

def env_name(provider):
    names = {"GROQ": "GROQ", "Google Gemini": "GEMINI", "OpenAI": "OPENAI",
             "OpenRouter": "OPENROUTER", "Cerebras": "CEREBRAS", "DeepSeek": "DEEPSEEK", "xAI Grok": "XAI"}
    return names[provider] + "_API_KEYS"

def hydrate_secrets(cfg):
    for provider, info in cfg["providers"].items():
        keys = [x.strip() for x in os.getenv(env_name(provider), "").split(",") if x.strip()]
        existing = info.get("keys", [])
        flags = [x.get("free", PROVIDERS[provider]["free"]) for x in existing]
        info["keys"] = [{"key": key, "free": flags[i] if i < len(flags) else PROVIDERS[provider]["free"]}
                        for i, key in enumerate(keys)]
    cfg["composio"]["api_key"] = os.getenv("COMPOSIO_API_KEY", "")
    cfg["discord"]["token"] = os.getenv("DISCORD_BOT_TOKEN", "")
    cfg["discord"]["allowed_channel_id"] = os.getenv("DISCORD_ALLOWED_CHANNEL_ID", cfg["discord"].get("allowed_channel_id", ""))

def secret_values(cfg):
    values = {env_name(p): ",".join(x["key"] for x in info.get("keys", [])) for p, info in cfg["providers"].items()}
    values.update({"COMPOSIO_API_KEY": cfg["composio"].get("api_key", ""),
                   "DISCORD_BOT_TOKEN": cfg["discord"].get("token", ""),
                   "DISCORD_ALLOWED_CHANNEL_ID": cfg["discord"].get("allowed_channel_id", "")})
    return values

def log_error(exc):
    DATA.mkdir(exist_ok=True)
    with ERROR_FILE.open("a", encoding="utf-8") as f:
        f.write("\n[" + now() + "]\n" + traceback.format_exc() + "\n")

def default_config():
    return {"providers": {name: {"base_url": x["base_url"], "models": x["models"], "keys": []}
                          for name, x in PROVIDERS.items()},
            "selected_provider": "GROQ", "selected_model": PROVIDERS["GROQ"]["models"][0],
            "mode": "free", "last_working": {}, "failures": {}, "composio": {"api_key": "",
            "toolkits": ["GMAIL", "WEB_SEARCH", "NEWS"], "user_id": "tab-owner"},
            "discord": {"token": "", "allowed_channel_id": "", "allowed_user_ids": []},
            "settings": {"system_prompt": DEFAULT_SYSTEM, "max_history": 12, "streaming": True,
                         "temperature": 0.7, "require_approval": True, "cooldown_seconds": 60}}

def load_config():
    DATA.mkdir(exist_ok=True)
    load_dotenv()
    if not CONFIG_FILE.exists(): return None
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        base = default_config()
        for k, v in base.items(): cfg.setdefault(k, v)
        for name, v in base["providers"].items(): cfg["providers"].setdefault(name, v)
        last = cfg.get("last_working", {})
        if last.get("provider") in cfg["providers"] and last.get("model"):
            cfg["selected_provider"], cfg["selected_model"] = last["provider"], last["model"]
        hydrate_secrets(cfg)
        return cfg
    except Exception:
        p("Config is invalid. Rename data/config.json and restart."); return None

def save_config(cfg):
    DATA.mkdir(exist_ok=True)
    save_dotenv(secret_values(cfg))
    public = copy.deepcopy(cfg)
    for info in public["providers"].values():
        info["keys"] = [{"free": x.get("free", False)} for x in info.get("keys", [])]
    public["composio"]["api_key"] = ""
    public["discord"]["token"] = ""
    public["discord"]["allowed_channel_id"] = ""
    public["last_working"].pop("key", None)
    CONFIG_FILE.write_text(json.dumps(public, indent=2), encoding="utf-8")

def load_memory():
    if not MEMORY_FILE.exists(): return {"sessions": []}
    try: return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception: return {"sessions": []}

def save_memory(mem):
    DATA.mkdir(exist_ok=True); MEMORY_FILE.write_text(json.dumps(mem, indent=2), encoding="utf-8")

def wizard():
    clear(); p("=== Tab Assistant setup ===\nLeave any value blank to skip it. /quit always exits chat.\n")
    cfg = default_config()
    key = input("Composio API key (optional): ").strip()
    if key:
        cfg["composio"]["api_key"] = key
        try:
            r = requests.get("https://backend.composio.dev/api/v3/auth/session/info",
                             headers={"x-api-key": key}, timeout=8)
            p("Composio key looks valid." if r.ok else "Composio validation failed; saved anyway.")
        except requests.RequestException: p("Could not validate Composio; saved anyway.")
    cfg["discord"]["token"] = input("Discord bot token (optional): ").strip()
    if cfg["discord"]["token"]:
        cfg["discord"]["allowed_channel_id"] = input("Allowed Discord channel ID: ").strip()
    for name in PROVIDERS:
        if input("Add a key for %s now? [y/N] " % name).strip().lower() == "y":
            key = input("  API key: ").strip()
            if key:
                free = input("  Is this key FREE tier? [Y/n] ").strip().lower() != "n"
                cfg["providers"][name]["keys"].append({"key": key, "free": free})
    mode = input("Default mode Manual / Free / Auto [Free]: ").strip().lower() or "free"
    cfg["mode"] = mode if mode in ("manual", "free", "auto") else "free"
    save_config(cfg); p("\nSetup complete. Everything can be changed later from the menu.")
    return cfg

def failure_id(provider, key):
    return provider + ":" + hashlib.sha256(key.encode()).hexdigest()[:16]
def health(cfg, provider, key):
    f = cfg["failures"].get(failure_id(provider, key), {})
    until = f.get("cooldown_until", 0)
    if until > time.time(): return "rate-limited, retry after " + time.strftime("%H:%M", time.localtime(until))
    return "last error: " + short(f["last_error"], 55) if f.get("last_error") else "OK"

def mark_failure(cfg, provider, key, exc):
    text = short(exc); auth = isinstance(exc, AuthenticationError) or getattr(exc, "status_code", 0) == 401
    seconds = 600 if auth else cfg["settings"].get("cooldown_seconds", 60)
    response = getattr(exc, "response", None)
    if response is not None:
        try: seconds = max(seconds, int(response.headers.get("retry-after", 0)))
        except (ValueError, TypeError): pass
    fid = failure_id(provider, key); old = cfg["failures"].get(fid, {})
    cfg["failures"][fid] = {"fail_count": old.get("fail_count", 0) + 1,
                            "cooldown_until": time.time() + seconds, "last_error": text}
    save_config(cfg)

def mark_success(cfg, provider, model, key):
    cfg["last_working"] = {"provider": provider, "model": model, "key": key}
    cfg["failures"].pop(failure_id(provider, key), None); save_config(cfg)

def candidates(cfg, mode):
    out = []
    for provider, info in cfg["providers"].items():
        for item in info.get("keys", []):
            if mode == "free" and not item.get("free"): continue
            for model in info.get("models", []): out.append((provider, model, item["key"]))
    last = cfg.get("last_working", {})
    out.sort(key=lambda x: 0 if x[:2] == (last.get("provider"), last.get("model")) else 1)
    return out

def get_client(cfg, provider, key): return OpenAI(api_key=key, base_url=cfg["providers"][provider]["base_url"])
def friendly(provider, exc, mode):
    status = getattr(exc, "status_code", 0)
    if isinstance(exc, RateLimitError) or status == 429:
        return "Rate limited — switching to next free key…" if mode != "manual" else "Rate limited — wait and try again."
    if isinstance(exc, AuthenticationError) or status == 401: return "❌ %s rejected your API key — check it in menu 5" % provider
    if isinstance(exc, APIConnectionError): return "❌ Can't reach the internet — check Wi-Fi/mobile data"
    if isinstance(exc, APIStatusError): return "❌ %s error %s: %s" % (provider, status, short(exc))
    return "❌ Request failed: " + short(exc)

def messages_for(cfg, session):
    n = int(cfg["settings"].get("max_history", 12))
    return [{"role": "system", "content": cfg["settings"]["system_prompt"]}] + [
        {"role": m["role"], "content": m["content"]} for m in session["messages"][-n:]]

def send_with_failover(cfg, session, stream=None, force_json=False):
    mode = cfg["mode"]; stream = cfg["settings"].get("streaming", True) if stream is None else stream
    if mode == "manual":
        provider, model = cfg["selected_provider"], cfg["selected_model"]
        keys = cfg["providers"].get(provider, {}).get("keys", [])
        choices = [(provider, model, keys[0]["key"])] if keys else []
    else:
        choices = candidates(cfg, "free" if mode == "free" else "auto")
    if not choices: raise RuntimeError("No usable API keys. Add one in menu 5.")
    last_exc = None
    for provider, model, key in choices:
        if cfg["failures"].get(failure_id(provider, key), {}).get("cooldown_until", 0) > time.time(): continue
        try:
            args = {"model": model, "messages": messages_for(cfg, session),
                    "temperature": cfg["settings"].get("temperature", .7), "stream": stream}
            if force_json: args["response_format"] = {"type": "json_object"}
            response = get_client(cfg, provider, key).chat.completions.create(**args)
            if stream:
                text = ""
                for chunk in response:
                    bit = chunk.choices[0].delta.content or ""; text += bit; print(bit, end="", flush=True)
                print(flush=True)
            else: text = response.choices[0].message.content or ""
            if not text.strip(): raise RuntimeError("The model returned nothing — try again.")
            mark_success(cfg, provider, model, key); return text, provider, model
        except Exception as exc:
            log_error(exc); last_exc = exc; mark_failure(cfg, provider, key, exc)
            p(friendly(provider, exc, mode))
            if mode == "manual": break
    raise RuntimeError(friendly("Assistant", last_exc, mode) if last_exc else "All keys are cooling down.")

def new_session(): return {"id": uuid.uuid4().hex, "created": now(), "messages": []}
def add_message(session, role, content, provider="", model=""):
    session["messages"].append({"role": role, "content": content, "provider": provider, "model": model, "time": now()})

def chat_loop(cfg, session=None):
    session = session or new_session(); p("Chat: /help, /new, /model NAME, /provider NAME, /mode MODE, /history, /quit")
    while True:
        try: prompt = "you@tab [%s %s | %s] > " % (cfg["selected_provider"], cfg["selected_model"], cfg["mode"]); text = input(prompt).strip()
        except KeyboardInterrupt: p("\nBack to menu."); return session
        if not text: continue
        if text == "/quit": return session
        if text == "/new": session = new_session(); p("New conversation."); continue
        if text == "/history":
            for m in session["messages"][-12:]: p("%s: %s" % (m["role"], short(m["content"], 160)))
            continue
        if text == "/help": p("/new clears chat; /quit returns to menu; provider/model/mode set options."); continue
        if text.startswith("/provider "):
            name = text[10:].strip();
            if name in cfg["providers"]: cfg["selected_provider"] = name; cfg["selected_model"] = cfg["providers"][name]["models"][0]; save_config(cfg)
            else: p("Unknown provider.")
            continue
        if text.startswith("/model "): cfg["selected_model"] = text[7:].strip(); save_config(cfg); continue
        if text.startswith("/mode "):
            x = text[6:].strip().lower();
            if x in ("manual", "free", "auto"): cfg["mode"] = x; save_config(cfg)
            else: p("Use manual, free, or auto.")
            continue
        add_message(session, "user", text)
        try:
            p("assistant> "); answer, provider, model = send_with_failover(cfg, session)
            add_message(session, "assistant", answer, provider, model)
        except Exception as exc: p("❌ " + short(exc))
        mem = load_memory(); mem["sessions"] = [x for x in mem["sessions"] if x["id"] != session["id"]] + [session]; save_memory(mem)

def manage_keys(cfg):
    while True:
        clear(); p("=== API keys ===")
        for name, info in cfg["providers"].items():
            keys = info.get("keys", [])
            p(name + ": " + (", ".join("%s [%s] %s" % (mask(k["key"]), "FREE" if k.get("free") else "PAID", health(cfg, name, k["key"])) for k in keys) or "none"))
        p("\n1 Add  2 Replace  3 Delete  4 Toggle free/paid  0 Back")
        action = input("> ").strip()
        if action == "0": return
        name = input("Provider (exact name): ").strip()
        if name not in cfg["providers"]: p("Unknown provider."); input("Enter to continue"); continue
        keys = cfg["providers"][name]["keys"]
        if action == "1":
            key = input("Key: ").strip()
            if key: keys.append({"key": key, "free": input("Free? [Y/n] ").strip().lower() != "n"})
        else:
            try: i = int(input("Key number (0-based): "))
            except ValueError: continue
            if not 0 <= i < len(keys): continue
            if action == "2": keys[i]["key"] = input("Replacement key: ").strip() or keys[i]["key"]
            elif action == "3": keys.pop(i)
            elif action == "4": keys[i]["free"] = not keys[i].get("free")
        save_config(cfg)

def local_tool(tool, args):
    DATA.mkdir(exist_ok=True)
    if tool == "ADD_TO_LIST":
        path = DATA / "leads.csv"; exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "email", "notes"])
            if not exists: w.writeheader()
            w.writerow({k: args.get(k, "") for k in ("name", "email", "notes")})
        return "added %s to leads.csv" % args.get("name", "item")
    if tool == "READ_LIST": return (DATA / "leads.csv").read_text(encoding="utf-8") if (DATA / "leads.csv").exists() else "leads list is empty"
    if tool == "SAVE_NOTE":
        with (DATA / "notes.txt").open("a", encoding="utf-8") as f: f.write("[" + now() + "] " + args.get("text", "") + "\n")
        return "note saved"
    if tool == "READ_NOTE": return (DATA / "notes.txt").read_text(encoding="utf-8") if (DATA / "notes.txt").exists() else "no notes yet"
    raise RuntimeError("Unsupported local tool: " + tool)

PLAN_PROMPT = """Return JSON only: {\"summary\":string,\"steps\":[{\"tool\":string,\"description\":string,\"args\":object}]}.
You can use local tools ADD_TO_LIST(name,email,notes), READ_LIST, SAVE_NOTE(text), READ_NOTE.
For Composio actions use their action name and fully specified args. Make a safe, exact plan; never execute it."""
def build_plan(cfg, request):
    s = {"messages": [{"role": "user", "content": PLAN_PROMPT + "\nRequest: " + request}]}
    try: raw, _, _ = send_with_failover(cfg, s, stream=False, force_json=True)
    except Exception: raw, _, _ = send_with_failover(cfg, s, stream=False)
    match = re.search(r"\{.*\}", raw, re.S); plan = json.loads(match.group(0) if match else raw)
    if not isinstance(plan.get("steps"), list): raise ValueError("No steps in plan")
    return plan

def execute_plan(cfg, plan):
    results = []
    for step in plan["steps"]:
        try:
            tool, args = step.get("tool", ""), step.get("args", {})
            if tool in ("ADD_TO_LIST", "READ_LIST", "SAVE_NOTE", "READ_NOTE"): result = local_tool(tool, args)
            else: result = composio_tool(cfg, tool, args)
            results.append((True, result))
        except Exception as exc: log_error(exc); results.append((False, short(exc)))
    return results

def composio_tool(cfg, action, args):
    """SDK versions change; all optional Composio imports are deliberately isolated here."""
    if not cfg["composio"].get("api_key"): raise RuntimeError("Composio API key is not configured")
    try:
        from composio import Composio
        c = Composio(api_key=cfg["composio"]["api_key"])
        # Current SDK action invocation may vary by toolkit; expose an explicit safe error if unavailable.
        if hasattr(c, "tools") and hasattr(c.tools, "execute"):
            return str(c.tools.execute(user_id=cfg["composio"]["user_id"], action=action, arguments=args))
        raise RuntimeError("Composio installed, but this SDK needs its action adapter updated")
    except ImportError:
        raise RuntimeError("Composio not installed — run: pip install composio composio-openai")

def tools_menu(cfg):
    p("\nComposio is optional. Local list and note tools always work.")
    p("1 Build/run a plan  2 Open Composio connection help  0 Back")
    x = input("> ").strip()
    if x == "1":
        try:
            plan = build_plan(cfg, input("Request: ")); p("\n📋 " + plan.get("summary", "Plan"))
            for i, step in enumerate(plan["steps"], 1): p("%d. %s" % (i, step.get("description", step.get("tool"))))
            if not cfg["settings"].get("require_approval") or input("Run? [y/N] ").strip().lower() == "y":
                for i, (ok, result) in enumerate(execute_plan(cfg, plan), 1): p(("✅" if ok else "❌") + " %d/%d — %s" % (i, len(plan["steps"]), result))
            else: p("Plan cancelled — nothing was executed.")
        except Exception as exc: log_error(exc); p("Couldn't build a plan from that — try rephrasing.")
    elif x == "2": p("Install composio composio-openai, then use its connection flow for Gmail. Auth links are SDK-version dependent.")

def choose(cfg, kind):
    if kind == "provider":
        for i, x in enumerate(cfg["providers"], 1): p("%d. %s" % (i, x))
        name = input("Provider: ").strip()
        if name in cfg["providers"]: cfg["selected_provider"] = name; cfg["selected_model"] = cfg["providers"][name]["models"][0]
    elif kind == "model":
        for x in cfg["providers"][cfg["selected_provider"]]["models"]: p("- " + x)
        cfg["selected_model"] = input("Model (or paste a new ID): ").strip() or cfg["selected_model"]
    else:
        x = input("manual / free / auto: ").strip().lower()
        if x in ("manual", "free", "auto"): cfg["mode"] = x
    save_config(cfg)

def settings(cfg):
    s = cfg["settings"]; p("1 System prompt  2 History count  3 Streaming  4 Temperature  5 Approval  6 Fetch free OpenRouter models  0 Back")
    x = input("> ").strip()
    try:
        if x == "1": s["system_prompt"] = input("System prompt: ").strip() or s["system_prompt"]
        elif x == "2": s["max_history"] = int(input("Last messages: "))
        elif x == "3": s["streaming"] = not s["streaming"]
        elif x == "4": s["temperature"] = float(input("Temperature: "))
        elif x == "5": s["require_approval"] = not s["require_approval"]
        elif x == "6":
            data = requests.get("https://openrouter.ai/api/v1/models", timeout=15).json()["data"]
            models = [m["id"] for m in data if ":free" in m["id"] or str(m.get("pricing", {}).get("prompt")) == "0"]
            cfg["providers"]["OpenRouter"]["models"] = models; p("Saved %d free model IDs." % len(models))
    except Exception as exc: p("Could not update setting: " + short(exc))
    save_config(cfg)

def history_menu(cfg):
    mem = load_memory(); sessions = mem["sessions"]
    for i, s in enumerate(sessions, 1): p("%d. %s | %s | %d messages" % (i, s.get("created"), short(s.get("messages", [{}])[0].get("content", "empty"), 45), len(s.get("messages", []))))
    x = input("Resume number, d NUMBER delete, c clear all, Enter back: ").strip()
    if x.isdigit() and 0 < int(x) <= len(sessions): chat_loop(cfg, sessions[int(x)-1])
    elif x.startswith("d ") and x[2:].isdigit(): sessions.pop(int(x[2:])-1); save_memory(mem)
    elif x == "c" and input("Delete all history? [y/N] ").lower() == "y": save_memory({"sessions": []})

def main_menu(cfg):
    while True:
        clear(); p("=== Tab Assistant | %s / %s | %s ===" % (cfg["selected_provider"], cfg["selected_model"], cfg["mode"]))
        p("1. Start / continue chat       6. Memory & history\n2. Choose provider             7. Composio tools & connections\n3. Choose model                8. Discord bot\n4. Choose mode                 9. Settings\n5. Manage API keys             0. Exit")
        try: x = input("\nChoose: ").strip()
        except KeyboardInterrupt: p("\nBye."); return
        if x == "0": return
        if x == "1": chat_loop(cfg)
        elif x == "2": choose(cfg, "provider")
        elif x == "3": choose(cfg, "model")
        elif x == "4": choose(cfg, "mode")
        elif x == "5": manage_keys(cfg)
        elif x == "6": history_menu(cfg)
        elif x == "7": tools_menu(cfg)
        elif x == "8": p("Run this in another Termux/tmux session: python app.py --bot"); input("Enter")
        elif x == "9": settings(cfg)

def run_bot(cfg):
    try: import discord
    except ImportError: p("Discord bot needs: pip install discord.py"); return
    token, allowed = cfg["discord"].get("token"), str(cfg["discord"].get("allowed_channel_id", ""))
    if not token or not allowed: p("Set Discord token and allowed channel ID in data/config.json first."); return
    intents = discord.Intents.default(); bot = discord.Client(intents=intents); tree = discord.app_commands.CommandTree(bot); plans = {}
    discord_session = new_session()
    def allowed_interaction(interaction):
        return interaction.guild is not None and str(interaction.channel_id) == allowed and (not cfg["discord"].get("allowed_user_ids") or interaction.user.id in cfg["discord"]["allowed_user_ids"])
    async def guard(interaction):
        if allowed_interaction(interaction): return True
        await interaction.response.send_message("🔒 I only work in the configured channel.", ephemeral=True); return False
    class PlanView(discord.ui.View):
        def __init__(self, pid, owner): super().__init__(timeout=300); self.pid, self.owner = pid, owner
        async def finish(self, interaction, message):
            for child in self.children: child.disabled = True
            await interaction.message.edit(view=self); await interaction.followup.send(message)
        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
        async def confirm(self, interaction, button):
            if not allowed_interaction(interaction) or interaction.user.id != self.owner: await interaction.response.send_message("Not your plan.", ephemeral=True); return
            await interaction.response.defer(); plan = plans.pop(self.pid, None)
            if not plan: await self.finish(interaction, "Plan expired — nothing was executed."); return
            for i, (ok, result) in enumerate(execute_plan(cfg, plan), 1): await interaction.followup.send(("✅" if ok else "❌") + " %d/%d — %s" % (i, len(plan["steps"]), result))
            await self.finish(interaction, "Plan completed.")
        @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
        async def deny(self, interaction, button):
            if interaction.user.id != self.owner: await interaction.response.send_message("Not your plan.", ephemeral=True); return
            await interaction.response.defer(); plans.pop(self.pid, None); await self.finish(interaction, "Plan cancelled — nothing was executed.")
        async def on_timeout(self): plans.pop(self.pid, None); [setattr(c, "disabled", True) for c in self.children]
    @tree.command(name="chat", description="Chat without tools")
    async def chat(interaction, message: str):
        if not await guard(interaction): return
        await interaction.response.defer(thinking=True)
        try:
            add_message(discord_session, "user", message)
            answer, provider, model = send_with_failover(cfg, discord_session, stream=False)
            add_message(discord_session, "assistant", answer, provider, model)
            await interaction.followup.send(answer[:1900])
        except Exception as exc: log_error(exc); await interaction.followup.send("❌ " + short(exc), ephemeral=True)
    @tree.command(name="plan", description="Build an approval-gated action plan")
    async def plan(interaction, request: str):
        if not await guard(interaction): return
        await interaction.response.defer(thinking=True)
        try:
            plan_data = build_plan(cfg, request); pid = uuid.uuid4().hex; plans[pid] = plan_data
            text = "📋 Plan (%d steps)\n" % len(plan_data["steps"]) + "\n".join("%d. %s" % (i, s.get("description", s.get("tool"))) for i, s in enumerate(plan_data["steps"], 1))
            await interaction.followup.send(text[:1900], view=PlanView(pid, interaction.user.id))
        except Exception as exc: log_error(exc); await interaction.followup.send("Couldn't build a plan from that — try rephrasing.", ephemeral=True)
    @tree.command(name="status", description="Show active model and key health")
    async def status(interaction):
        if not await guard(interaction): return
        lines = ["%s: %s" % (n, ", ".join(health(cfg, n, k["key"]) for k in v["keys"]) or "no key") for n, v in cfg["providers"].items()]
        await interaction.response.send_message("%s / %s | %s\n" % (cfg["selected_provider"], cfg["selected_model"], cfg["mode"]) + "\n".join(lines), ephemeral=True)
    @tree.command(name="provider", description="Select a provider")
    async def provider_cmd(interaction, name: str):
        if not await guard(interaction): return
        if name not in cfg["providers"]: await interaction.response.send_message("Unknown provider.", ephemeral=True); return
        cfg["selected_provider"] = name; cfg["selected_model"] = cfg["providers"][name]["models"][0]; save_config(cfg)
        await interaction.response.send_message("Provider set to " + name, ephemeral=True)
    @tree.command(name="model", description="Select a model ID")
    async def model_cmd(interaction, name: str):
        if not await guard(interaction): return
        cfg["selected_model"] = name; save_config(cfg); await interaction.response.send_message("Model set to " + name, ephemeral=True)
    @tree.command(name="mode", description="Set manual, free, or auto mode")
    async def mode_cmd(interaction, name: str):
        if not await guard(interaction): return
        name = name.lower()
        if name not in ("manual", "free", "auto"): await interaction.response.send_message("Use manual, free, or auto.", ephemeral=True); return
        cfg["mode"] = name; save_config(cfg); await interaction.response.send_message("Mode set to " + name, ephemeral=True)
    @tree.command(name="history", description="Show recent Discord conversation")
    async def history_cmd(interaction):
        if not await guard(interaction): return
        text = "\n".join("%s: %s" % (m["role"], short(m["content"], 200)) for m in discord_session["messages"][-10:]) or "No conversation yet."
        await interaction.response.send_message(text[:1900], ephemeral=True)
    @tree.command(name="clear", description="Clear Discord conversation")
    async def clear_cmd(interaction):
        nonlocal discord_session
        if not await guard(interaction): return
        discord_session = new_session(); await interaction.response.send_message("Conversation cleared.", ephemeral=True)
    @tree.command(name="help", description="Show commands")
    async def help_cmd(interaction):
        if await guard(interaction): await interaction.response.send_message("/chat, /plan, /status, /model, /provider, /mode, /history, /clear. Plans never execute until their owner confirms.", ephemeral=True)
    @bot.event
    async def on_ready():
        await tree.sync(); p("Discord ready as %s; locked to channel %s" % (bot.user, allowed))
    try: bot.run(token, reconnect=True)
    except KeyboardInterrupt: p("Bot stopped.")
    except Exception as exc: log_error(exc); p("Discord bot could not start: " + short(exc))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--bot", action="store_true"); ap.add_argument("--debug", action="store_true"); args = ap.parse_args()
    try:
        cfg = load_config() or wizard()
        if args.bot: run_bot(cfg)
        else: main_menu(cfg)
    except KeyboardInterrupt: p("\nStopped safely.")
    except Exception as exc:
        log_error(exc); p("❌ Unexpected error. Details were saved to data/errors.log.")
        if args.debug: traceback.print_exc()
if __name__ == "__main__": main()
