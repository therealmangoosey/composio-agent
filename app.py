#!/usr/bin/env python3
"""Tab Assistant: a lightweight AI assistant designed for Termux/Android.

Required: Python 3.10+, requests, openai
Optional: discord.py, composio
Secrets are stored in .env; runtime data is stored under data/.
"""
import argparse
import copy
import csv
import datetime as dt
import hashlib
import json
import os
import re
import time
import traceback
import uuid
from pathlib import Path

try:
    import readline  # noqa: F401  # improves Termux input/history when available
except ImportError:
    pass

import requests
from openai import OpenAI, APIConnectionError, APIStatusError, AuthenticationError, RateLimitError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_FILE = DATA / "config.json"
MEMORY_FILE = DATA / "memory.json"
ERROR_FILE = DATA / "errors.log"
ENV_FILE = ROOT / ".env"

PROVIDERS = {
    "GROQ": {"base_url": "https://api.groq.com/openai/v1", "free": True, "models": [
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "meta-llama/llama-4-scout-17b-16e-instruct",
        "openai/gpt-oss-120b", "qwen/qwen3-32b"]},
    "Google Gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "free": True,
                      "models": ["gemini-2.5-flash", "gemini-2.5-flash-lite"]},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "free": False,
               "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"]},
    "OpenRouter": {"base_url": "https://openrouter.ai/api/v1", "free": True,
                   "models": ["openai/gpt-oss-120b:free", "meta-llama/llama-3.3-70b-instruct:free",
                              "qwen/qwen3-32b:free"]},
    "Cerebras": {"base_url": "https://api.cerebras.ai/v1", "free": True,
                 "models": ["llama3.1-8b", "gpt-oss-120b"]},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "free": False,
                 "models": ["deepseek-chat"]},
    "xAI Grok": {"base_url": "https://api.x.ai/v1", "free": False, "models": ["grok-4.6"]},
}

DEFAULT_SYSTEM = (
    "You are Tab Assistant, a careful personal AI assistant. Be concise and helpful. "
    "Never claim an external action happened unless a tool result confirms it."
)


def p(text=""):
    print(text, flush=True)


def clear():
    print("\033[2J\033[H", end="", flush=True)


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def short(text, n=160):
    return re.sub(r"\s+", " ", str(text)).strip()[:n]


def mask(key):
    if not key:
        return "(none)"
    return key[:4] + "…" + key[-4:] if len(key) > 9 else "…"


def load_dotenv():
    if not ENV_FILE.exists():
        return
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(name.strip(), value)
    except OSError as exc:
        p("Warning: could not read .env: " + short(exc))


def save_dotenv(values):
    old = {}
    if ENV_FILE.exists():
        try:
            for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if "=" in raw and not raw.lstrip().startswith("#"):
                    name, value = raw.split("=", 1)
                    old[name.strip()] = value
        except OSError:
            pass
    old.update({k: str(v) for k, v in values.items() if v is not None})
    lines = ["# Tab Assistant secrets — do not commit this file."]
    lines.extend(f"{k}={v}" for k, v in sorted(old.items()) if v != "")
    try:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise RuntimeError("Cannot write .env: " + str(exc)) from exc
    os.environ.update({k: str(v) for k, v in values.items() if v is not None})


def env_name(provider):
    return {"GROQ": "GROQ", "Google Gemini": "GEMINI", "OpenAI": "OPENAI",
            "OpenRouter": "OPENROUTER", "Cerebras": "CEREBRAS", "DeepSeek": "DEEPSEEK",
            "xAI Grok": "XAI"}[provider] + "_API_KEYS"


def hydrate_secrets(cfg):
    for provider, info in cfg["providers"].items():
        raw = os.getenv(env_name(provider), "")
        keys = [x.strip() for x in raw.split(",") if x.strip()]
        old = info.get("keys", [])
        flags = [bool(x.get("free", PROVIDERS[provider]["free"])) for x in old]
        info["keys"] = [{"key": key, "free": flags[i] if i < len(flags) else PROVIDERS[provider]["free"]}
                        for i, key in enumerate(keys)]
    cfg["composio"]["api_key"] = os.getenv("COMPOSIO_API_KEY", "")
    cfg["discord"]["token"] = os.getenv("DISCORD_BOT_TOKEN", "")
    cfg["discord"]["allowed_channel_id"] = os.getenv(
        "DISCORD_ALLOWED_CHANNEL_ID", cfg["discord"].get("allowed_channel_id", ""))


