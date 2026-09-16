> ## Self-Hosting Rules — read this first

You are welcome to use this repository's code to run your own copy of the bot, but:

- **Change the bot's name.** You may not run your copy as "Nova Nexus 3 Engine" — pick your own name.
- **Use your own Discord bot token.** You may never use the owner's bot token.
- **Follow the platform rules.** You must also follow GitHub's Terms of Service and policies, and Discord's Terms of Service and Developer Policies for bots.

Full terms: [Terms of Service](https://muse.ai/s/terms-of-service-xsxr5xql92lvxjy) · [Privacy Policy](https://muse.ai/s/privacy-policy-xxxc5xql9xuxidpv)

---
# Nova Nexus 3 Engine Discord Bot

**Nova, Nexus, 3 engine.** — the community bot for the Nova Nexus 3 engine.
It only talks about the engine: `/ask` answers engine questions, plus
`/help` `/engine` `/rules` `/links` and mod tools `/kick` `/ban` `/timeout`.

## Run it on GitHub Actions (always-on)

The repo is public, so Actions minutes are unlimited. A scheduled workflow
starts a fresh bot run every 5 hours; each new run cleanly takes over from
the previous one. A weekly keepalive commit keeps the schedule from expiring.

1. Repo **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret**. Name: `DISCORD_TOKEN`, value: your bot token
   from discord.com/developers/applications → your app → Bot.
   (The token is never committed to this repo.)
2. **Actions** tab → **Nova Nexus 3 Engine Bot** → **Run workflow** to start
   the first run immediately. The schedule takes it from there.
3. Watch the run log for `[nova-nexus] online as ...`.

Only one copy of the bot can be online per token — stop any other copies
(PC, other hosts) before starting.

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
