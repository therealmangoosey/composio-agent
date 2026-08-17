#!/usr/bin/env python3
"""Stable, Termux-friendly Tab Assistant."""
import asyncio
import copy
import datetime as dt
import json
import os
import re
import threading
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
MAX_AGENTS = 4

PROVIDERS = {
    "GROQ": ("https://api.groq.com/openai/v1", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], True),
    "Google Gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", ["gemini-2.5-flash"], True),
    "OpenAI": ("https://api.openai.com/v1", ["gpt-4o-mini"], False),
    "OpenRouter": ("https://openrouter.ai/api/v1", ["openrouter/free"], True),
    "Cerebras": ("https://api.cerebras.ai/v1", ["gpt-oss-120b"], True),
    "DeepSeek": ("https://api.deepseek.com", ["deepseek-chat"], False),
    "xAI Grok": ("https://api.x.ai/v1", ["grok-4.1-mini"], False),
}
ENV_KEYS = {
    "GROQ": "GROQ_API_KEYS", "Google Gemini": "GEMINI_API_KEYS",
    "OpenAI": "OPENAI_API_KEYS", "OpenRouter": "OPENROUTER_API_KEYS",
    "Cerebras": "CEREBRAS_API_KEYS", "DeepSeek": "DEEPSEEK_API_KEYS",
    "xAI Grok": "XAI_API_KEYS",
}
DEFAULT_SYSTEM = (
    "You are Tab Assistant. Be concise and useful. Never claim an external action happened "
    "unless a successful tool result confirms it. Composio tools are available when configured. "
    "Independent research can be split into parallel agents and must be approved before agents run."
)


def now(): return dt.datetime.now().isoformat(timespec="seconds")
def short(x, n=600): return re.sub(r"\s+", " ", str(x)).strip()[:n]
def p(x=""): print(x, flush=True)


def log_error(exc):
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        with ERROR_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n[{now()}]\n{traceback.format_exc()}\n")
    except OSError: pass


def load_dotenv():
    if not ENV_FILE.exists(): return
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except OSError: pass


def save_dotenv(values):
    old = {}
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1); old[k.strip()] = v
        except OSError: pass
    old.update({k: str(v) for k, v in values.items() if v is not None})
    ENV_FILE.write_text("# Secrets; do not commit.\n" + "\n".join(f"{k}={v}" for k,v in sorted(old.items()) if v) + "\n", encoding="utf-8")
    try: os.chmod(ENV_FILE, 0o600)
    except OSError: pass


def defaults():
    return {
        "providers": {k: {"base_url": v[0], "models": list(v[1]), "keys": []} for k,v in PROVIDERS.items()},
        "selected_provider": "GROQ", "selected_model": PROVIDERS["GROQ"][1][0], "mode": "free",
        "composio": {"api_key": "", "user_id": "tab-owner", "toolkit_version": "latest"},
        "discord": {"token": "", "channel_ids": [], "allowed_user_ids": [], "autostart": True},
        "settings": {"system_prompt": DEFAULT_SYSTEM, "max_history": 8, "temperature": 0.7, "max_research_agents": 4},
    }


def load_config():
    DATA.mkdir(parents=True, exist_ok=True); load_dotenv(); cfg = defaults()
    if CONFIG_FILE.exists():
        try: cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError): pass
    for name, info in cfg["providers"].items():
        info.setdefault("base_url", PROVIDERS[name][0]); info.setdefault("models", list(PROVIDERS[name][1])); info.setdefault("keys", [])
        raw = os.getenv(ENV_KEYS[name], "")
        if raw:
            old=info["keys"]; flags=[bool(x.get("free",PROVIDERS[name][2])) for x in old]
            info["keys"]=[{"key":k,"free":flags[i] if i < len(flags) else PROVIDERS[name][2]} for i,k in enumerate(x.strip() for x in raw.split(",") if x.strip())]
    cfg["composio"]["api_key"]=os.getenv("COMPOSIO_API_KEY", ""); cfg["discord"]["token"]=os.getenv("DISCORD_BOT_TOKEN", "")
    legacy=os.getenv("DISCORD_ALLOWED_CHANNEL_ID", "")
    if legacy and not cfg["discord"].get("channel_ids"): cfg["discord"]["channel_ids"]=[x.strip() for x in legacy.split(",") if x.strip()]
    cfg["settings"].setdefault("max_research_agents",4); return cfg


