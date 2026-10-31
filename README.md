# RSS Hub

Checks news sites every 15 minutes and posts new articles to Telegram. Runs free on GitHub Actions.

| Source | How |
|---|---|
| SBR Insurance | Reads https://sbr.com.sg/insurance (no feed for this section) |
| Al Jazeera | Official RSS feed |
| Reuters | Google News search for `site:reuters.com` (Reuters has no public RSS). Posts have no photo or summary |
| Yahoo Finance SG | Official RSS feed (top stories) |

Sources are listed in [sources.yaml](sources.yaml). Edit that file to add or remove sites.

## One-time setup

1. **Create the bot.** In Telegram, message **@BotFather** → `/newbot` → pick a name. Copy the token it gives you.
2. **Add the bot to your group.** Group → Add members → search for your bot's username.
   Make sure members (or the bot, if you make it an admin) are allowed to post in the target topic.
3. **Get the group ID and topic ID.** In the Telegram app (desktop is easiest), open the topic,
   right-click any message in it → **Copy Message Link**. You get a link like:

   ```
   https://t.me/c/1234567890/42/1057
                  ^^^^^^^^^^ ^^
                  group      topic
   ```

   - Group ID = `-100` + the first number → `-1001234567890`
   - Topic ID = the second number → `42`

   If the link has only two numbers (`/c/1234567890/1057`), the message is in the **General** topic:
   leave the topic ID out.
   If your group is public, the link looks like `t.me/groupname/42/1057`; use `@groupname` as the group ID.
4. **Add the secrets on GitHub.** Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `TELEGRAM_BOT_TOKEN` = the token from step 1
   - `TELEGRAM_CHAT_ID` = the group ID from step 3
   - `TELEGRAM_THREAD_ID` = the topic ID from step 3
5. **Start it.** Repo → Actions → RSS Hub → Run workflow.

The first run only remembers what's already on each site and sends nothing, so you don't get a flood of old
articles. From the next run onward you get only new articles.

## How it behaves

- Sent articles are recorded in `seen.json`, which the workflow commits back to the repo.
- At most 25 messages per source per run, so a broken site can't spam you.
- If one site fails, the others still run. You get a GitHub email if Telegram rejects the messages or every site fails.
- If a site prints `0 items found` in the Actions log, its layout probably changed (most likely SBR).

## Test locally

```bash
pip install -r requirements.txt
DRY_RUN=1 python rss_hub.py
```

To check your Telegram settings, put them in a `.env` file (never committed) and send one test message:

```bash
python rss_hub.py --test
```

Delete `seen.json` afterwards so the first GitHub run starts clean.
