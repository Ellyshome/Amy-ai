# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

CowAgent (package name `amy-ai`) is an AI agent platform evolved from `chatgpt-on-wechat`. It supports multi-model LLM integration, multi-channel messaging (WeChat, Feishu, DingTalk, QQ, Web, Terminal), tool-calling loops, long-term memory, a declarative skill system, and plugin extensions.

## Commands

**Install dependencies:**
```bash
uv sync
# or: pip install -e .
```

**Run the project:**
```bash
make dev-local         # local dev (uv sync + run)
make dev-deploy        # Docker dev environment
make start             # start service (Docker or local)
python app.py          # or run directly
```

**Docker dev environment:**
```bash
docker compose -f docker/docker-compose.dev.yml up -d
# Web console exposed on port 9899
```

**Configuration:**
- Copy `config-template.json` to `config.json` and fill in API keys (not needed for Docker, which uses env vars)
- All available settings (~200 keys) are defined in `config.py` as `available_setting` dict with defaults
- Environment variables override config.json values

## Architecture

```
app.py (entry)
  └─ ChannelManager ──> channel_factory ──> [Web, Feishu, DingTalk, WeChat, QQ, Terminal, ...]
        │                      │
        │                ChatChannel._compose_context() / build_reply_content()
        │
        ├── Conversation mode: Bridge.fetch_reply_content() → Bot.reply() (models/)
        │
        └── Agent mode (agent: true): Bridge.fetch_agent_reply() → AgentBridge → AgentStreamExecutor
              └─ multi-step loop: think → tool_call → observe → repeat (agent/protocol/agent_stream.py)
```

### Key layers

- **`agent/`** — The modern agent core. `protocol/agent_stream.py` runs the multi-step reasoning loop with tool execution and event emission. `tools/` registers 12+ built-in tools (Read, Write, Edit, Bash, WebSearch, Memory, etc.). `memory/` provides hybrid vector+keyword search with SQLite storage. `skills/` loads declarative Markdown skills with YAML frontmatter. `prompt/` builds dynamic system prompts from context files.
- **`bridge/`** — Integration layer connecting the legacy COW message routing (`bridge.py` Bridge singleton) with the new agent subsystem (`agent_bridge.py`). `agent_event_handler.py` listens to agent execution events and streams responses back to channels.
- **`channel/`** — Message platform adapters. Each channel subdirectory implements a `ChatChannel` subclass that handles platform-specific auth, message parsing, and reply delivery. `chat_channel.py` (~25KB) is the key base class with context composition, session management, rate limiting, and plugin event hooks.
- **`models/`** — LLM provider adapters. All implement the `Bot` abstract base class (`reply()` method). `OpenAICompatibleBot` is a shared base for any OpenAI-compatible API with tool-calling support. Factory: `bot_factory.py` + type constants in `common/const.py`.
- **`plugins/`** — Event-based plugin system with 4 hooks: `ON_RECEIVE_MESSAGE`, `ON_HANDLE_CONTEXT`, `ON_DECORATE_REPLY`, `ON_SEND_REPLY`. Plugins register handlers that can modify or interrupt the event chain.
- **`common/`** — Shared utilities: logging, rate limiting (token bucket), singleton decorator, TTL dict, config constants.
- **`voice/`** — TTS/ASR abstraction over 13 providers (OpenAI, Azure, Google, etc.).
- **`skills/`** — User-defined skill Markdown files loaded at runtime.

### Configuration pattern

`config.py` defines a `Config` singleton. All keys are lowercase strings in `available_setting` with defaults. The singleton loads from `config.json` first, then environment variables override. Plugin-specific config is stored in `conf().plugin_config` dict and serialized via pickle.

Key agent-mode settings (set as env vars in docker-compose):
- `AGENT=true` — enables agent mode (otherwise conversation mode)
- `AGENT_MAX_CONTEXT_TOKENS` — max context window (default 40000)
- `AGENT_MAX_STEPS` — max tool-calling iterations per turn (default 15)

### Python version and package manager

- Python >= 3.10 (`.python-version` pins 3.10)
- Uses `uv` for package management (`pyproject.toml` + `uv.lock`)
- No setup.py; no requirements.txt in the modern path (legacy one may exist in `old/`)