def secret_values(cfg):
    values = {env_name(name): ",".join(k["key"] for k in info.get("keys", []))
              for name, info in cfg["providers"].items()}
    values.update({
        "COMPOSIO_API_KEY": cfg["composio"].get("api_key", ""),
        "DISCORD_BOT_TOKEN": cfg["discord"].get("token", ""),
        "DISCORD_ALLOWED_CHANNEL_ID": cfg["discord"].get("allowed_channel_id", ""),
    })
    return values


def log_error(exc):
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        with ERROR_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n[{now()}]\n{traceback.format_exc()}\n")
    except OSError:
        pass


def default_config():
    return {
        "providers": {name: {"base_url": x["base_url"], "models": list(x["models"]), "keys": []}
                      for name, x in PROVIDERS.items()},
        "selected_provider": "GROQ",
        "selected_model": PROVIDERS["GROQ"]["models"][0],
        "mode": "free",
        "last_working": {},
        "failures": {},
        "composio": {"api_key": "", "toolkits": ["GMAIL", "WEB_SEARCH", "NEWS"], "user_id": "tab-owner",
                     "toolkit_version": "latest"},
        "discord": {"token": "", "allowed_channel_id": "", "allowed_user_ids": []},
        "settings": {"system_prompt": DEFAULT_SYSTEM, "max_history": 8, "streaming": False,
                     "temperature": 0.7, "require_approval": True, "cooldown_seconds": 60},
    }


def merge_defaults(cfg):
    base = default_config()
    for key, value in base.items():
        if key not in cfg:
            cfg[key] = copy.deepcopy(value)
    for name, value in base["providers"].items():
        if name not in cfg["providers"]:
            cfg["providers"][name] = copy.deepcopy(value)
        else:
            cfg["providers"][name].setdefault("base_url", value["base_url"])
            cfg["providers"][name].setdefault("models", list(value["models"]))
            cfg["providers"][name].setdefault("keys", [])
    cfg["settings"].setdefault("max_history", 8)
    cfg["settings"].setdefault("streaming", False)
    cfg["settings"].setdefault("temperature", 0.7)
    cfg["settings"].setdefault("require_approval", True)
    cfg["settings"].setdefault("cooldown_seconds", 60)
    cfg["composio"].setdefault("toolkits", ["GMAIL", "WEB_SEARCH", "NEWS"])
    cfg["composio"].setdefault("user_id", "tab-owner")
    cfg["composio"].setdefault("toolkit_version", "latest")
    cfg["discord"].setdefault("allowed_user_ids", [])
    return cfg


def load_config():
    DATA.mkdir(parents=True, exist_ok=True)
    load_dotenv()
    if not CONFIG_FILE.exists():
        return None
    try:
        cfg = merge_defaults(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        last = cfg.get("last_working", {})
        if last.get("provider") in cfg["providers"] and last.get("model"):
            cfg["selected_provider"] = last["provider"]
            cfg["selected_model"] = last["model"]
        hydrate_secrets(cfg)
        return cfg
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        p("Config is invalid or unreadable: " + short(exc))
        return None


def save_config(cfg):
    DATA.mkdir(parents=True, exist_ok=True)
    save_dotenv(secret_values(cfg))
    public = copy.deepcopy(cfg)
    for info in public["providers"].values():
        info["keys"] = [{"free": bool(x.get("free", False))} for x in info.get("keys", [])]
    public["composio"]["api_key"] = ""
    public["discord"]["token"] = ""
    public["discord"]["allowed_channel_id"] = ""
    public.get("last_working", {}).pop("key", None)
    CONFIG_FILE.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")


def load_memory():
    if not MEMORY_FILE.exists():
        return {"sessions": []}
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("sessions", []), list) else {"sessions": []}
    except (OSError, json.JSONDecodeError):
        return {"sessions": []}


def save_memory(mem):
    DATA.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(mem, indent=2) + "\n", encoding="utf-8")


def wizard():
    clear()
    p("=== Tab Assistant setup ===\nBlank values skip optional settings. /quit exits chat.\n")
    cfg = default_config()
    key = input("Composio API key (optional): ").strip()
    if key:
        cfg["composio"]["api_key"] = key
    token = input("Discord bot token (optional): ").strip()
    cfg["discord"]["token"] = token
    if token:
        cfg["discord"]["allowed_channel_id"] = input("Allowed Discord channel ID: ").strip()
    for name in PROVIDERS:
        if input(f"Add a key for {name} now? [y/N] ").strip().lower() == "y":
            key = input("  API key: ").strip()
            if key:
                cfg["providers"][name]["keys"].append({"key": key, "free": input("  Free tier? [Y/n] ").strip().lower() != "n"})
    mode = input("Default mode Manual / Free / Auto [Free]: ").strip().lower() or "free"
    cfg["mode"] = mode if mode in ("manual", "free", "auto") else "free"
    save_config(cfg)
    p("\nSetup complete. Use the menu to change settings later.")
    return cfg


