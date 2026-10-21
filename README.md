# Anonymous Stranger Chat Bot

A Telegram bot that pairs two random users and relays messages between them
anonymously. Needs only a Telegram bot token.

## Commands
| Command | What it does |
|---|---|
| /start | Welcome message and rules |
| /find | Search for a stranger |
| /next | Leave the current chat and find a new stranger |
| /stop | End the chat or cancel the search |
| /report | Report the stranger (3 different reports = automatic ban) |

## Run locally
```bash
pip install -r requirements.txt
export BOT_TOKEN="your-token"      # Windows: set BOT_TOKEN=your-token
python bot.py
```

## Deploy: GitHub + Railway
1. Get a token from @BotFather (`/newbot`).
2. Create a GitHub repo and push these files.
3. On railway.app: New Project > Deploy from GitHub repo > pick the repo.
4. Open the service > Variables > add `BOT_TOKEN` with your token.
5. Railway reads the `Procfile` and runs `python bot.py` as a worker.
   No domain or port is needed because the bot uses polling.
6. Optional: add a Volume mounted at `/data` and set `DB_PATH=/data/bot.db`
   so bans and reports survive redeploys.

## Settings (top of bot.py)
- `SEARCH_TIMEOUT`: seconds to wait for a match (60)
- `INACTIVITY_TIMEOUT`: seconds of silence before a chat closes (600)
- `REPORTS_TO_BAN`: reports needed for a ban (3)

## Notes
- Run only ONE copy of the bot at a time, or Telegram will return conflict errors.
- Active chats and the waiting list are kept in memory, so a restart ends all current chats.
- Message content is never stored or logged.
