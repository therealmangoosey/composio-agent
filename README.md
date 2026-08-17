# Tab Assistant

A single-file, pure-Python personal AI assistant built for [Termux](https://termux.dev) on Android
(it runs anywhere Python 3 does). It gives you two frontends that share the same brain:

1. **A numbered terminal menu app** with an interactive chat, key management, memory, and tools.
2. **An optional, locked-down Discord bot** that answers slash commands in exactly one channel.

Everything lives in one file — `app.py` — plus a `data/` folder it creates beside itself. Keys and
conversations never leave your device except to the model providers you configure.

---

## What it does

### Multi-provider chat with automatic failover — free tier if you want it
Talk to any of seven OpenAI-compatible providers, switching at any time:

| Provider      | Free tier available | Example models (editable) |
|---------------|--------------------|---------------------------|
| GROQ          | ✅ | llama-3.3-70b-versatile, llama-3.1-8b-instant, llama-4-scout, gpt-oss-120b, qwen3-32b |
| Google Gemini | ✅ | gemini-2.5-flash, gemini-2.5-flash-lite |
| OpenRouter    | ✅ | llama-3.3-70b-instruct:free, deepseek-r1:free, gemma-3-27b-it:free, … |
| Cerebras      | ✅ | llama3.1-8b, gpt-oss-120b |
| OpenAI        | paid | gpt-4o-mini, gpt-4o, gpt-4.1-mini |
| DeepSeek      | paid | deepseek-chat, deepseek-v4-flash |
| xAI Grok      | paid | grok-4.6 |

Three **modes** control which keys a request may use:

- **Manual** — exactly the provider/model you selected, using its first key. No rotation.
- **Free** *(default)* — rotates only keys you marked as FREE tier, across providers, until one answers.
- **Auto** — like Free, but paid keys are eligible too.

Every key is health-tracked: rate limits and auth failures put that key on a cooldown (auth errors
cool down longer; `Retry-After` headers are honored), and the last provider/model that answered
successfully is tried first next time and restored on restart. Friendly errors tell you what
happened ("Rate limited — switching…", "Can't reach the internet", …) while full tracebacks go to
`data/errors.log`.

### Chat features
- Streaming replies (toggleable), adjustable temperature, and a configurable system prompt.
- Rolling context window (`max_history`, default 12 messages).
- Persistent sessions in `data/memory.json` — resume, delete, or clear-all from the Memory menu.
- Slash commands inside chat: `/help`, `/new`, `/history`, `/provider NAME`, `/model NAME`, `/mode MODE`, `/quit`.

### Agent plans with mandatory human approval
Ask for something multi-step ("save a note with … and add … to my leads") and the assistant builds a
JSON **plan** — a summary plus numbered steps. **Nothing ever executes without your explicit
confirmation**: `y/N` in the terminal, or ✅ / ❌ buttons in Discord (only the plan's owner can click,
and plans expire after 5 minutes).

Available tools:

- **Local tools — always work, no account needed**
  - `ADD_TO_LIST` / `READ_LIST` — a simple CRM-style `data/leads.csv` (name, email, notes).
  - `SAVE_NOTE` / `READ_NOTE` — a timestamped `data/notes.txt` journal.
- **Composio tools — optional** (Gmail, web search, news, and any other Composio action)
  - Requires the Composio SDK, an API key, and an OAuth connection for the toolkit.
  - Configured under `composio` in `data/config.json` (`toolkits`, `user_id`).

### Discord bot (optional)
Run alongside or instead of the terminal UI. It is deliberately locked down:

- Responds **only** in the one channel ID you configure — DMs and all other channels are refused.
- Optional `allowed_user_ids` allowlist in `data/config.json`.
- Slash commands: `/chat`, `/plan` (with Confirm/Deny buttons), `/status` (active model + key health),
  `/provider`, `/model`, `/mode`, `/history`, `/clear`, `/help`.

### Privacy & secrets handling
- All secrets (provider keys, Discord token/channel, Composio key) live **only** in `.env`, never in
  `data/config.json`; the included `.gitignore` excludes `.env`, `data/`, `__pycache__/`, and `*.pyc`.
- Keys are masked (`abcd…wxyz`) everywhere they are displayed.
- The first-run wizard validates your Composio key if provided, but never prints secrets.

---

## Setup

### On Termux (Android)

```sh
pkg update && pkg install python -y
pip install openai requests discord.py
# Optional: real Gmail/web/news tools via Composio
pip install composio composio-openai
```

### Anywhere else (Linux/macOS/Windows)

Python 3.10+ recommended, then the same installs:

```sh
pip install openai requests discord.py        # discord.py only needed for the bot
pip install composio composio-openai          # optional
```

Dependencies are minimal on purpose: `requests` + `openai` are required; `discord.py` and `composio`
are only imported when you use those features.

### First run — the setup wizard

```sh
python app.py
```

On first launch the wizard walks you through:

1. **Composio API key** *(optional)* — validated against Composio if given.
2. **Discord bot token + allowed channel ID** *(optional)* — only needed for the bot.
3. **Provider API keys** — for each provider, optionally add a key and mark it FREE or PAID tier.
4. **Default mode** — Manual / Free / Auto (default: Free).

It writes `data/config.json` (non-secret settings) and `.env` (all secrets). Everything is editable
later from the menus or by hand. Blank answers skip.

### Where to get API keys

[Groq](https://console.groq.com) · [Gemini](https://aistudio.google.com) ·
[OpenAI](https://platform.openai.com) · [OpenRouter](https://openrouter.ai/keys) ·
[Cerebras](https://cloud.cerebras.ai) · [DeepSeek](https://platform.deepseek.com) ·
[xAI](https://console.x.ai) · [Composio](https://platform.composio.dev)

---

## Usage

### Terminal app

```sh
python app.py
```

```
=== Tab Assistant | GROQ / llama-3.3-70b-versatile | free ===
1. Start / continue chat       6. Memory & history
2. Choose provider             7. Composio tools & connections
3. Choose model                8. Discord bot
4. Choose mode                 9. Settings
5. Manage API keys             0. Exit
```

- **Menu 5 — Manage API keys**: add / replace / delete keys, toggle free↔paid, and see per-key
  health (OK, rate-limited until HH:MM, or the last error).
- **Menu 6 — Memory & history**: list past sessions, resume one, delete one (`d NUMBER`), or clear
  all (`c`).
- **Menu 7 — Tools**: build and approve an action plan, or get Composio connection help.
- **Menu 9 — Settings**: system prompt, history length, streaming on/off, temperature, plan-approval
  on/off, and a handy **"Fetch free OpenRouter models"** that pulls the current list of $0 models
  straight from OpenRouter's API.

Inside chat, the prompt shows your live context —
`you@tab [GROQ llama-3.3-70b-versatile | free] >` — and `/quit` returns to the menu.
`Ctrl-C` anywhere backs out safely.

### Discord bot

```sh
python app.py --bot
```

For 24/7 background use on Termux, run it inside tmux:

```sh
tmux new -s ai
python app.py --bot
# detach with Ctrl-b then d; reattach later with: tmux attach -t ai
```

**Creating the Discord bot:**

1. Enable Developer Mode in Discord (Settings → Advanced).
2. Create an application + bot in the [Discord Developer Portal](https://discord.com/developers/applications) and copy the token.
3. Invite it to your server with the `bot` and `applications.commands` scopes.
4. Right-click the channel it should live in → Copy Channel ID, and enter both values in the setup
   wizard (menu 8 reminds you of the launch command).

### Flags

| Flag | Effect |
|------|--------|
| `--bot` | Run the Discord bot instead of the terminal menu. |
| `--debug` | Also print tracebacks to the screen. Without it they only go to `data/errors.log`. |

---

## File layout (created at runtime)

```
app.py            # the entire application
.env              # ALL secrets — never commit (gitignored)
data/
  config.json     # providers, models, mode, settings — secretly scrubbed
  memory.json     # saved chat sessions
  errors.log      # tracebacks for troubleshooting
  leads.csv       # local ADD_TO_LIST tool output
  notes.txt       # local SAVE_NOTE tool output
```

Because model identifiers change over time, the model lists are just data — edit the `models`
arrays in `data/config.json` (or paste a new ID at the model prompt / via `/model`) without touching
code.

## Troubleshooting

- **"No usable API keys"** — add one in menu 5, or switch out of Manual mode.
- **A key suddenly failing** — check its health in menu 5; cooldowns clear automatically.
- **Bot is silent** — confirm the token, that you're typing in the configured channel ID, and that
  the invite included the `applications.commands` scope.
- **Composio actions fail** — the SDK evolves quickly; if plans return an adapter error, reinstall
  with `pip install -U composio composio-openai` and re-do the toolkit's OAuth connection.
- Everything unexpected is logged with a full traceback in `data/errors.log`; rerun with
  `python app.py --debug` only while actively debugging.