def save_config(cfg):
    DATA.mkdir(parents=True, exist_ok=True)
    env={ENV_KEYS[name]:",".join(x["key"] for x in info["keys"]) for name,info in cfg["providers"].items()}
    env.update({"COMPOSIO_API_KEY":cfg["composio"].get("api_key",""),"DISCORD_BOT_TOKEN":cfg["discord"].get("token",""),"DISCORD_ALLOWED_CHANNEL_ID":",".join(cfg["discord"].get("channel_ids",[]))})
    save_dotenv(env); public=copy.deepcopy(cfg)
    for info in public["providers"].values(): info["keys"]=[{"free":bool(x.get("free"))} for x in info["keys"]]
    public["composio"]["api_key"]=""; public["discord"]["token"]=""; CONFIG_FILE.write_text(json.dumps(public,indent=2)+"\n",encoding="utf-8")


def persist(cfg):
    try: save_config(cfg)
    except Exception as exc: p("Save warning: "+short(exc,180))


def new_session(): return {"id":uuid.uuid4().hex,"messages":[],"created":now()}
def add(session,role,content): session["messages"].append({"role":role,"content":str(content),"time":now()})

def save_session(session):
    try: mem=json.loads(MEMORY_FILE.read_text(encoding="utf-8")) if MEMORY_FILE.exists() else {"sessions":[]}
    except (OSError,json.JSONDecodeError): mem={"sessions":[]}
    mem["sessions"]=[x for x in mem["sessions"] if x.get("id")!=session["id"]]+[session]; mem["sessions"]=mem["sessions"][-100:]
    DATA.mkdir(parents=True,exist_ok=True); MEMORY_FILE.write_text(json.dumps(mem,indent=2)+"\n",encoding="utf-8")


def fetch_models(cfg,provider,key):
    try:
        data=requests.get(cfg["providers"][provider]["base_url"].rstrip("/")+"/models",headers={"Authorization":f"Bearer {key}"},timeout=8).json().get("data",[])
        models=[x["id"] for x in data if x.get("id")]
        if provider=="OpenRouter":
            free=[]
            for x in data:
                mid=str(x.get("id","")); price=x.get("pricing") or {}
                if mid=="openrouter/free" or ":free" in mid or (str(price.get("prompt")) in {"0","0.0"} and str(price.get("completion",price.get("output"))) in {"0","0.0"}): free.append(mid)
            models=free or ["openrouter/free"]
        return models or cfg["providers"][provider]["models"]
    except Exception: return cfg["providers"][provider]["models"]


def send(cfg,session,system=None,temperature=None,json_mode=False):
    order=[]; last=cfg.get("last_working",{})
    for provider,info in cfg["providers"].items():
        for item in info["keys"]:
            if cfg["mode"]=="free" and not item.get("free"): continue
            for model in fetch_models(cfg,provider,item["key"]):
                if cfg["mode"]=="free" and provider=="OpenRouter" and model!="openrouter/free" and ":free" not in model: continue
                order.append((0 if provider==last.get("provider") and model==last.get("model") else 1,provider,model,item["key"]))
    if not order: raise RuntimeError("No usable API keys. Add one in menu 5.")
    order.sort(key=lambda x:x[0]); messages=[{"role":"system","content":system or cfg["settings"]["system_prompt"]}]+session["messages"][-int(cfg["settings"].get("max_history",8)):]
    last_error=None
    for _,provider,model,key in order:
        payload={"model":model,"messages":messages,"temperature":float(temperature if temperature is not None else cfg["settings"]["temperature"])}
        if json_mode: payload["response_format"]={"type":"json_object"}
        for variant in (payload,{k:v for k,v in payload.items() if k!="response_format"},{k:v for k,v in payload.items() if k not in {"response_format","temperature"}}):
            try:
                r=requests.post(cfg["providers"][provider]["base_url"].rstrip("/")+"/chat/completions",headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},json=variant,timeout=45)
                if not r.ok: raise RuntimeError(f"{provider} error {r.status_code}: {short(r.text,300)}")
                text=str(r.json()["choices"][0]["message"].get("content","")).strip()
                if not text: raise RuntimeError("Model returned an empty response.")
                cfg["last_working"]={"provider":provider,"model":model}; persist(cfg); return text
            except Exception as exc: last_error=exc; log_error(exc); break
    raise last_error or RuntimeError("All providers failed.")


