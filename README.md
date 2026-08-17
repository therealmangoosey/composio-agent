# Tab Assistant

A lightweight single-file AI assistant designed for **Termux on Android**, including older Samsung Tab A devices. It provides a numbered terminal UI, multi-provider chat/failover, local notes/lists, optional Composio actions, and an optional locked Discord bot.

The app intentionally keeps CPU/RAM use low: short default history, no background workers, no always-on polling, a 30-second provider timeout, and streaming is **off by default**. Everything runs only when you use it.

## Features

- OpenAI-compatible providers: GROQ, Google Gemini, OpenRouter, Cerebras, OpenAI, DeepSeek and xAI.
- Manual, Free and Auto provider modes.
- Automatic key/model failover with cooldowns and `Retry-After` handling.
- Safer handling of providers that reject `temperature` or JSON response formatting.
- Persistent chat sessions in `data/memory.json`.
- Local `ADD_TO_LIST`, `READ_LIST`, `SAVE_NOTE` and `READ_NOTE` tools.
- Approval-gated agent plans. Plans do not execute until you confirm them.
- Optional Composio direct tool execution.
- Optional Discord bot locked to one configured channel and optional user allowlist.
- Full unexpected tracebacks go to `data/errors.log`.
- API keys and Discord/Composio secrets stay in `.env`, which is gitignored.

## Termux / Samsung Tab A setup

The project is designed to run on normal Termux Python without a compiler-heavy dependency stack.

```sh
pkg update
pkg install git python -y
cd ~
git clone https://github.com/therealmangoosey/composio-agent.git
cd composio-agent
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

Python **3.10+** is recommended. The core app only needs `openai` and `requests`.

### Optional Discord bot

```sh
python -m pip install -U discord.py
```

### Optional Composio tools

```sh
python -m pip install -U composio
```

The current Composio Python SDK supports direct `composio.tools.execute(...)` calls. If a tool requires a specific toolkit version, set `composio.toolkit_version` in `data/config.json`.

## Updating the app

**Use this whenever you want to update to the latest version from GitHub:**

```sh
cd ~/composio-agent
git pull --ff-only
python -m pip install -r requirements.txt --upgrade
```

Then start it normally:

```sh
python app.py
```

For the optional Discord bot:

```sh
python app.py --bot
```

### Updating without losing your settings

`.env` and `data/` are deliberately ignored by Git, so a normal `git pull` does **not** replace your API keys, chat history, notes or logs.

If Git reports local changes before updating, check them first instead of using `git reset --hard`:

```sh
git status
git diff
```

Do **not** delete `.env` or `data/` if you want to keep your local configuration and history.

## First run

```sh
python app.py
```

The setup wizard can configure:

1. Composio API key (optional).
2. Discord token and allowed channel (optional).
3. Provider API keys, including whether each key is free-tier.
4. Default Manual / Free / Auto mode.

Secrets are written to `.env`; non-secret settings go into `data/config.json`.

## Terminal usage

```text
=== Tab Assistant | GROQ / llama-3.3-70b-versatile | free ===
1. Start / continue chat       6. Memory & history
2. Choose provider             7. Composio tools & connections
3. Choose model                8. Discord bot
4. Choose mode                 9. Settings
5. Manage API keys             0. Exit
```

Inside chat:

- `/help` — show commands
- `/new` — start a new conversation
- `/history` — show recent messages
- `/provider NAME` — change provider
- `/model NAME` — change model
- `/mode MODE` — `manual`, `free` or `auto`
- `/quit` — return to the menu
- `Ctrl-C` / EOF — safely return to the menu

## Provider modes

- **Manual** — use the selected provider/model and its first key only.
- **Free** — only keys marked FREE are used, with automatic failover.
- **Auto** — both FREE and PAID keys may be used.

A failed key is temporarily cooled down instead of being hammered repeatedly. The last successful provider/model is preferred on the next request.

## Discord bot

```sh
python app.py --bot
```

For a long-running Termux session, `tmux` is recommended:

```sh
pkg install tmux -y
tmux new -s assistant
python app.py --bot
```

Detach with `Ctrl-b`, then `d`. Reattach with:

```sh
tmux attach -t assistant
```

The bot refuses DMs and channels other than the configured channel. You can also set `allowed_user_ids` in `data/config.json` for an additional user allowlist.

## Composio

Composio is optional. The terminal assistant and local tools work without it.

The app uses the current Python SDK's direct execution interface. Composio may require a toolkit version for direct execution, so the setting is available here:

```text
data/config.json
  composio.toolkit_version
```

The default is `latest`; if Composio reports that a concrete toolkit version is required, set the version returned by Composio for that toolkit.

## API keys

Supported environment variables are:

```text
GROQ_API_KEYS=
GEMINI_API_KEYS=
OPENAI_API_KEYS=
OPENROUTER_API_KEYS=
CEREBRAS_API_KEYS=
DEEPSEEK_API_KEYS=
XAI_API_KEYS=
COMPOSIO_API_KEY=
DISCORD_BOT_TOKEN=
DISCORD_ALLOWED_CHANNEL_ID=
```

Multiple provider keys can be comma-separated. Keys are never written into `data/config.json`.

## Troubleshooting

### Check Python and dependencies

```sh
python --version
python -m pip show openai requests
```

### See the real error

```sh
python app.py --debug
```

The normal app also writes tracebacks to:

```text
data/errors.log
```

### No usable API keys

Add a key through menu 5, make sure it is marked FREE when using Free mode, or switch to Auto/Manual.

### Provider/model errors

The app automatically retries common OpenAI-compatible incompatibilities by removing optional `temperature` and JSON-format parameters. If a model still fails, use menu 3 to select a model that the provider actually exposes.

Menu 9 can fetch the current free OpenRouter model list directly from OpenRouter.

### Composio errors

Update the SDK:

```sh
python -m pip install -U composio
```

Then check `data/config.json` for `composio.toolkit_version` and the Composio connection for the requested toolkit.

## File layout

```text
app.py            # application
requirements.txt  # core Python dependencies
.env              # secrets; never commit
data/
  config.json     # non-secret settings
  memory.json     # saved sessions
  errors.log      # troubleshooting tracebacks
  leads.csv       # local list tool output
  notes.txt       # local notes
```

The `.gitignore` protects `.env`, `data/`, Python bytecode and caches.