def failure_id(provider, key):
    return provider + ":" + hashlib.sha256(key.encode()).hexdigest()[:16]


def health(cfg, provider, key):
    failure = cfg["failures"].get(failure_id(provider, key), {})
    until = float(failure.get("cooldown_until", 0))
    if until > time.time():
        return "cooldown until " + time.strftime("%H:%M", time.localtime(until))
    return "last error: " + short(failure["last_error"], 55) if failure.get("last_error") else "OK"


def mark_failure(cfg, provider, key, exc):
    status = getattr(exc, "status_code", 0)
    auth = isinstance(exc, AuthenticationError) or status == 401
    seconds = 600 if auth else max(5, int(cfg["settings"].get("cooldown_seconds", 60)))
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            retry_after = int(float(response.headers.get("retry-after", 0)))
            if retry_after > 0:
                seconds = max(seconds, retry_after)
        except (ValueError, TypeError, AttributeError):
            pass
    fid = failure_id(provider, key)
    old = cfg["failures"].get(fid, {})
    cfg["failures"][fid] = {"fail_count": int(old.get("fail_count", 0)) + 1,
                            "cooldown_until": time.time() + seconds, "last_error": short(exc)}


def mark_success(cfg, provider, model, key):
    cfg["last_working"] = {"provider": provider, "model": model}
    cfg["failures"].pop(failure_id(provider, key), None)


def persist_runtime(cfg):
    try:
        save_config(cfg)
    except Exception as exc:
        p("Warning: could not save settings: " + short(exc))


def candidates(cfg, mode):
    out = []
    last = cfg.get("last_working", {})
    for provider, info in cfg["providers"].items():
        keys = info.get("keys", [])
        for item in keys:
            if mode == "free" and not item.get("free"):
                continue
            models = info.get("models", []) or [cfg.get("selected_model", "")]
            preferred = last.get("model") if last.get("provider") == provider else None
            ordered = ([preferred] if preferred in models else []) + [m for m in models if m != preferred]
            for model in ordered:
                if model:
                    out.append((provider, model, item.get("key", "")))
    out.sort(key=lambda x: 0 if x[:2] == (last.get("provider"), last.get("model")) else 1)
    return [x for x in out if x[2]]


def get_client(cfg, provider, key):
    return OpenAI(api_key=key, base_url=cfg["providers"][provider]["base_url"], timeout=30, max_retries=0)


def friendly(provider, exc, mode):
    status = getattr(exc, "status_code", 0)
    if isinstance(exc, RateLimitError) or status == 429:
        return "Rate limited — switching to the next available key/model…" if mode != "manual" else "Rate limited — wait and try again."
    if isinstance(exc, AuthenticationError) or status == 401:
        return f"❌ {provider} rejected the API key — check it in menu 5."
    if isinstance(exc, APIConnectionError):
        return "❌ Can't reach the provider — check your internet connection."
    if isinstance(exc, APIStatusError):
        return f"❌ {provider} error {status}: {short(exc)}"
    return "❌ Request failed: " + short(exc)