def split_topics(text):
    raw=re.sub(r"^\s*/?research(?:\s+on)?\s*:?\s*","",text.strip(),flags=re.I)
    return [x.strip(" \t\r\n,.;|") for x in re.split(r"\s*(?:,|;|\||\band\b|\n)\s*",raw,flags=re.I) if x.strip()][:MAX_AGENTS]


def is_research(text): return any(k in text.lower() for k in ("research","look into","compare")) and len(split_topics(text))>=2

def research_plan(topics): return "🔎 Research plan\n"+"\n".join(f"{i}. {t}" for i,t in enumerate(topics,1))+f"\n\nThis will run {len(topics)} agents in parallel. Nothing runs until approved."


def research_agents(cfg,topics):
    limit=max(1,min(MAX_AGENTS,int(cfg["settings"].get("max_research_agents",MAX_AGENTS)),len(topics)))
    def worker(topic):
        s=new_session(); add(s,"user",f"Research this topic carefully. Give concise factual findings and useful source URLs when available: {topic}"); return send(cfg,s,system="You are a research worker. Return factual research notes. Do not claim browsing unless tools actually browsed.",temperature=0.2)
    out=[]
    with ThreadPoolExecutor(max_workers=limit,thread_name_prefix="research-agent") as pool:
        futures={pool.submit(worker,t):t for t in topics}
        for f in as_completed(futures):
            t=futures[f]
            try: out.append((t,f.result()))
            except Exception as exc: out.append((t,"❌ "+short(exc)))
    return out


def synthesize(cfg,results):
    s=new_session(); add(s,"user","Synthesize these independent reports, preserve topic boundaries, flag uncertainty and do not invent citations.\n\n"+"\n\n".join(f"TOPIC: {t}\n{r}" for t,r in results)); return send(cfg,s,system="You are the final research synthesizer. Be concise and accurate.",temperature=0.2)


def composio_plan(cfg,request):
    if not cfg["composio"].get("api_key"): raise RuntimeError("Composio API key is not configured.")
    s=new_session(); add(s,'user','Return JSON only: {"steps":[{"tool":"ACTION_SLUG","description":"what it does","args":{}}]}. Never execute. Request: '+request); raw=send(cfg,s,json_mode=True); return json.loads(re.search(r"\{.*\}",raw,re.S).group(0))


def execute_composio(cfg,plan):
    try: from composio import Composio
    except ImportError as exc: raise RuntimeError("Install Composio with: pip install -U composio") from exc
    client=Composio(api_key=cfg["composio"]["api_key"]); results=[]
    for step in plan.get("steps",[]):
        try:
            args=step.get("args") if isinstance(step.get("args"),dict) else {}
            try: result=client.tools.execute(step["tool"],version=cfg["composio"].get("toolkit_version","latest"),arguments=args,user_id=cfg["composio"].get("user_id","tab-owner"))
            except TypeError: result=client.tools.execute(step["tool"],arguments=args,user_id=cfg["composio"].get("user_id","tab-owner"))
            results.append((True,str(result)))
        except Exception as exc: results.append((False,short(exc)))
    return results


