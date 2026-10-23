# 🚆 Indian Railways Train Ticket Availability & Seat Tracker Bot

A 24/7 autonomous Telegram alert bot that tracks train ticket availability between **Palani / Dindigul** and **Chennai**, scanning up to **60 days in advance** for open seats, cancellations, and quota releases.

Inspired by [bms-ticket-bot-v2](https://github.com/goatsept5-lab/bms-ticket-bot-v2), it leverages a **5.6-hour continuous watcher workflow** with concurrency queueing to bypass GitHub Actions cron delays and provide uninterrupted 24/7 monitoring.

---

## 🌟 Key Features

* **Complete Corridor Coverage**:
  * Palani (`PLNI`) ⇄ MGR Chennai Central (`MAS`)
  * Dindigul (`DG`) ⇄ Chennai Egmore (`MS`)
  * Dindigul (`DG`) ⇄ MGR Chennai Central (`MAS`)
* **60-Day Lookahead Calendar**: Continuously checks seat availability starting from today up to the next 60 days.
* **Smart Calendar Intelligence**: Alerts explicitly identify the day of the week and relative timing (e.g. `NEXT WEEK SATURDAY [🚨 WEEKEND]`, `TOMORROW`, `THIS SUNDAY`).
* **Zero Duplicate Spam**: State persistence via `state.json` ensures you are notified **only** when:
  * A fresh booking opens (`🆕 BOOKING OPEN`)
  * A previously sold-out or waitlisted train reopens (`🔁 RE-OPENED`)
  * Extra coaches or quota are added (`➕ SEATS ADDED`)
* **Continuous 24/7 Watcher Architecture**:
  * Runs a single long-running job for 340 minutes (~5.6 hours) checking every 5 minutes.
  * Hourly cron queues the next watcher with `concurrency: cancel-in-progress: false`, ensuring continuous hand-off.
  * Auto-commits `state.json` with `[skip ci]`.
  * Hot-reloads code changes on the fly via `git pull --rebase`.

---

## 📱 Sample Telegram Alert

```text
🚨 TRAIN SEAT ALERT: SEATS AVAILABLE! 🚨
📍 Route: Dindigul ➔ Chennai Egmore
📅 Date: Sat 31 Oct 2026 (NEXT WEEK SATURDAY [🚨 WEEKEND])
━━━━━━━━━━━━━━━━
🚆 12632 - NELLAI SF EXP
⏰ Dep: 00:15 ➔ Arr: 07:10 (6h 55m)
💺 Available Seats:
  • 3A: ✅ 20 Seats | ₹785 [🆕 BOOKING OPEN]
  • SL: ✅ 4 Seats  | ₹310 [🔁 RE-OPENED]
🔗 1-Click Book on ConfirmTkt: https://www.confirmtkt.com/rbooking/trains/from/DG/to/MS/date/31-10-2026
🔗 Or Book on IRCTC: https://www.irctc.co.in/nget/train-search
```

---

## 🚀 Setup & Deployment

### 1. Create a Telegram Bot
1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`, follow prompts, and copy the **HTTP API Token**.
3. Send `/start` to `@userinfobot` to get your numeric **Chat ID**.
4. Send a test message (e.g. `hi`) to your newly created bot to start the chat.

### 2. Add GitHub Repository Secrets
In your GitHub repo under **Settings > Secrets and variables > Actions**, add:
* `TELEGRAM_BOT_TOKEN`: Your Telegram Bot API token.
* `TELEGRAM_CHAT_ID`: Your Telegram numeric Chat ID.

### 3. Trigger the Workflow
* Push your code to `main`.
* Go to the **Actions** tab, select **Train Ticket Alert Bot**, and click **Run workflow**.
* The bot will immediately begin monitoring and keep running continuously!

---

## 💻 Local Testing

You can also run the checker locally on your PC:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables (Optional: if omitted, alerts print to console)
set TELEGRAM_BOT_TOKEN="your_bot_token"
set TELEGRAM_CHAT_ID="your_chat_id"
set SCAN_DAYS="7"

# 3. Run checker
python checker.py
```