def messages_for(cfg, session):
    n = max(1, min(50, int(cfg["settings"].get("max_history", 8))))
    return [{"role": "system", "content": cfg["settings"].get("system_prompt", DEFAULT_SYSTEM)}] + [
        {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
        for m in session.get("messages", [])[-n:]
    ]


def _request(client, args):
    """Retry common OpenAI-compatible API incompatibilities without hiding real errors."""
    attempts = [dict(args)]
    no_temp = dict(args); no_temp.pop("temperature", None)
    if no_temp != args:
        attempts.append(no_temp)
    no_format = dict(no_temp); no_format.pop("response_format", None)
    if no_format != no_temp:
        attempts.append(no_format)
    last = None
    for candidate in attempts:
        try:
            return client.chat.completions.create(**candidate)
        except APIStatusError as exc:
            last = exc
            if getattr(exc, "status_code", 0) not in (400, 404, 422):
                raise
    raise last


def send_with_failover(cfg, session, stream=None, force_json=False):
    mode = cfg["mode"]
    stream = cfg["settings"].get("streaming", False) if stream is None else bool(stream)
    if mode == "manual":
        provider = cfg.get("selected_provider", "GROQ")
        model = cfg.get("selected_model") or cfg["providers"][provider]["models"][0]
        keys = cfg["providers"].get(provider, {}).get("keys", [])
        choices = [(provider, model, keys[0]["key"])] if keys and keys[0].get("key") else []
    else:
        choices = candidates(cfg, "free" if mode == "free" else "auto")
    if not choices:
        raise RuntimeError("No usable API keys. Add one in menu 5.")

    last_exc = None
    skipped = 0
    for provider, model, key in choices:
        if cfg["failures"].get(failure_id(provider, key), {}).get("cooldown_until", 0) > time.time():
            skipped += 1
            continue
        try:
            args = {"model": model, "messages": messages_for(cfg, session),
                    "temperature": float(cfg["settings"].get("temperature", 0.7)), "stream": stream}
            if force_json:
                args["response_format"] = {"type": "json_object"}
            response = _request(get_client(cfg, provider, key), args)
            if stream:
                parts = []
                for chunk in response:
                    choices_chunk = getattr(chunk, "choices", [])
                    if not choices_chunk:
                        continue
                    delta = getattr(choices_chunk[0], "delta", None)
                    bit = getattr(delta, "content", None) or ""
                    if bit:
                        parts.append(bit)
                        print(bit, end="", flush=True)
                text = "".join(parts)
                print(flush=True)
            else:
                text = getattr(response.choices[0].message, "content", "") or ""
            if not text.strip():
                raise RuntimeError("The model returned an empty response.")
            mark_success(cfg, provider, model, key)
            persist_runtime(cfg)
            return text, provider, model
        except Exception as exc:
            last_exc = exc
            log_error(exc)
            mark_failure(cfg, provider, key, exc)
            p(friendly(provider, exc, mode))
            if mode == "manual":
                break
    if skipped == len(choices):
        raise RuntimeError("All available keys are cooling down. Try again later.")
    raise RuntimeError(friendly("Assistant", last_exc, mode) if last_exc else "All available keys failed.")


def new_session():
    return {"id": uuid.uuid4().hex, "created": now(), "messages": []}


def add_message(session, role, content, provider="", model=""):
    session.setdefault("messages", []).append({"role": role, "content": str(content), "provider": provider,
                                                "model": model, "time": now()})


def save_session(session):
    mem = load_memory()
    sessions = [x for x in mem["sessions"] if x.get("id") != session.get("id")]
    sessions.append(session)
    mem["sessions"] = sessions[-100:]
    save_memory(mem)


def chat_loop(cfg, session=None):
    session = session or new_session()
    p("Chat: /help, /new, /model NAME, /provider NAME, /mode MODE, /history, /quit")
    while True:
        try:
            prompt = f"you@tab [{cfg['selected_provider']} {cfg['selected_model']} | {cfg['mode']}] > "
            text = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            p("\nBack to menu.")
            save_session(session)
            return session
        if not text:
            continue
        if text == "/quit":
            save_session(session)
            return session
        if text == "/new":
            session = new_session(); p("New conversation."); continue
        if text == "/history":
            for m in session["messages"][-12:]:
                p(f"{m['role']}: {short(m['content'])}")
            continue
        if text == "/help":
            p("/new clears chat; /quit returns to menu; /provider, /model and /mode change options.")
            continue
        if text.startswith("/provider "):
            name = text[len("/provider "):].strip()
            if name in cfg["providers"]:
                cfg["selected_provider"] = name
                cfg["selected_model"] = cfg["providers"][name]["models"][0]
                persist_runtime(cfg)
            else:
                p("Unknown provider.")
            continue
        if text.startswith("/model "):
            model = text[len("/model "):].strip()
            if model:
                cfg["selected_model"] = model; persist_runtime(cfg)
            continue
        if text.startswith("/mode "):
            mode = text[len("/mode "):].strip().lower()
            if mode in ("manual", "free", "auto"):
                cfg["mode"] = mode; persist_runtime(cfg)
            else:
                p("Use manual, free, or auto.")
            continue
        add_message(session, "user", text)
        try:
            p("assistant> ")
            answer, provider, model = send_with_failover(cfg, session)
            add_message(session, "assistant", answer, provider, model)
        except Exception as exc:
            p("❌ " + short(exc))
        save_session(session)


def manage_keys(cfg):
    while True:
        clear(); p("=== API keys ===")
        for name, info in cfg["providers"].items():
            keys = info.get("keys", [])
            shown = ", ".join(f"{mask(k['key'])} [{'FREE' if k.get('free') else 'PAID'}] {health(cfg, name, k['key'])}" for k in keys)
            p(name + ": " + (shown or "none"))
        p("\n1 Add  2 Replace  3 Delete  4 Toggle free/paid  0 Back")
        action = input("> ").strip()
        if action == "0":
            return
        if action not in {"1", "2", "3", "4"}:
            continue
        name = input("Provider (exact name): ").strip()
        if name not in cfg["providers"]:
            p("Unknown provider."); input("Enter to continue"); continue
        keys = cfg["providers"][name]["keys"]
        if action == "1":
            key = input("Key: ").strip()
            if key:
                keys.append({"key": key, "free": input("Free? [Y/n] ").strip().lower() != "n"})
        else:
            try:
                i = int(input("Key number (0-based): "))
            except ValueError:
                continue
            if not 0 <= i < len(keys):
                continue
            if action == "2":
                replacement = input("Replacement key: ").strip()
                if replacement: keys[i]["key"] = replacement
            elif action == "3":
                keys.pop(i)
            else:
                keys[i]["free"] = not bool(keys[i].get("free"))
        persist_runtime(cfg)



def discord_invite_link(cfg):
    """Return a Discord bot invite URL using the configured bot token."""
    token = cfg.get("discord", {}).get("token", "")
    if not token:
        return None, "Set DISCORD_BOT_TOKEN in .env first."
    try:
        r = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": "Bot " + token},
            timeout=10,
        )
        if not r.ok:
            return None, "Discord rejected the bot token."
        app_id = str(r.json().get("id", ""))
        if not app_id:
            return None, "Discord did not return the bot application ID."
        return (
            "https://discord.com/oauth2/authorize?client_id="
            + app_id
            + "&scope=bot%20applications.commands&permissions=84992",
            None,
        )
    except (requests.RequestException, ValueError, TypeError) as exc:
        return None, "Could not contact Discord: " + short(exc)

