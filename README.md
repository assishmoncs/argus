# 🤖 Argus — Enterprise AI-Powered Telegram Group Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://t.me/Argus_Group_Manager_Bot)

Argus is an enterprise-ready, modular, and extensible Telegram group management and moderation platform. It pairs the **Google Gemini 2.0 Flash API** (with LRU+TTL response caching, circuit-breaker resilience, and model fallback cascades) with a multi-tiered defensive pipeline covering lexical/regex filters, flood/spam limiters, anti-raid lockouts, CAPTCHA verification, structured telemetry, and a dependency-aware plugin system.

Experience Argus live on Telegram at: **[t.me/Argus_Group_Manager_Bot](https://t.me/Argus_Group_Manager_Bot)**

---

## 🚀 Key Enterprise Capabilities

### 🛡️ Multi-Tiered Content Moderation Pipeline
Every incoming message flows through an isolated, fault-tolerant verification pipeline:
1. **Filter Service**: Compiled regex/wildcard/exact pattern matching with LRU regex caching, ReDoS protection, zero-width character stripping, Unicode homoglyph normalization, and obfuscation detection.
2. **Spam & Flood Protection**: Thread-safe sliding-window rate limiters for floods, duplicate messages, mention spam, emoji spam, stickers, GIFs, forwards, files, and links with automatic memory garbage collection.
3. **AI Service (Gemini + Multi-Signal Heuristics)**:
   - **LRU + TTL Response Cache**: Deduplicates repeated messages in `<0.1ms` without consuming API quota.
   - **Three-State Circuit Breaker**: Automatically isolates upstream outages (`CLOSED` → `OPEN` → `HALF_OPEN`) and cascades across fallback Gemini models (`gemini-3.8-flash` → `gemini-3.5-flash-lite`).
   - **Multi-Signal Heuristic Engine**: Detects severe safety violations, crypto/phishing scams, toxic insults, and all-caps shouting locally when offline or rate-limited.

### 🗄️ Production-Scale Database Layer
- **Connection Pooling & WAL Concurrency**: Bounded `sqlite3` connection pool configured with `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout`, and 64MB page cache.
- **Atomic Transactions & Lock Retries**: `async with db.transaction()` guarantees atomic read-modify-write sequences with jittered exponential backoff on transient lock contention.
- **Read-Through Repository Cache & Audit Trail**: Hot-path group settings, roles, filters, and whitelists are cached in memory with automatic invalidation on write, backed by composite indexes and an `audit_logs` compliance table.

### 📊 Observability, Logging & Telemetry
- **Correlation IDs & Secret Scrubbing**: Every incoming update is assigned a trace correlation ID (`[cid=...]`) and passes through automatic secret redaction (`TELEGRAM_TOKEN`, `GEMINI_API_KEY`).
- **Structured JSON & Rotating File Logs**: Supports both human-readable text logs and single-line JSON logs (`LOG_FORMAT=json`) for log aggregators.
- **Real-Time Telemetry Collector**: Tracks counters, gauges, and rolling latency percentiles (`p50`, `p95`, `p99`), inspectable via `/health` or `/status`.

### 🧩 Extensible Plugin Architecture
- **Dependency & Priority Resolution**: Topological dependency sorting, priority ordering, per-plugin load timeouts, hot-reload (`reload_plugin`), router detachment on unload, and pipeline hooks (`on_startup`, `on_shutdown`, `on_pre_message`, `on_post_moderation`).

---

## 🛠️ Command Reference

### Moderation Commands
* `/warn [reason]` — Issues an atomic warning to the target user (auto-escalates at limit).
* `/unwarn` — Removes the most recent warning from the target user.
* `/warnings` — Displays current warning count for a user.
* `/mute [reason]` — Permanently mutes the target user.
* `/tmute [duration] [reason]` — Temporarily mutes the target user (`30s`, `15m`, `2h`, `1d`, `1w`).
* `/unmute` — Lifts mute restrictions from the target user.
* `/ban [reason]` — Permanently bans the target user.
* `/tban [duration] [reason]` — Temporarily bans the target user.
* `/unban` — Lifts ban restrictions from the target user.
* `/kick [reason]` / `/softban [reason]` — Kicks or softbans the target user.
* `/reset` — Resets a user's warning history (admin-only).

### Administrative & Filter Commands
* `/settings [key] [value]` — Configures group settings (`limit`, `ai`, `captcha`, `raid`).
* `/filter [pattern] [exact|wildcard|regex] [delete|warn|mute|ban]` — Adds a custom filter.
* `/delfilter [id]` / `/filters` — Deletes or lists custom filters for the chat.
* `/whitelist [word]` / `/unwhitelist [word]` — Manages whitelisted words for the chat.
* `/promote [owner|admin|moderator|trusted]` / `/demote` — Manages member authorization roles.
* `/lock` / `/unlock` — Restricts or restores group message permissions.
* `/pin` / `/unpin` — Pins or unpins messages.
* `/purge [N]` — Purges up to `N` messages (bounded up to 500 messages).
* `/welcome [template]` / `/goodbye [template]` — Custom join/leave templates (supports `[first_name]`/`{first_name}`, `[username]`/`{username}`, `[group_title]`/`{group_title}`).
* `/health` / `/status` — Displays live database pool, AI cache/circuit-breaker, and telemetry metrics.

### Notes & Custom Triggers
* `/note [trigger] [content]` — Saves a note reply linked to a trigger.
* `/delnote [trigger]` — Deletes the specified note.
* `/notes` — Lists all registered notes for the chat.
* `#trigger` — Triggers a saved note response in chat.

---

## 🔧 Installation & Setup

### 1. Quickstart
```bash
git clone https://github.com/assishmoncs/argus.git
cd argus
pip install -r requirements.txt
cp .env.example .env
```

### 2. Running the Bot & Test Suite
```bash
python main.py
python -m pytest
```
