# 🦅 Vulture — Telegram Social Graph Intelligence

A silent, data-driven Telegram bot that observes group dynamics, constructs social graphs, generates cynical psychological profiles, and renders dark cyberpunk dossier cards.

> **Privacy-first**: No raw message text is ever stored. Only metadata (timestamps, IDs, lengths, reply relationships).

---

## Features

| Feature | Description |
|---|---|
| **Silent Listener** | Logs message metadata & reactions without sending any messages |
| **Social Graph** | NetworkX-powered directed weighted graph with PageRank, degree ratios |
| **Psychological Profiler** | LLM-generated cynical rank titles & 2-sentence diagnoses |
| **Dossier Cards** | 1200×675 dark cyberpunk PNG cards rendered with Pillow |
| **Group Pulse** | Admin-only `/pulse` command for group anomaly reports |
| **Personal Dossier** | `/dossier` command in DM for individual analysis |

---

## Prerequisites

- Python 3.11+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- *(Optional)* An OpenAI-compatible API key for LLM profiling

---

## Installation

```bash
# Clone the repository
git clone <your-repo-url> vulture-bot
cd vulture-bot

# Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/macOS

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram bot token |
| `DATABASE_URL` | — | `sqlite+aiosqlite:///./vulture.db` | Async database URL |
| `LLM_API_KEY` | — | — | OpenAI / OpenRouter / Anthropic API key |
| `LLM_BASE_URL` | — | `https://api.openai.com/v1` | LLM endpoint base URL |
| `LLM_MODEL` | — | `gpt-4o-mini` | Model identifier |

### PostgreSQL (optional)

```
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/vulture
```

---

## Usage

```bash
python main.py
```

### Bot Commands

| Command | Context | Description |
|---|---|---|
| `/start` | Private DM | Show welcome message |
| `/dossier` | Private DM | Generate your personal dossier card |
| `/pulse` | Group chat | Post group anomalies summary (admin-only, 24h cooldown) |

### Setup in a Group

1. Add the bot to your Telegram group.
2. **Promote the bot to admin** — this is required for reaction tracking (see note below).
3. Let the bot silently collect metadata for a while.
4. Use `/pulse` (as an admin) to see the group report.
5. DM the bot with `/dossier` to get your personal card.

---

## ⚠️ Important: Admin Permissions

> The bot **must be promoted to group admin** to receive `MessageReactionUpdated` events from the Telegram API. Without admin status, reaction-based analytics (affinity weighting, reaction edges) will be unavailable. Message reply tracking works without admin privileges.

---

## Architecture

```
main.py                     Entrypoint: bot startup, polling
config.py                   Pydantic-settings (.env)
database.py                 SQLAlchemy 2.0 async ORM

handlers/
  group.py                  Silent listener, /pulse, membership tracking
  private.py                /start, /dossier, callback handlers

engine/
  graph.py                  NetworkX social graph analytics
  profiler.py               LLM / heuristic psychological profiler
  card_generator.py         Pillow dossier card renderer

assets/fonts/               JetBrains Mono (auto-downloaded)
```

### Database Schema

- **`groups`** — Tracked group chats (chat_id, title, join date, activity status, last report time)
- **`messages`** — Message metadata (IDs, user, reply target, timestamp, length, media flag)
- **`reactions`** — Reaction events (source user, target user, emoji, timestamp)

### Analytics Pipeline

1. **Build Graph** — Directed weighted graph from replies + reactions
2. **Edge Weighting** — `count × speed_factor + reaction_weight` (replies < 60s get 2× weight)
3. **PageRank** — Influence index (0.0 – 10.0 scale)
4. **Degree Ratios** — Gravitational / Desperate / Balanced classification
5. **Secret Dynamic** — Highest bidirectional weight pair
6. **Ignored Metric** — High out-degree with < 40% reciprocation
7. **LLM Profile** — Rank title + cynical diagnosis (heuristic fallback)
8. **Card Render** — 1200×675 cyberpunk PNG via Pillow

---

## Fonts

The card generator automatically downloads **JetBrains Mono** (Regular + Bold) from GitHub on first run. Fonts are cached in `assets/fonts/`. If the download fails, it falls back to PIL's built-in font.

---

## License

MIT