def local_tool(tool, args):
    DATA.mkdir(parents=True, exist_ok=True)
    args = args if isinstance(args, dict) else {}
    if tool == "ADD_TO_LIST":
        path = DATA / "leads.csv"; exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "email", "notes"])
            if not exists: writer.writeheader()
            writer.writerow({k: str(args.get(k, "")) for k in ("name", "email", "notes")})
        return f"added {args.get('name', 'item')} to leads.csv"
    if tool == "READ_LIST":
        path = DATA / "leads.csv"
        return path.read_text(encoding="utf-8") if path.exists() else "leads list is empty"
    if tool == "SAVE_NOTE":
        with (DATA / "notes.txt").open("a", encoding="utf-8") as f:
            f.write(f"[{now()}] {args.get('text', '')}\n")
        return "note saved"
    if tool == "READ_NOTE":
        path = DATA / "notes.txt"
        return path.read_text(encoding="utf-8") if path.exists() else "no notes yet"
    raise RuntimeError("Unsupported local tool: " + str(tool))


PLAN_PROMPT = '''Return JSON only: {"summary": string, "steps": [{"tool": string, "description": string, "args": object}]}.
Local tools: ADD_TO_LIST(name,email,notes), READ_LIST, SAVE_NOTE(text), READ_NOTE.
For Composio actions use their action slug and fully specified args. Make a safe, exact plan. Never execute it.'''


def build_plan(cfg, request):
    session = {"messages": [{"role": "user", "content": PLAN_PROMPT + "\nRequest: " + request}]}
    try:
        raw, _, _ = send_with_failover(cfg, session, stream=False, force_json=True)
    except Exception:
        raw, _, _ = send_with_failover(cfg, session, stream=False)
    match = re.search(r"\{.*\}", raw, re.S)
    plan = json.loads(match.group(0) if match else raw)
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise ValueError("Model did not return a valid plan")
    return plan


def composio_tool(cfg, action, args):
    key = cfg["composio"].get("api_key")
    if not key:
        raise RuntimeError("Composio API key is not configured")
    try:
        from composio import Composio
    except ImportError as exc:
        raise RuntimeError("Composio is not installed. Run: pip install -U composio") from exc
    client = Composio(api_key=key)
    version = cfg["composio"].get("toolkit_version") or "latest"
    kwargs = {"arguments": args if isinstance(args, dict) else {}, "user_id": cfg["composio"].get("user_id", "tab-owner")}
    try:
        return str(client.tools.execute(action, version=version, **kwargs))
    except TypeError:
        return str(client.tools.execute(action, **kwargs))
    except Exception as exc:
        message = short(exc, 300)
        if "version" in message.lower() and "required" in message.lower():
            raise RuntimeError("Composio needs a toolkit version. Set composio.toolkit_version in data/config.json.") from exc
        raise


