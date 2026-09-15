# Nova Nexus 3 Engine Discord Bot

**Nova, Nexus, 3 engine.** — the community bot for the Nova Nexus 3 engine.
It only talks about the engine: `/ask` answers engine questions, plus
`/help` `/engine` `/rules` `/links` and mod tools `/kick` `/ban` `/timeout`.

## Run it on GitHub Actions (test hosting)

1. Repo **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret**. Name: `DISCORD_TOKEN`, value: your bot token
   from discord.com/developers/applications → your app → Bot.
   (The token is never committed to this repo.)
2. **Actions** tab → **Nova Nexus 3 Engine Bot** → **Run workflow**.
3. Watch the run log for `[nova-nexus] online as ...`.

Each run lasts up to 6 hours (GitHub's cap). This is test hosting, not 24/7.

## Run it on your own machine

```
pip install -r requirements.txt
set DISCORD_TOKEN=your-token-here
python -u nova_nexus_bot.py
```

## Notes

- Only one copy of the bot can be online per token. Stop other copies first.
- Welcome messages need **Server Members Intent** enabled in the Discord
  developer portal, plus `intents.members = True` in `nova_nexus_bot.py`.
