#!/usr/bin/env python3
"""Termux-friendly Tab Assistant with Discord, Composio and parallel research."""
import copy
import csv
import datetime as dt
import json
import os
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_FILE = DATA / "config.json"
MEMORY_FILE = DATA / "memory.json"
ERROR_FILE = DATA / "errors.log"
ENV_FILE = ROOT / ".env"

DEFAULT_SYSTEM = (
    "You are Tab Assistant, a concise and capable personal AI assistant. "
    "Never claim an external action happened unless a tool result confirms it. "
    "Composio tools are available when configured. Use the tool planner for external actions. "
    "For independent research requests, split work into parallel research tasks and synthesize results."
)

PROVIDERS = {
    "GROQ": {"base_url": "https://api.groq.com/openai/v1", "models": ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], "free": True},
    "Google Gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "models": ["gemini-2.5-flash", "gemini-2.5-flash-lite"], "free": True},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "models": ["gpt-4o-mini"], "free": False},
    "OpenRouter": {"base_url": "https://openrouter.ai/api/v1", "models": ["openrouter/free"], "free": True},
    "Cerebras": {"base_url": "https://api.cerebras.ai/v1", "models": ["gpt-oss-120b"], "free": True},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "models": ["deepseek-chat"], "free": False},
    "xAI Grok": {"base_url": "https://api.x.ai/v1", "models": ["grok-4.1-mini"], "free": False},
}
ENV_NAMES = {"GROQ": "GROQ_API_KEYS", "Google Gemini": "GEMINI_API_KEYS", "OpenAI": "OPENAI_API_KEYS", "OpenRouter": "OPENROUTER_API_KEYS", "Cerebras": "CEREBRAS_API_KEYS", "DeepSeek": "DEEPSEEK_API_KEYS", "xAI Grok": "XAI_API_KEYS"}
MODEL_CACHE, MODEL_CACHE_TTL = {}, 300
RESEARCH_MAX_AGENTS = 4


def now(): return dt.datetime.now().isoformat(timespec="seconds")
def short(value, limit=180): return re.sub(r"\s+", " ", str(value)).strip()[:limit]
def p(text=""): print(text, flush=True)

def log_error(exc):
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        with ERROR_FILE.open("a", encoding="utf-8") as f: f.write(f"\n[{now()}]\n{traceback.format_exc()}\n")
    except OSError: pass


def load_dotenv():
    if not ENV_FILE.exists(): return
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            key, value = line.split("=", 1); value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'": value = value[1:-1]
            os.environ.setdefault(key.strip(), value)
    except OSError: pass


def save_dotenv(values):
    existing = {}
    if ENV_FILE.exists():
        try:
            for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if "=" in raw and not raw.lstrip().startswith("#"):
                    key, value = raw.split("=", 1); existing[key.strip()] = value
        except OSError: pass
    existing.update({k: str(v) for k, v in values.items() if v is not None})
    ENV_FILE.write_text("# Tab Assistant secrets — do not commit this file.\n" + "\n".join(f"{k}={v}" for k, v in sorted(existing.items()) if v != "") + "\n", encoding="utf-8")
    try: os.chmod(ENV_FILE, 0o600)
    except OSError: pass
    os.environ.update({k: str(v) for k, v in values.items() if v is not None})


def default_config():
    return {
        "providers": {name: {"base_url": d["base_url"], "models": list(d["models"]), "keys": []} for name, d in PROVIDERS.items()},
        "selected_provider": "GROQ", "selected_model": PROVIDERS["GROQ"]["models"][0], "mode": "free",
        "failures": {}, "last_working": {},
        "composio": {"api_key": "", "toolkits": ["GMAIL", "WEB_SEARCH", "NEWS"], "user_id": "tab-owner", "toolkit_version": "latest"},
        "discord": {"token": "", "channel_ids": [], "allowed_user_ids": [], "autostart": True},
        "settings": {"system_prompt": DEFAULT_SYSTEM, "max_history": 8, "temperature": 0.7, "require_approval": True, "max_research_agents": RESEARCH_MAX_AGENTS},
    }


