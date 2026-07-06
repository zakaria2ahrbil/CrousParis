# CROUS Watcher

Watches trouverunlogement.lescrous.fr for new Île-de-France listings under
405€/month and pings you on Telegram, tagged with how convenient the commute
to Sorbonne Paris Nord (Villetaneuse) is. Checks every 5 minutes, for free,
via GitHub Actions.

## Setup (~10 minutes)

### 1. Create the Telegram bot
1. Open Telegram, message **@BotFather**.
2. Send `/newbot`, follow the prompts, give it any name.
3. BotFather gives you a **token** like `123456789:AAExxxxx...` — copy it.
4. Send any message (e.g. "hi") to your new bot.
5. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in your
   browser (replace `<YOUR_TOKEN>`). Find `"chat":{"id":123456789,...}` in
   the response — that number is your **chat ID**.

### 2. Create a GitHub repo
1. Go to github.com, create a **new repository** (can be public or private —
   public repos get unlimited free Actions minutes).
2. Upload these 3 files, keeping the folder structure:
   - `crous_watcher.py`
   - `.github/workflows/watch.yml`
   - `README.md` (optional)
   You can do this by dragging files into the GitHub web UI, or via git:
   ```
   git init
   git add .
   git commit -m "initial commit"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

### 3. Add your secrets
1. In the repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID

### 4. Turn it on
1. Go to the **Actions** tab in your repo.
2. If prompted, click "I understand my workflows, enable them".
3. Click on "CROUS watcher" → **Run workflow** to test it once manually.
4. Check your Telegram — you should get pinged for any current match, or
   nothing if there's nothing matching yet (normal before July 7).

From here it runs itself every 5 minutes automatically.

## Adjusting settings

Open `crous_watcher.py` and edit near the top:
- `MAX_PRICE` — currently 405
- `IDF_POSTAL_PREFIXES` — currently all of Île-de-France
- `SPECIFIC_TIERS` / `DEPT_TIERS` — the commute preference labels

## Notes

- This only reads public listing pages — no login, no auto-booking. You
  still open the link and reserve manually.
- GitHub schedules aren't perfectly precise under load, so an alert might
  land a few minutes late sometimes — but it won't be skipped.
- The commute tiers are rough estimates. Always tap the Google Maps link
  in the alert to see the actual transit time before deciding.