def invite_link(cfg):
    token=cfg["discord"].get("token")
    if not token: return None,"DISCORD_BOT_TOKEN is not configured."
    try:
        r=requests.get("https://discord.com/api/v10/users/@me",headers={"Authorization":"Bot "+token},timeout=10); r.raise_for_status(); return f"https://discord.com/oauth2/authorize?client_id={r.json()['id']}&scope=bot%20applications.commands&permissions=84992",None
    except Exception as exc: return None,short(exc)


def allowed_user(cfg,user_id):
    allowed=cfg["discord"].get("allowed_user_ids",[]); return not allowed or user_id in allowed

def channel_enabled(cfg,channel_id): return str(channel_id) in set(cfg["discord"].get("channel_ids",[]))


async def run_approved(cfg,interaction,kind,payload):
    await interaction.followup.send("🚀 Starting approved task...")
    try:
        if kind=="research":
            results=await asyncio.to_thread(research_agents,cfg,payload); answer=await asyncio.to_thread(synthesize,cfg,results); await interaction.followup.send(answer[:1900])
        else:
            results=await asyncio.to_thread(execute_composio,cfg,payload); await interaction.followup.send("\n".join(("✅" if ok else "❌")+" "+r for ok,r in results)[:1900])
    except Exception as exc: await interaction.followup.send("❌ "+short(exc))


def approval_view(discord,cfg,kind,payload,owner_id):
    class Approval(discord.ui.View):
        def __init__(self): super().__init__(timeout=300); self.used=False
        async def check(self,i):
            if self.used: await i.response.send_message("This approval has expired.",ephemeral=True); return False
            if i.user.id!=owner_id: await i.response.send_message("Not your approval.",ephemeral=True); return False
            return True
        @discord.ui.button(label="Approve",style=discord.ButtonStyle.success,emoji="✅")
        async def approve(self,i,button):
            if not await self.check(i): return
            self.used=True
            for child in self.children: child.disabled=True
            await i.response.edit_message(view=self); await run_approved(cfg,i,kind,payload)
        @discord.ui.button(label="Deny",style=discord.ButtonStyle.danger,emoji="❌")
        async def deny(self,i,button):
            if not await self.check(i): return
            self.used=True
            for child in self.children: child.disabled=True
            await i.response.edit_message(content="❌ Cancelled — nothing was run.",view=self)
    return Approval()