def execute_plan(cfg, plan):
    results = []
    for step in plan.get("steps", []):
        try:
            tool, args = step.get("tool", ""), step.get("args", {})
            result = local_tool(tool, args) if tool in {"ADD_TO_LIST", "READ_LIST", "SAVE_NOTE", "READ_NOTE"} else composio_tool(cfg, tool, args)
            results.append((True, result))
        except Exception as exc:
            log_error(exc); results.append((False, short(exc)))
    return results


def tools_menu(cfg):
    p("\nComposio is optional. Local list/note tools work without it.")
    p("1 Build/run a plan  2 Composio help  0 Back")
    choice = input("> ").strip()
    if choice == "1":
        try:
            plan = build_plan(cfg, input("Request: "))
            p("\n📋 " + short(plan.get("summary", "Plan")))
            for i, step in enumerate(plan.get("steps", []), 1):
                p(f"{i}. {step.get('description', step.get('tool', 'unknown'))}")
            if not cfg["settings"].get("require_approval") or input("Run? [y/N] ").strip().lower() == "y":
                for i, (ok, result) in enumerate(execute_plan(cfg, plan), 1):
                    p(("✅" if ok else "❌") + f" {i}/{len(plan['steps'])} — {result}")
            else:
                p("Plan cancelled — nothing was executed.")
        except Exception as exc:
            log_error(exc); p("Couldn't build a plan: " + short(exc))
    elif choice == "2":
        p("Install with: pip install -U composio")
        p("For direct tool execution, set composio.toolkit_version in data/config.json when required.")


def choose(cfg, kind):
    if kind == "provider":
        for i, name in enumerate(cfg["providers"], 1): p(f"{i}. {name}")
        name = input("Provider: ").strip()
        if name in cfg["providers"]:
            cfg["selected_provider"] = name
            cfg["selected_model"] = cfg["providers"][name]["models"][0]
    elif kind == "model":
        for model in cfg["providers"][cfg["selected_provider"]]["models"]: p("- " + model)
        model = input("Model (or paste a new ID): ").strip()
        if model: cfg["selected_model"] = model
    else:
        mode = input("manual / free / auto: ").strip().lower()
        if mode in ("manual", "free", "auto"): cfg["mode"] = mode
    persist_runtime(cfg)


def settings(cfg):
    s = cfg["settings"]
    p("1 System prompt  2 History count  3 Streaming  4 Temperature  5 Approval  6 Fetch free OpenRouter models  0 Back")
    choice = input("> ").strip()
    try:
        if choice == "1": s["system_prompt"] = input("System prompt: ").strip() or s["system_prompt"]
        elif choice == "2": s["max_history"] = max(1, min(50, int(input("Last messages: "))))
        elif choice == "3": s["streaming"] = not bool(s["streaming"])
        elif choice == "4": s["temperature"] = max(0.0, min(2.0, float(input("Temperature: "))))
        elif choice == "5": s["require_approval"] = not bool(s["require_approval"])
        elif choice == "6":
            data = requests.get("https://openrouter.ai/api/v1/models", timeout=15).json().get("data", [])
            models = [m["id"] for m in data if m.get("id") and (":free" in m["id"] or str(m.get("pricing", {}).get("prompt")) in {"0", "0.0"})]
            if models:
                cfg["providers"]["OpenRouter"]["models"] = models
                p(f"Saved {len(models)} current free model IDs.")
            else:
                p("OpenRouter returned no free models.")
    except (ValueError, requests.RequestException, KeyError, TypeError) as exc:
        p("Could not update setting: " + short(exc))
    persist_runtime(cfg)