def merge_defaults(cfg):
    base = default_config()
    for key, value in base.items(): cfg.setdefault(key, copy.deepcopy(value))
    for name, value in base["providers"].items():
        current = cfg["providers"].setdefault(name, copy.deepcopy(value)); current.setdefault("base_url", value["base_url"]); current.setdefault("models", list(value["models"])); current.setdefault("keys", [])
    cfg["discord"].setdefault("channel_ids", []); cfg["discord"].setdefault("allowed_user_ids", []); cfg["discord"].setdefault("autostart", True)
    if isinstance(cfg["discord"].get("channel_ids"), str): cfg["discord"]["channel_ids"] = [x.strip() for x in cfg["discord"]["channel_ids"].split(",") if x.strip()]
    if not cfg["discord"]["channel_ids"]:
        legacy = os.getenv("DISCORD_ALLOWED_CHANNEL_ID", "")
        if legacy: cfg["discord"]["channel_ids"] = [x.strip() for x in legacy.split(",") if x.strip()]
    cfg["settings"].setdefault("max_history", 8); cfg["settings"].setdefault("temperature", 0.7); cfg["settings"].setdefault("require_approval", True); cfg["settings"].setdefault("max_research_agents", RESEARCH_MAX_AGENTS)
    return cfg


def hydrate_secrets(cfg):
    load_dotenv()
    for provider, info in cfg["providers"].items():
        raw = os.getenv(ENV_NAMES[provider], ""); keys = [x.strip() for x in raw.split(",") if x.strip()]; old = info.get("keys", []); flags = [bool(x.get("free", PROVIDERS[provider]["free"])) for x in old]
        info["keys"] = [{"key": key, "free": flags[i] if i < len(flags) else PROVIDERS[provider]["free"]} for i, key in enumerate(keys)]
    cfg["composio"]["api_key"] = os.getenv("COMPOSIO_API_KEY", ""); cfg["discord"]["token"] = os.getenv("DISCORD_BOT_TOKEN", "")


def secret_values(cfg):
    values = {ENV_NAMES[name]: ",".join(item["key"] for item in info.get("keys", [])) for name, info in cfg["providers"].items()}
    values.update({"COMPOSIO_API_KEY": cfg["composio"].get("api_key", ""), "DISCORD_BOT_TOKEN": cfg["discord"].get("token", ""), "DISCORD_ALLOWED_CHANNEL_ID": ",".join(cfg["discord"].get("channel_ids", []))})
    return values


def load_config():
    DATA.mkdir(parents=True, exist_ok=True); load_dotenv()
    if not CONFIG_FILE.exists(): return None
    try:
        cfg = merge_defaults(json.loads(CONFIG_FILE.read_text(encoding="utf-8"))); hydrate_secrets(cfg); return cfg
    except (OSError, json.JSONDecodeError, TypeError, KeyError): return None


def save_config(cfg):
    DATA.mkdir(parents=True, exist_ok=True); save_dotenv(secret_values(cfg)); public = copy.deepcopy(cfg)
    for info in public["providers"].values(): info["keys"] = [{"free": bool(item.get("free"))} for item in info.get("keys", [])]
    public["composio"]["api_key"] = ""; public["discord"]["token"] = ""
    CONFIG_FILE.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")


def persist(cfg):
    try: save_config(cfg)
    except Exception as exc: p("Warning: could not save settings: " + short(exc))


def new_session(): return {"id": uuid.uuid4().hex, "created": now(), "messages": []}
def add_message(session, role, content, provider="", model=""): session["messages"].append({"role": role, "content": str(content), "provider": provider, "model": model, "time": now()})

def load_memory():
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8")); return data if isinstance(data, dict) and isinstance(data.get("sessions"), list) else {"sessions": []}
    except (OSError, json.JSONDecodeError): return {"sessions": []}

def save_session(session):
    mem = load_memory(); mem["sessions"] = [x for x in mem["sessions"] if x.get("id") != session.get("id")]; mem["sessions"].append(session); mem["sessions"] = mem["sessions"][-100:]; DATA.mkdir(parents=True, exist_ok=True); MEMORY_FILE.write_text(json.dumps(mem, indent=2) + "\n", encoding="utf-8")