def start_discord(cfg):
    try: import discord
    except ImportError: p("Discord bot requires discord.py; requirements.txt installs it."); return
    token=cfg["discord"].get("token")
    if not token: return
    intents=discord.Intents.default(); intents.message_content=True; bot=discord.Client(intents=intents); tree=discord.app_commands.CommandTree(bot)
    def can_manage(i):
        perms=getattr(i.user,"guild_permissions",None); return bool(perms and (perms.manage_channels or perms.administrator))
    def valid(i): return i.guild is not None and channel_enabled(cfg,i.channel_id) and allowed_user(cfg,i.user.id)
    @tree.command(name="this-channel",description="Set this as the bot channel")
    async def this_channel(i):
        if not can_manage(i): await i.response.send_message("Manage Channels or Administrator required.",ephemeral=True); return
        cfg["discord"]["channel_ids"]=[str(i.channel_id)]; persist(cfg); await i.response.send_message("✅ This is now the bot channel.")
    @tree.command(name="add-channel",description="Add this channel to the bot")
    async def add_channel(i):
        if not can_manage(i): await i.response.send_message("Manage Channels or Administrator required.",ephemeral=True); return
        cfg["discord"]["channel_ids"]=sorted(set(cfg["discord"].get("channel_ids",[]))|{str(i.channel_id)}); persist(cfg); await i.response.send_message("✅ Added this channel.")
    @tree.command(name="remove-channel",description="Remove this channel from the bot")
    async def remove_channel(i):
        if not can_manage(i): await i.response.send_message("Manage Channels or Administrator required.",ephemeral=True); return
        cfg["discord"]["channel_ids"]=sorted(set(cfg["discord"].get("channel_ids",[]))-{str(i.channel_id)}); persist(cfg); await i.response.send_message("✅ Removed this channel.")
    @tree.command(name="research",description="Create a parallel research plan")
    async def research_cmd(i,topics:str):
        if not valid(i): await i.response.send_message("🔒 Not an enabled bot channel.",ephemeral=True); return
        topics_list=split_topics(topics)
        if len(topics_list)<2: await i.response.send_message("Use at least two topics separated by commas.",ephemeral=True); return
        await i.response.send_message(research_plan(topics_list),view=approval_view(discord,cfg,"research",topics_list,i.user.id))
    @tree.command(name="status",description="Show bot status")
    async def status_cmd(i): await i.response.send_message(f"{cfg['selected_provider']} / {cfg['selected_model']}\nChannels: {', '.join(cfg['discord'].get('channel_ids',[])) or 'none'}",ephemeral=True)
    @tree.command(name="invite",description="Show the bot invite link")
    async def invite_cmd(i):
        link,error=invite_link(cfg); await i.response.send_message(link or "❌ "+error,ephemeral=True)
    @tree.command(name="help",description="Show bot commands")
    async def help_cmd(i): await i.response.send_message("/this-channel /add-channel /remove-channel /research /status /invite\nNormal messages work in enabled channels.",ephemeral=True)
    @bot.event
    async def on_ready():
        try:
            for guild in bot.guilds:
                tree.copy_global_to(guild=guild); await tree.sync(guild=guild)
            await tree.sync(); p(f"Discord ready as {bot.user}; channels: {', '.join(cfg['discord'].get('channel_ids',[])) or 'none'}")
        except Exception as exc: log_error(exc); p("Discord command sync failed: "+short(exc))
    @bot.event
    async def on_message(message):
        if message.author.bot or message.guild is None or not channel_enabled(cfg,message.channel.id) or not allowed_user(cfg,message.author.id): return
        text=message.content.strip()
        if not text or text.startswith("/"): return
        try:
            if is_research(text):
                topics=split_topics(text); await message.reply(research_plan(topics),view=approval_view(discord,cfg,"research",topics,message.author.id),mention_author=False); return
            if any(k in text.lower() for k in ("composio","gmail","send an email","search the web","news","save a note","add to list")):
                plan=composio_plan(cfg,text); preview="\n".join(f"{i}. {x.get('description',x.get('tool','unknown'))}" for i,x in enumerate(plan.get('steps',[]),1)); await message.reply("🛠️ **Composio plan**\n"+preview+"\n\nNothing will run until approved.",view=approval_view(discord,cfg,"composio",plan,message.author.id),mention_author=False); return
            s=new_session(); add(s,"user",text); answer=send(cfg,s); await message.reply(answer[:1900],mention_author=False)
        except Exception as exc: log_error(exc); await message.reply("❌ "+short(exc),mention_author=False)
    try: bot.run(token)
    except Exception as exc: log_error(exc); p("Discord bot stopped: "+short(exc))


def console_chat(cfg):
    session=new_session(); p("Chat: /help /new /quit")
    while True:
        try: text=input(f"you@tab [{cfg['selected_provider']} {cfg['selected_model']} | {cfg['mode']}] > ").strip()
        except (KeyboardInterrupt,EOFError): return
        if not text: continue
        if text=="/quit": return
        if text=="/new": session=new_session(); p("New conversation."); continue
        if text=="/help": p("Research requests like 'research A, B' show an approval prompt. /new resets chat. /quit returns to menu."); continue
        if is_research(text):
            topics=split_topics(text); p(research_plan(topics))
            if input("Approve? [y/N] ").strip().lower()=="y":
                try: p(synthesize(cfg,research_agents(cfg,topics)))
                except Exception as exc: p("❌ "+short(exc))
            else: p("❌ Cancelled.")
            continue
        if any(k in text.lower() for k in ("composio","gmail","send an email","search the web","news","save a note","add to list")):
            try:
                plan=composio_plan(cfg,text); p("\n".join(f"{i}. {x.get('description',x.get('tool','unknown'))}" for i,x in enumerate(plan.get('steps',[]),1)))
                if input("Approve Composio plan? [y/N] ").strip().lower()=="y":
                    for ok,result in execute_composio(cfg,plan): p(("✅" if ok else "❌")+" "+result)
            except Exception as exc: p("❌ "+short(exc))
            continue
        add(session,"user",text)
        try: answer=send(cfg,session); add(session,"assistant",answer); p("assistant> "+answer); save_session(session)
        except Exception as exc: p("❌ "+short(exc))