def history_menu(cfg):
    mem = load_memory(); sessions = mem["sessions"]
    if not sessions:
        p("No saved sessions."); input("Enter to continue"); return
    for i, session in enumerate(sessions, 1):
        messages = session.get("messages", [])
        first = messages[0].get("content", "empty") if messages else "empty"
        p(f"{i}. {session.get('created', '?')} | {short(first, 45)} | {len(messages)} messages")
    choice = input("Resume number, d NUMBER delete, c clear all, Enter back: ").strip()
    if choice.isdigit() and 0 < int(choice) <= len(sessions):
        chat_loop(cfg, sessions[int(choice) - 1])
    elif choice.startswith("d ") and choice[2:].isdigit():
        i = int(choice[2:]) - 1
        if 0 <= i < len(sessions): sessions.pop(i); save_memory(mem)
    elif choice == "c" and input("Delete all history? [y/N] ").strip().lower() == "y":
        save_memory({"sessions": []})


def main_menu(cfg):
    while True:
        clear(); p(f"=== Tab Assistant | {cfg['selected_provider']} / {cfg['selected_model']} | {cfg['mode']} ===")
        p("1. Start / continue chat       6. Memory & history\n2. Choose provider             7. Composio tools & connections\n3. Choose model                8. Discord bot\n4. Choose mode                 9. Settings\n5. Manage API keys            10. Invite bot to a server\n0. Exit")
        try:
            choice = input("\nChoose: ").strip()
        except (KeyboardInterrupt, EOFError):
            p("\nBye."); return
        if choice == "0": return
        if choice == "1": chat_loop(cfg)
        elif choice == "2": choose(cfg, "provider")
        elif choice == "3": choose(cfg, "model")
        elif choice == "4": choose(cfg, "mode")
        elif choice == "5": manage_keys(cfg)
        elif choice == "6": history_menu(cfg)
        elif choice == "7": tools_menu(cfg)
        elif choice == "8": p("Run in another Termux/tmux session: python app.py --bot"); input("Enter")
        elif choice == "9": settings(cfg)
        elif choice == "10":
            link, error = discord_invite_link(cfg)
            if link:
                p("\nInvite link:")
                p(link)
            else:
                p("\n❌ " + error)
            input("Enter to continue")