def available_models(cfg, provider, key):
    if not key: return cfg["providers"][provider]["models"]
    cache_key = (provider, key[:12]); cached = MODEL_CACHE.get(cache_key)
    if cached and time.time() - cached["time"] < MODEL_CACHE_TTL: return cached["models"]
    try:
        response = requests.get(cfg["providers"][provider]["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + key}, timeout=8); response.raise_for_status(); data = response.json().get("data", []); models = [str(x["id"]) for x in data if x.get("id")]
        if provider == "OpenRouter":
            models = [str(x["id"]) for x in data if x.get("id") == "openrouter/free" or ":free" in str(x.get("id", "")) or (str((x.get("pricing") or {}).get("prompt")) in {"0", "0.0"} and str((x.get("pricing") or {}).get("completion", (x.get("pricing") or {}).get("output"))) in {"0", "0.0"})] or ["openrouter/free"]
        if models: MODEL_CACHE[cache_key] = {"time": time.time(), "models": models}; return models
    except (requests.RequestException, ValueError, TypeError, KeyError): pass
    return cfg["providers"][provider]["models"]


def candidates(cfg):
    out=[]; last=cfg.get("last_working", {})
    for provider, info in cfg["providers"].items():
        for item in info.get("keys", []):
            if cfg["mode"] == "free" and not item.get("free"): continue
            models=available_models(cfg, provider, item["key"])
            for model in models:
                if cfg["mode"] == "free" and provider == "OpenRouter" and model != "openrouter/free" and ":free" not in model: continue
                out.append((0 if (provider == last.get("provider") and model == last.get("model")) else 1, provider, model, item["key"]))
    out.sort(key=lambda x: x[0]); return [(p,m,k) for _,p,m,k in out]


def request_model(cfg, provider, key, payload):
    response = requests.post(cfg["providers"][provider]["base_url"].rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, json=payload, timeout=45)
    if not response.ok: raise RuntimeError(f"{provider} error {response.status_code}: {short(response.text, 300)}")
    return str(response.json()["choices"][0]["message"].get("content", ""))


def send_message(cfg, session, *, temperature=None, system_prompt=None, force_json=False):
    choices=candidates(cfg)
    if not choices: raise RuntimeError("No usable API keys. Add one in menu 5.")
    messages=[{"role":"system","content":system_prompt or cfg["settings"]["system_prompt"]}] + [{"role":m["role"],"content":m["content"]} for m in session["messages"][-max(1,int(cfg["settings"]["max_history"])):]]
    last_error=None
    for provider,model,key in choices:
        payload={"model":model,"messages":messages,"temperature":float(cfg["settings"]["temperature"] if temperature is None else temperature)}
        if force_json: payload["response_format"]={"type":"json_object"}
        variants=[payload,{k:v for k,v in payload.items() if k!="response_format"},{k:v for k,v in payload.items() if k not in {"response_format","temperature"}}]
        for variant in variants:
            try:
                answer=request_model(cfg,provider,key,variant)
                if not answer.strip(): raise RuntimeError("The model returned an empty response.")
                cfg["last_working"]={"provider":provider,"model":model}; persist(cfg); return answer,provider,model
            except Exception as exc: last_error=exc; log_error(exc); break
    raise last_error or RuntimeError("All providers failed.")


def split_topics(text):
    raw=text.strip()
    if raw.startswith("/research"): raw=raw[len("/research"):].strip()
    raw=re.sub(r"^\s*(research|research on|look into)\s*:?\s*","",raw,flags=re.I)
    parts=[x.strip(" \t\r\n,.;|") for x in re.split(r"\s*(?:,|;|\||\band\b|\n)\s*",raw,flags=re.I) if x.strip()]
    return parts[:RESEARCH_MAX_AGENTS]


def looks_like_research(text): return any(word in text.lower() for word in ("research","research on","look into","compare")) and len(split_topics(text)) >= 2

def research_plan_text(topics): return "🔎 **Research plan**\n" + f"I’ll launch {len(topics)} parallel research agents:\n" + "\n".join(f"{i}. {topic}" for i,topic in enumerate(topics,1)) + "\n\nNothing will run until you approve it."

def research_prompt(topic): return "Research this topic carefully. Return concise factual findings, caveats, and useful source URLs where available. Topic: " + topic


def run_parallel_research(cfg, topics):
    workers=max(1,min(int(cfg["settings"].get("max_research_agents",RESEARCH_MAX_AGENTS)),len(topics)))
    def worker(topic):
        session=new_session(); add_message(session,"user",research_prompt(topic)); answer,provider,model=send_message(cfg,session,temperature=0.2,system_prompt="You are a research worker. Produce factual research notes and never claim browsing unless a configured tool actually browsed."); return topic,answer,provider,model
    results=[]
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="research-agent") as pool:
        futures={pool.submit(worker,topic):topic for topic in topics}
        for future in as_completed(futures):
            topic=futures[future]
            try: results.append((topic,future.result()[1]))
            except Exception as exc: results.append((topic,"❌ "+short(exc,800)))
    return results


def synthesize_research(cfg,results):
    session=new_session(); add_message(session,"user","Synthesize these independent research reports. Keep topics distinct, flag conflicts/uncertainty, and never invent citations.\n\n"+"\n\n".join(f"TOPIC: {t}\n{r}" for t,r in results)); answer,_,_=send_message(cfg,session,temperature=0.2,system_prompt="You are the final research synthesizer. Be accurate, concise and transparent."); return answer


def composio_plan(cfg,request):
    if not cfg["composio"].get("api_key"): raise RuntimeError("Composio API key is not configured.")
    session=new_session(); add_message(session,"user",'Return JSON only: {"steps":[{"tool":"ACTION_SLUG","description":"what it will do","args":{}}]}. Use exact safe args. Never execute. Request: '+request); raw,_,_=send_message(cfg,session,force_json=True); match=re.search(r"\{.*\}",raw,re.S); data=json.loads(match.group(0) if match else raw)
    if not isinstance(data,dict) or not isinstance(data.get("steps"),list): raise RuntimeError("The model did not produce a valid Composio plan.")
    return data


def execute_composio(cfg,plan):
    try: from composio import Composio
    except ImportError as exc: raise RuntimeError("Install Composio with: pip install -U composio") from exc
    client=Composio(api_key=cfg["composio"]["api_key"]); version=cfg["composio"].get("toolkit_version") or "latest"; results=[]
    for step in plan.get("steps",[]):
        args=step.get("args") if isinstance(step.get("args"),dict) else {}
        try: results.append((True,str(client.tools.execute(step.get("tool",""),version=version,arguments=args,user_id=cfg["composio"].get("user_id","tab-owner"))))
        except TypeError: results.append((True,str(client.tools.execute(step.get("tool",""),arguments=args,user_id=cfg["composio"].get("user_id","tab-owner"))))
        except Exception as exc: results.append((False,short(exc,800)))
    return results


def discord_invite(cfg):
    token=cfg["discord"].get("token")
    if not token: return None,"DISCORD_BOT_TOKEN is not configured."
    try:
        r=requests.get("https://discord.com/api/v10/users/@me",headers={"Authorization":"Bot "+token},timeout=10); r.raise_for_status(); app_id=str(r.json()["id"]); return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot%20applications.commands&permissions=84992",None
    except Exception as exc: return None,short(exc)


def add_channel(cfg,cid): cfg["discord"]["channel_ids"]=sorted(set(cfg["discord"].get("channel_ids",[]))|{str(cid)}); persist(cfg)
def set_channel(cfg,cid): cfg["discord"]["channel_ids"]=[str(cid)]; persist(cfg)
def remove_channel(cfg,cid): cfg["discord"]["channel_ids"]=sorted(set(cfg["discord"].get("channel_ids",[]))-{str(cid)}); persist(cfg)


def discord_bot(cfg):
    try: import discord
    except ImportError: p("Discord bot requires: python -m pip install -U discord.py"); return
    token=cfg["discord"].get("token")
    if not token: return
    intents=discord.Intents.default(); intents.message_content=True; bot=discord.Client(intents=intents); tree=discord.app_commands.CommandTree(bot); session=new_session()
    def enabled(chid): return str(chid) in set(cfg["discord"].get("channel_ids",[]))
    def allowed(i): return i.guild is not None and enabled(i.channel_id) and (not cfg["discord"].get("allowed_user_ids") or i.user.id in cfg["discord"]["allowed_user_ids"])
    def can_manage(i):
        perms=getattr(i.user,"guild_permissions",None); return bool(perms and (perms.manage_channels or perms.administrator))
    async def approval(kind,payload,owner,interaction=None,message=None):
        class View(discord.ui.View):
            def __init__(self): super().__init__(timeout=300); self.used=False
            async def check(self,i):
                if i.user.id!=owner: await i.response.send_message("Not your approval.",ephemeral=True); return False
                return True
            async def run(self,i):
                self.used=True
                for c in self.children: c.disabled=True
                await i.response.edit_message(view=self)
                if kind=="research":
                    await i.followup.send(f"🚀 Starting {len(payload)} research agents in parallel...")
                    try:
                        results=await __import__("asyncio").to_thread(run_parallel_research,cfg,payload); answer=await __import__("asyncio").to_thread(synthesize_research,cfg,results); await i.followup.send(answer[:1900])
                    except Exception as exc: await i.followup.send("❌ "+short(exc))
                else:
                    await i.followup.send("🚀 Executing the approved Composio plan...")
                    try:
                        results=await __import__("asyncio").to_thread(execute_composio,cfg,payload); await i.followup.send("\n".join(("✅" if ok else "❌")+" "+r for ok,r in results)[:1900])
                    except Exception as exc: await i.followup.send("❌ "+short(exc))
            @discord.ui.button(label="Approve",style=discord.ButtonStyle.success,emoji="✅")
            async def approve(self,i,button):
                if self.used: return
                if await self.check(i): await self.run(i)
            @discord.ui.button(label="Deny",style=discord.ButtonStyle.danger,emoji="❌")
            async def deny(self,i,button):
                if self.used: return
                if not await self.check(i): return
                self.used=True
                for c in self.children: c.disabled=True
                await i.response.edit_message(content="❌ Cancelled — nothing was run.",view=self)
        view=View()
        if interaction: await interaction.response.send_message(message,view=view)
        else: await message.reply(message,view=view,mention_author=False)

    @tree.command(name="this-channel",description="Use this channel for the bot")
    async def this_channel(i):
        if not can_manage(i): await i.response.send_message("Manage Channels or Administrator required.",ephemeral=True); return
        set_channel(cfg,i.channel_id); await i.response.send_message("✅ This is now the bot channel.")
    @tree.command(name="add-channel",description="Add this channel to the bot")
    async def add_channel_cmd(i):
        if not can_manage(i): await i.response.send_message("Manage Channels or Administrator required.",ephemeral=True); return
        add_channel(cfg,i.channel_id); await i.response.send_message("✅ Added this channel.")
    @tree.command(name="remove-channel",description="Remove this channel from the bot")
    async def remove_channel_cmd(i):
        if not can_manage(i): await i.response.send_message("Manage Channels or Administrator required.",ephemeral=True); return
        remove_channel(cfg,i.channel_id); await i.response.send_message("✅ Removed this channel.")
    @tree.command(name="research",description="Create a parallel research plan")
    async def research_cmd(i,topics:str):
        if not allowed(i): await i.response.send_message("🔒 Not an enabled bot channel.",ephemeral=True); return
        topics_list=split_topics(topics)
        if len(topics_list)<2: await i.response.send_message("Use at least two topics separated by commas.",ephemeral=True); return
        await approval("research",topics_list,i.user.id,interaction=i,message=research_plan_text(topics_list))
    @tree.command(name="status",description="Show bot status")
    async def status_cmd(i): await i.response.send_message(f"{cfg['selected_provider']} / {cfg['selected_model']}\nChannels: {', '.join(cfg['discord'].get('channel_ids',[])) or 'none'}\nResearch agents: {cfg['settings'].get('max_research_agents',RESEARCH_MAX_AGENTS)}",ephemeral=True)
    @tree.command(name="invite",description="Show bot invite link")
    async def invite_cmd(i):
        link,error=discord_invite(cfg); await i.response.send_message(link or "❌ "+error,ephemeral=True)
    @tree.command(name="help",description="Show available commands")
    async def help_cmd(i): await i.response.send_message("/this-channel, /add-channel, /remove-channel, /research, /status, /invite\nNormal messages in enabled channels are handled automatically.",ephemeral=True)
    @bot.event
    async def on_ready():
        try:
            for guild in bot.guilds:
                tree.copy_global_to(guild=guild); await tree.sync(guild=guild)
            await tree.sync(); p(f"Discord ready as {bot.user}; channels: {', '.join(cfg['discord'].get('channel_ids',[])) or 'none'}")
        except Exception as exc: log_error(exc); p("Discord command sync failed: "+short(exc))
    @bot.event
    async def on_message(message):
        if message.author.bot or message.guild is None or not enabled(message.channel.id) or (cfg["discord"].get("allowed_user_ids") and message.author.id not in cfg["discord"]["allowed_user_ids"]): return
        text=message.content.strip()
        if not text or text.startswith("/"): return
        try:
            if looks_like_research(text):
                topics=split_topics(text)
                if len(topics)>=2: await approval("research",topics,message.author.id,message=message,interaction=None); return
            local=new_session(); add_message(local,"user",text); answer,provider,model=send_message(cfg,local); await message.reply(answer[:1900],mention_author=False)
        except Exception as exc: log_error(exc); await message.reply("❌ "+short(exc),mention_author=False)
    try: bot.run(token)
    except Exception as exc: log_error(exc); p("Discord bot stopped: "+short(exc))


def chat_loop(cfg):
    session=new_session(); p("Chat: /help, /new, /quit")
    while True:
        try: text=input(f"you@tab [{cfg['selected_provider']} {cfg['selected_model']} | {cfg['mode']}] > ").strip()
        except (KeyboardInterrupt,EOFError): return
        if not text: continue
        if text=="/quit": return
        if text=="/new": session=new_session(); p("New conversation."); continue
        if text=="/help": p("/new clears chat; /quit returns to menu. Research requests can require approval."); continue
        if looks_like_research(text):
            topics=split_topics(text)
            if len(topics)>=2:
                p(research_plan_text(topics))
                if input("Approve? [y/N] ").strip().lower()=="y":
                    try: p(synthesize_research(cfg,run_parallel_research(cfg,topics)))
                    except Exception as exc: p("❌ "+short(exc))
                else: p("❌ Cancelled.")
                continue
        add_message(session,"user",text)
        try:
            answer,provider,model=send_message(cfg,session); add_message(session,"assistant",answer,provider,model); p("assistant> "+answer)
        except Exception as exc: p("❌ "+short(exc))
        save_session(session)


def manage_keys(cfg):
    while True:
        p("\n=== API keys ==="); [p(f"{name}: {len(info.get('keys',[]))} key(s)") for name,info in cfg["providers"].items()]; p("1 Add  2 Delete  0 Back"); choice=input("> ").strip()
        if choice=="0": return
        name=input("Provider: ").strip()
        if name not in cfg["providers"]: continue
        if choice=="1":
            key=input("API key: ").strip()
            if key: cfg["providers"][name]["keys"].append({"key":key,"free":input("Free tier? [Y/n] ").strip().lower()!="n"}); persist(cfg)
        elif choice=="2":
            try: cfg["providers"][name]["keys"].pop(int(input("Key index: "))); persist(cfg)
            except (ValueError,IndexError): pass


def settings_menu(cfg):
    p("1 System prompt  2 Max research agents  3 Approval required  0 Back"); choice=input("> ").strip()
    if choice=="1":
        value=input("System prompt: ").strip()
        if value: cfg["settings"]["system_prompt"]=value; persist(cfg)
    elif choice=="2":
        try: cfg["settings"]["max_research_agents"]=max(1,min(4,int(input("Agents (1-4): ")))); persist(cfg)
        except ValueError: pass
    elif choice=="3": cfg["settings"]["require_approval"]=not cfg["settings"].get("require_approval",True); persist(cfg)


def main_menu(cfg):
    while True:
        p(f"\n=== Tab Assistant | {cfg['selected_provider']} / {cfg['selected_model']} | {cfg['mode']} ===\n1. Start / continue chat       6. Memory & history\n2. Choose provider             7. Composio tools\n3. Choose model                8. Discord bot\n4. Choose mode                 9. Settings\n5. Manage API keys            10. Invite bot\n0. Exit")
        choice=input("\nChoose: ").strip()
        if choice=="0": return
        if choice=="1": chat_loop(cfg)
        elif choice=="5": manage_keys(cfg)
        elif choice=="9": settings_menu(cfg)
        elif choice=="10":
            link,error=discord_invite(cfg); p(link or "❌ "+error)
        elif choice=="8": p("Discord runs automatically in the background when DISCORD_BOT_TOKEN is configured.")
        elif choice=="7":
            request=input("Composio request: ").strip()
            if request:
                try:
                    plan=composio_plan(cfg,request); p("\n"+"\n".join(f"{i}. {s.get('description',s.get('tool','unknown'))}" for i,s in enumerate(plan.get('steps',[]),1)))
                    if input("Approve? [y/N] ").strip().lower()=="y":
                        for ok,result in execute_composio(cfg,plan): p(("✅" if ok else "❌")+" "+result)
                except Exception as exc: p("❌ "+short(exc))
        else: p("That menu item is not implemented in this lightweight build yet.")


def main():
    DATA.mkdir(parents=True,exist_ok=True); cfg=load_config()
    if cfg is None: cfg=default_config(); save_config(cfg); p("Created config. Add API keys with menu 5.")
    if cfg["discord"].get("token") and cfg["discord"].get("autostart",True): threading.Thread(target=discord_bot,args=(cfg,),daemon=True,name="discord-bot").start()
    main_menu(cfg)

if __name__=="__main__": main()