def menu(cfg):
    while True:
        p(f"\n=== Tab Assistant | {cfg['selected_provider']} / {cfg['selected_model']} | {cfg['mode']} ===\n1 Chat\n2 Provider\n3 Model\n4 Mode\n5 API keys\n6 History\n7 Composio\n8 Discord\n9 Settings\n10 Invite bot\n0 Exit")
        c=input("Choose: ").strip()
        if c=="0": return
        if c=="1": console_chat(cfg)
        elif c=="2":
            names=list(cfg["providers"]); p("\n".join(f"{i+1}. {n}" for i,n in enumerate(names)))
            try: i=int(input("Provider: "))-1; cfg["selected_provider"]=names[i]; cfg["selected_model"]=cfg["providers"][names[i]]["models"][0]; persist(cfg)
            except (ValueError,IndexError): pass
        elif c=="3":
            models=cfg["providers"][cfg["selected_provider"]]["models"]; p("\n".join(f"{i+1}. {m}" for i,m in enumerate(models)))
            try: cfg["selected_model"]=models[int(input("Model: "))-1]; persist(cfg)
            except (ValueError,IndexError): pass
        elif c=="4":
            mode=input("manual/free/auto: ").strip().lower()
            if mode in ("manual","free","auto"): cfg["mode"]=mode; persist(cfg)
        elif c=="5":
            name=input("Provider: ").strip()
            if name in cfg["providers"]:
                key=input("Add API key: ").strip()
                if key: cfg["providers"][name]["keys"].append({"key":key,"free":input("Free tier? [Y/n] ").strip().lower()!="n"}); persist(cfg)
        elif c=="7":
            req=input("Composio request: ").strip()
            if req:
                try:
                    plan=composio_plan(cfg,req); p("\n".join(f"{i}. {x.get('description',x.get('tool','unknown'))}" for i,x in enumerate(plan.get('steps',[]),1)))
                    if input("Approve? [y/N] ").strip().lower()=="y":
                        for ok,result in execute_composio(cfg,plan): p(("✅" if ok else "❌")+" "+result)
                except Exception as exc: p("❌ "+short(exc))
        elif c=="8": p("Discord runs automatically when DISCORD_BOT_TOKEN is configured.")
        elif c=="9": cfg["settings"]["max_research_agents"]=max(1,min(MAX_AGENTS,int(input("Max research agents [1-4]: ")))); persist(cfg)
        elif c=="10":
            link,error=invite_link(cfg); p(link or "❌ "+error)
        elif c=="6":
            try:
                mem=json.loads(MEMORY_FILE.read_text(encoding="utf-8")) if MEMORY_FILE.exists() else {"sessions":[]}
                p("\n".join(f"{i+1}. {x.get('created','?')} ({len(x.get('messages',[]))} messages)" for i,x in enumerate(mem.get('sessions',[])[-10:])))
            except Exception: p("No saved history.")


def main():
    cfg=load_config()
    if cfg["discord"].get("token") and cfg["discord"].get("autostart",True): threading.Thread(target=start_discord,args=(cfg,),daemon=True,name="discord").start()
    menu(cfg)

if __name__=="__main__": main()
