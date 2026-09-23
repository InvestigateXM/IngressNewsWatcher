# IngressNewsWatcher

A Python script that monitors the [Ingress](https://ingress.com/news) news blog and [Ingress Support Center](https://support.ingress.com) for changes, sending Telegram notifications when articles are added, edited, or removed.

## Features

- Detects **new**, **edited**, and **deleted** articles on both the Ingress news blog and the Zendesk support center
- Watches the [in-game events calendar](https://prod-ingress.ingress.com/plfe/events) for new events, edits (e.g. extended end dates) and events pulled before their end date; new-event messages note any skipped internal event IDs
- Sends a **unified diff** as a `.txt` file attachment when an article is edited, showing exactly what changed with 3 lines of context
- **Two-tier scan**: checks page 1 of the news blog every run (for fast detection of new posts) and performs a full scan of all pages once per hour
- Robust against site CSS rebuilds — only fires on actual content changes, not navigation/footer/styling updates
- Respects Telegram's rate limits (1 message/second enforced, automatic retry on 429)

## Requirements

- Python 3.10+
- A Telegram bot token and channel/chat ID

## Setup

**1. Clone the repository**
```bash
git clone https://github.com/InvestigateXM/IngressNewsWatcher.git
cd IngressNewsWatcher
```

**2. Create a virtual environment and install dependencies**
```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**3. Configure credentials**
```bash
cp .env.example .env
```
Edit `.env` and fill in your Telegram bot token and chat ID. For a channel, prefix the chat ID with `-100` (e.g. `-1001234567890`).

**4. Run once to build the initial state**
```bash
venv/bin/python checker.py
```
This crawls all pages and saves a `state.json` baseline. No notifications are sent on the first run.

**5. Schedule with cron (Linux/VPS)**
```cron
*/5 * * * * cd /path/to/IngressNewsWatcher && venv/bin/python checker.py >> /var/log/ingressnewswatcher.log 2>&1
```

## Notifications

| Event | Message |
|-------|---------|
| New article | 🆕 `[Source] New article:` with link |
| Edited article | ✏️ `[Source] Article updated:` with link + diff `.txt` attachment |
| Deleted article | 🗑️ `[Source] Article removed:` with link |

Source is either `Ingress News` or `Support Center`.

| Event | Message |
|-------|---------|
| New calendar event | 🗓️ `[Ingress Events] New event:` with dates, location, internal ID, and a skipped-ID note if applicable |
| Changed calendar event | ✏️ `[Ingress Events] Event updated:` with `old → new` per changed field |
| Event removed before its end date | 🗑️ `[Ingress Events] Event removed before its end date:` |

Recurring events (First Saturday, Second Sunday) moving to their next date and events leaving the calendar after they end are silent.

## Files

| File | Description |
|------|-------------|
| `checker.py` | Main script |
| `requirements.txt` | Python dependencies |
| `.env.example` | Credentials template |
| `.env` | Your credentials (gitignored) |
| `state.json` | Persisted article state (gitignored) |
