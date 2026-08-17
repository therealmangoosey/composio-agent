# Tab Assistant

A lightweight single-file AI assistant designed for **Termux on Android**, including older Samsung Tab A devices. It provides a numbered terminal UI, multi-provider chat/failover, local notes/lists, optional Composio actions, and an optional locked Discord bot.

The app intentionally keeps CPU/RAM use low: short default history, no background workers except the optional Discord bot, a 30-second provider timeout, and streaming is **off by default**.

## Features

- OpenAI-compatible providers: GROQ, Google Gemini, OpenRouter, Cerebras, OpenAI, DeepSeek and xAI.
- Current provider models are refreshed from each provider's models endpoint and cached briefly, so retired model IDs do not break failover.
- Manual, Free and Auto provider modes.
- Automatic key/model failover with cooldowns and `Retry-After` handling.
- Persistent chat sessions in `data/memory.json`.
- Local `ADD_TO_LIST`, `READ_LIST`, `SAVE_NOTE` and `READ_NOTE` tools.
- Approval-gated agent plans.
- Optional Composio direct tool execution.
- Optional Discord bot locked to one configured channel and optional user allowlist.
- Discord can start automatically in the background alongside the terminal app.
- API keys and Discord/Composio secrets stay in `.env`, which is gitignored.

## Termux / Samsung Tab A setup

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

Python **3.10+** is recommended. The core app uses the bundled pure-Python OpenAI-compatible client and `requests`, so it avoids Rust/PyO3 build problems on Termux Android.

### Discord bot dependency

```sh
python -m pip install -U discord.py
```

When `DISCORD_BOT_TOKEN` and `DISCORD_ALLOWED_CHANNEL_ID` are configured, `python app.py` starts the Discord bot in the background automatically. You do **not** need a second Termux session.

Use this only when you specifically want bot-only mode:

```sh
python app.py --bot
```

### Optional Composio tools

```sh
python -m pip install -U composio
```

## Updating the app

```sh
cd ~/composio-agent
git pull --ff-only
python -m pip install -r requirements.txt --upgrade
python app.py
```

`.env` and `data/` are deliberately ignored by Git, so a normal `git pull` does not replace your API keys, chat history, notes or logs.

## First run

```sh
python app.py
```

The setup wizard can configure the optional Composio key, Discord token/channel, provider keys and default mode.

## Terminal menu

```text
=== Tab Assistant | GROQ / openai/gpt-oss-20b | free ===
1. Start / continue chat       6. Memory & history
2. Choose provider             7. Composio tools & connections
3. Choose model                8. Discord bot (background)
4. Choose mode                 9. Settings
5. Manage API keys            10. Invite bot to a server
0. Exit
```

Menu 10 prints a Discord OAuth invite URL generated from your configured bot token.

## Discord bot

The bot is locked to the configured channel and can optionally be restricted further with `allowed_user_ids` in `data/config.json`.

Commands:

- `/chat`
- `/plan`
- `/status`
- `/provider`
- `/model`
- `/mode`
- `/history`
- `/clear`
- `/help`

## Provider model handling

The app checks the provider's active model list instead of blindly trusting old hard-coded IDs. This matters because providers retire models over time.

For example, Groq retired `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` on August 16, 2026 and recommends GPT-OSS-20B / GPT-OSS-120B replacements. OpenRouter currently provides an `openrouter/free` router for free inference.

## Composio

Composio is optional. Local tools and normal chat work without it.

## Troubleshooting

Check the environment:

```sh
python --version
python -m pip show requests
```

See the full traceback when debugging:

```sh
python app.py --debug
```

Unexpected errors are also written to `data/errors.log`.

## File layout

```text
app.py
requirements.txt
.env              # secrets; gitignored
data/             # runtime data; gitignored
```
