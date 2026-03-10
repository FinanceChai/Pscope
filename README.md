# Pscope — Polymarket Alert Bot

A Telegram bot that tracks [Polymarket](https://polymarket.com) prediction markets and fires alerts when probabilities cross your chosen threshold.

## Features

- 🔔 Personal alerts via DM — set thresholds per market, get pinged when they fire
- 📣 Channel broadcasts — admin sets alerts that post rich market cards to a public channel
- 💼 Portfolio tracking — link a wallet address and view open positions + PnL
- 🔍 Market search — paste a Polymarket URL or type a keyword
- 💸 Affiliate links — every market link includes your Polymarket referral tag

## Commands

### User (DM)
| Command | Description |
|---|---|
| `/start` | Onboarding |
| `/watch [url or keyword]` | Watch a market and set an alert threshold |
| `/alerts` | List and remove your active alerts |
| `/portfolio [0x...]` | Link wallet and view open positions |

### Admin only
| Command | Description |
|---|---|
| `/broadcast [url or keyword]` | Set a channel-wide alert (posts to channel when triggered) |
| `/broadcastlist` | List active channel alerts |
| `/broadcastremove <n>` | Remove a channel alert by index |
| `/post [url or keyword]` | Immediately post a market snapshot to the channel |

## Setup

### 1. Create your bot
Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token.

### 2. Get your Telegram user ID
Message [@userinfobot](https://t.me/userinfobot) → copy the ID for `ADMIN_ID`.

### 3. Configure environment
```bash
cp .env.example .env
# Fill in BOT_TOKEN, ADMIN_ID, CHANNEL_ID, AFFILIATE_REF
```

### 4. Install and run
```bash
pip install -r requirements.txt
export $(cat .env | xargs)
python bot.py
```

## Deploy

Cheapest always-on options:
- **Railway** — connect GitHub repo, auto-deploys on push (free tier available)
- **Fly.io** — `fly launch` from project root
- **Hetzner VPS** — €4/mo, run with `nohup python bot.py &` or systemd

## Affiliate Program

Sign up at [partners.dub.co/polymarket](https://partners.dub.co/polymarket) to get your referral link. Set `AFFILIATE_REF=?ref=yourcode` in `.env` — every market link sent by the bot will include it automatically.

Earns **$10 per referred user** who makes their first deposit + **$0.01 per click**.

## Data Persistence

Currently uses a local `db.json` file (excluded from git). For production, swap the `load_db` / `save_db` functions for a Postgres or SQLite backend.

## License

MIT