def run_bot(cfg):
    try:
        import discord
    except ImportError:
        p("Discord bot needs: pip install -U discord.py"); return
    token = cfg["discord"].get("token")
    allowed = str(cfg["discord"].get("allowed_channel_id", ""))
    if not token or not allowed:
        p("Set DISCORD_BOT_TOKEN and DISCORD_ALLOWED_CHANNEL_ID in .env."); return
    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(bot)
    plans = {}
    discord_session = new_session()

    def allowed_interaction(interaction):
        allow_users = cfg["discord"].get("allowed_user_ids", [])
        return (interaction.guild is not None and str(interaction.channel_id) == allowed and
                (not allow_users or interaction.user.id in allow_users))

    async def guard(interaction):
        if allowed_interaction(interaction): return True
        await interaction.response.send_message("🔒 I only work in the configured channel.", ephemeral=True)
        return False

    class PlanView(discord.ui.View):
        def __init__(self, pid, owner):
            super().__init__(timeout=300); self.pid = pid; self.owner = owner
        async def finish(self, interaction, message):
            for child in self.children: child.disabled = True
            if interaction.message:
                await interaction.message.edit(view=self)
            await interaction.followup.send(message)
        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
        async def confirm(self, interaction, button):
            if not allowed_interaction(interaction) or interaction.user.id != self.owner:
                await interaction.response.send_message("Not your plan.", ephemeral=True); return
            await interaction.response.defer(); plan = plans.pop(self.pid, None)
            if not plan: await self.finish(interaction, "Plan expired — nothing was executed."); return
            for i, (ok, result) in enumerate(execute_plan(cfg, plan), 1):
                await interaction.followup.send(("✅" if ok else "❌") + f" {i}/{len(plan['steps'])} — {result}")
            await self.finish(interaction, "Plan completed.")
        @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
        async def deny(self, interaction, button):
            if interaction.user.id != self.owner:
                await interaction.response.send_message("Not your plan.", ephemeral=True); return
            await interaction.response.defer(); plans.pop(self.pid, None); await self.finish(interaction, "Plan cancelled — nothing was executed.")
        async def on_timeout(self):
            plans.pop(self.pid, None)
            for child in self.children: child.disabled = True

    @tree.command(name="chat", description="Chat without tools")
    async def chat(interaction, message: str):
        if not await guard(interaction): return
        await interaction.response.defer(thinking=True)
        try:
            add_message(discord_session, "user", message)
            answer, provider, model = send_with_failover(cfg, discord_session, stream=False)
            add_message(discord_session, "assistant", answer, provider, model)
            await interaction.followup.send(answer[:1900])
        except Exception as exc:
            log_error(exc); await interaction.followup.send("❌ " + short(exc), ephemeral=True)

    @tree.command(name="plan", description="Build an approval-gated action plan")
    async def plan(interaction, request: str):
        if not await guard(interaction): return
        await interaction.response.defer(thinking=True)
        try:
            plan_data = build_plan(cfg, request); pid = uuid.uuid4().hex; plans[pid] = plan_data
            text = "📋 Plan (%d steps)\n" % len(plan_data["steps"]) + "\n".join(
                f"{i}. {s.get('description', s.get('tool', 'unknown'))}" for i, s in enumerate(plan_data["steps"], 1))
            await interaction.followup.send(text[:1900], view=PlanView(pid, interaction.user.id))
        except Exception:
            await interaction.followup.send("Couldn't build a plan from that — try rephrasing.", ephemeral=True)

    @tree.command(name="status", description="Show active model and key health")
    async def status(interaction):
        if not await guard(interaction): return
        lines = [f"{n}: " + ", ".join(health(cfg, n, k["key"]) for k in v.get("keys", [])) if v.get("keys") else f"{n}: no key"
                 for n, v in cfg["providers"].items()]
        await interaction.response.send_message(f"{cfg['selected_provider']} / {cfg['selected_model']} | {cfg['mode']}\n" + "\n".join(lines), ephemeral=True)

    @tree.command(name="provider", description="Select a provider")
    async def provider_cmd(interaction, name: str):
        if not await guard(interaction): return
        if name not in cfg["providers"]:
            await interaction.response.send_message("Unknown provider.", ephemeral=True); return
        cfg["selected_provider"] = name; cfg["selected_model"] = cfg["providers"][name]["models"][0]; persist_runtime(cfg)
        await interaction.response.send_message("Provider set to " + name, ephemeral=True)

    @tree.command(name="model", description="Select a model ID")
    async def model_cmd(interaction, name: str):
        if not await guard(interaction): return
        cfg["selected_model"] = name; persist_runtime(cfg); await interaction.response.send_message("Model set to " + name, ephemeral=True)

    @tree.command(name="mode", description="Set manual, free, or auto mode")
    async def mode_cmd(interaction, name: str):
        if not await guard(interaction): return
        name = name.lower()
        if name not in ("manual", "free", "auto"):
            await interaction.response.send_message("Use manual, free, or auto.", ephemeral=True); return
        cfg["mode"] = name; persist_runtime(cfg); await interaction.response.send_message("Mode set to " + name, ephemeral=True)

    @tree.command(name="history", description="Show recent Discord conversation")
    async def history_cmd(interaction):
        if not await guard(interaction): return
        text = "\n".join(f"{m['role']}: {short(m['content'], 200)}" for m in discord_session["messages"][-10:]) or "No conversation yet."
        await interaction.response.send_message(text[:1900], ephemeral=True)

    @tree.command(name="clear", description="Clear Discord conversation")
    async def clear_cmd(interaction):
        nonlocal discord_session
        if not await guard(interaction): return
        discord_session = new_session(); await interaction.response.send_message("Conversation cleared.", ephemeral=True)

    @tree.command(name="help", description="Show commands")
    async def help_cmd(interaction):
        if await guard(interaction):
            await interaction.response.send_message("/chat, /plan, /status, /model, /provider, /mode, /history, /clear. Plans never execute until their owner confirms.", ephemeral=True)

    @bot.event
    async def on_ready():
        try:
            await tree.sync()
        except Exception as exc:
            log_error(exc); p("Discord command sync failed: " + short(exc)); return
        p(f"Discord ready as {bot.user}; locked to channel {allowed}")

    try:
        bot.run(token, reconnect=True)
    except KeyboardInterrupt:
        p("Bot stopped.")
    except Exception as exc:
        log_error(exc); p("Discord bot could not start: " + short(exc))


def main():
    parser = argparse.ArgumentParser(description="Lightweight Termux AI assistant")
    parser.add_argument("--bot", action="store_true", help="run the Discord bot")
    parser.add_argument("--debug", action="store_true", help="print the full traceback on unexpected errors")
    args = parser.parse_args()
    try:
        cfg = load_config() or wizard()
        if args.bot: run_bot(cfg)
        else: main_menu(cfg)
    except (KeyboardInterrupt, EOFError):
        p("\nStopped safely.")
    except Exception as exc:
        log_error(exc); p("❌ Unexpected error. Details saved to data/errors.log.")
        if args.debug: traceback.print_exc()


if __name__ == "__main__":
    main()
