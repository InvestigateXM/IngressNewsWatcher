#!/usr/bin/env python3
"""Ingress news and support center change monitor. Sends Telegram notifications for new, edited, or deleted articles."""

import difflib
import hashlib
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False  # Windows — locking skipped

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://ingress.com"
NEWS_URL = f"{BASE_URL}/news"
ZENDESK_API_URL = "https://support.ingress.com/api/v2/help_center/en-us/articles?page[size]=100"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup?id=576505181&country=us"
STATE_FILE = Path(__file__).parent / "state.json"
LOCK_FILE = Path(__file__).parent / ".lock"
FULL_SCAN_INTERVAL = 3600  # seconds (1 hour)
NEWS_FETCH_WORKERS = 10    # concurrent article fetches
NON_ARTICLE_SLUGS = {"/news/events"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "IngressDiff/1.0 (https://github.com/InvestigateXM/IngressNewsWatcher)"


class RateLimitError(RuntimeError):
    """Raised when a server persistently returns 429 after all retries."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not read state file, starting fresh: %s", exc)
    return {"last_full_scan": None, "articles": {}, "support_articles": {}, "app_store": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch(url: str, method: str = "GET") -> requests.Response | None:
    """Fetch a URL, retrying on 429 with Retry-After backoff.
    Raises RateLimitError if the server keeps rate limiting after all retries."""
    for attempt in range(1, 4):
        try:
            resp = SESSION.request(method, url, timeout=15)
        except requests.RequestException as exc:
            log.warning("Request failed for %s: %s", url, exc)
            return None
        if resp.status_code != 429:
            return resp
        retry_after = int(resp.headers.get("Retry-After", 5 * attempt))
        log.warning("Rate limited by server (attempt %d/3), backing off %ds: %s", attempt, retry_after, url)
        time.sleep(retry_after)
    raise RateLimitError(f"Persistent rate limiting after 3 attempts: {url}")


# ---------------------------------------------------------------------------
# Listing page parsing
# ---------------------------------------------------------------------------

def parse_max_page(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    nav = soup.find("nav", {"aria-label": "pagination"})
    if not nav:
        return 1
    page_nums = [
        int(m.group(1))
        for a in nav.find_all("a", href=True)
        if (m := re.search(r"page=(\d+)", a["href"]))
    ]
    return max(page_nums, default=1)


def parse_article_slugs(html: str) -> list[str]:
    """Return unique article slugs from a listing page, excluding non-article paths."""
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    slugs = []
    for a in soup.find_all("a", href=re.compile(r"^/news/")):
        href = a["href"].rstrip("/")
        if href in NON_ARTICLE_SLUGS:
            continue
        if href not in seen:
            seen.add(href)
            slugs.append(href)
    return slugs


def crawl_listing(full_scan: bool) -> tuple[list[str], int]:
    """
    Fetch listing pages and return (all_slugs, max_page).
    Quick scan: page 1 only. Full scan: all pages.
    """
    log.info("(News Blog) Fetching listing page 1...")
    resp = fetch(NEWS_URL)
    if not resp or resp.status_code != 200:
        raise RuntimeError("Failed to fetch main news listing page")

    html_p1 = resp.text
    max_page = parse_max_page(html_p1)
    slugs = parse_article_slugs(html_p1)
    log.info("(News Blog) Page 1: %d articles found (max_page=%d)", len(slugs), max_page)

    if full_scan and max_page > 1:
        for page in range(2, max_page + 1):
            log.info("(News Blog) Fetching listing page %d/%d...", page, max_page)
            resp = fetch(f"{NEWS_URL}?page={page}")
            if not resp or resp.status_code != 200:
                log.warning("(News Blog) Failed to fetch page %d, skipping", page)
                continue
            page_slugs = parse_article_slugs(resp.text)
            log.info("(News Blog) Page %d: %d articles found", page, len(page_slugs))
            for s in page_slugs:
                if s not in slugs:
                    slugs.append(s)
        log.info("(News Blog) Full listing crawl complete: %d articles across %d pages", len(slugs), max_page)

    return slugs, max_page


# ---------------------------------------------------------------------------
# Article content extraction
# ---------------------------------------------------------------------------

def extract_content(html: str) -> tuple[str, str, str]:
    """
    Returns (title, content_hash, content_text).
    Strips CSS module class attrs before hashing so CSS-only deploys don't trigger false positives.
    """
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article", {"aria-labelledby": "news-title"})
    if not article:
        return "", "", ""

    # Remove share/footer section inside article before stripping classes
    for tag in article.find_all("div"):
        classes = " ".join(tag.get("class", []))
        if "share" in classes:
            tag.decompose()

    # Strip class attributes — CSS module hashes change on every CSS rebuild
    for tag in article.find_all(True):
        tag.attrs.pop("class", None)

    title_tag = article.find("h1", id="news-title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    content_text = article.get_text(separator="\n", strip=True)
    content_hash = hashlib.sha256(content_text.encode()).hexdigest()
    return title, content_hash, content_text


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _tg_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment / .env")
    return token


def _tg_chat() -> str:
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID not set in environment / .env")
    return chat


_tg_last_send: float = 0.0  # monotonic timestamp of last successful Telegram POST


def _tg_post(endpoint: str, max_retries: int = 5, **kwargs) -> None:
    """POST to a Telegram Bot API endpoint, retrying on 429 with the server-specified delay."""
    global _tg_last_send
    url = f"https://api.telegram.org/bot{_tg_token()}/{endpoint}"
    for attempt in range(1, max_retries + 1):
        # Enforce one message per second regardless of endpoint
        elapsed = time.monotonic() - _tg_last_send
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        try:
            resp = requests.post(url, timeout=30, **kwargs)
            _tg_last_send = time.monotonic()
        except Exception as exc:
            log.warning("Telegram %s error: %s", endpoint, exc)
            return
        if resp.ok:
            return
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code == 429:
            retry_after = body.get("parameters", {}).get("retry_after", 5)
            log.warning("Telegram rate limit hit, waiting %ds (attempt %d/%d)", retry_after, attempt, max_retries)
            time.sleep(retry_after + 1)
        else:
            log.warning("Telegram %s failed: %s", endpoint, resp.text)
            return
    log.warning("Telegram %s gave up after %d attempts", endpoint, max_retries)


def send_message(text: str) -> None:
    _tg_post(
        "sendMessage",
        json={"chat_id": _tg_chat(), "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
    )


def send_document(caption: str, filename: str, content: bytes) -> None:
    _tg_post(
        "sendDocument",
        data={"chat_id": _tg_chat(), "caption": caption, "parse_mode": "HTML"},
        files={"document": (filename, content, "text/plain")},
    )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify_new(url: str, title: str, source: str) -> None:
    log.info("NEW [%s]: %s", source, url)
    send_message(f'🆕 <b>[{source}] New article:</b> <a href="{url}">{title}</a>')


def notify_edited(url: str, title: str, old_text: str, new_text: str, source: str, key: str) -> None:
    log.info("EDITED [%s]: %s", source, url)

    old_lines = [l + "\n" for l in old_text.splitlines()]
    new_lines = [l + "\n" for l in new_text.splitlines()]
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after", n=3))
    diff_text = "".join(diff_lines)

    caption = f'✏️ <b>[{source}] Article updated:</b> <a href="{url}">{title}</a>'

    if diff_text.strip():
        safe_name = re.sub(r"[^a-z0-9-]", "-", key.lstrip("/")) + ".txt"
        send_document(caption, safe_name, diff_text.encode("utf-8"))
    else:
        send_message(caption)


def notify_deleted(url: str, title: str, source: str) -> None:
    log.info("DELETED [%s]: %s", source, url)
    send_message(f'🗑️ <b>[{source}] Article removed:</b> <a href="{url}">{title}</a>')


# ---------------------------------------------------------------------------
# Zendesk support center
# ---------------------------------------------------------------------------

def fetch_zendesk_articles() -> list[dict]:
    """Fetch all articles from the Zendesk Help Center API, following cursor pagination."""
    articles = []
    url = ZENDESK_API_URL
    page = 1
    log.info("(Support Center) Fetching articles...")
    while url:
        resp = fetch(url)
        if not resp or resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch Zendesk articles (page {page}): {url}")
        data = resp.json()
        batch = data.get("articles", [])
        articles.extend(batch)
        log.info("(Support Center) Page %d: %d articles fetched (%d total so far)", page, len(batch), len(articles))
        meta = data.get("meta", {})
        if meta.get("has_more") and meta.get("after_cursor"):
            url = f"{ZENDESK_API_URL}&page[after]={meta['after_cursor']}"
            page += 1
        else:
            url = None
    log.info("(Support Center) Fetching complete: %d articles across %d page(s)", len(articles), page)
    return articles


def extract_support_content(body_html: str) -> tuple[str, str]:
    """Strip HTML from a Zendesk article body. Returns (content_hash, content_text)."""
    soup = BeautifulSoup(body_html or "", "lxml")
    content_text = soup.get_text(separator="\n", strip=True)
    content_hash = hashlib.sha256(content_text.encode()).hexdigest()
    return content_hash, content_text


def check_support_articles(state: dict) -> None:
    """Fetch all Zendesk articles, compare to state, and notify on changes."""
    if "support_articles" not in state:
        state["support_articles"] = {}

    try:
        articles = fetch_zendesk_articles()
    except RateLimitError as exc:
        log.error("Skipping support center check: %s", exc)
        send_message("⚠️ <b>[Support Center]</b> Rate limited by support.ingress.com. Support check skipped this run.")
        return
    except RuntimeError as exc:
        log.error("Skipping support center check: %s", exc)
        return

    current_ids = {str(a["id"]) for a in articles}
    known_ids = set(state["support_articles"].keys())
    now = now_iso()
    counts = {"new": 0, "edited": 0, "removed": 0, "unchanged": 0}

    # Deletions: article IDs no longer returned by the API
    removed_ids = known_ids - current_ids
    if removed_ids:
        log.info("(Support Center) %d article(s) no longer in API, marking as removed", len(removed_ids))
    for article_id in list(removed_ids):
        stored = state["support_articles"][article_id]
        notify_deleted(stored["html_url"], stored["title"], "Support Center")
        del state["support_articles"][article_id]
        counts["removed"] += 1

    # New and edited articles
    log.info("(Support Center) Checking %d articles for changes...", len(articles))
    for article in articles:
        article_id = str(article["id"])
        title = article.get("title") or article.get("name", "")
        html_url = article["html_url"]
        content_hash, content_text = extract_support_content(article.get("body", ""))

        stored = state["support_articles"].get(article_id)
        if stored is None:
            state["support_articles"][article_id] = {
                "title": title,
                "html_url": html_url,
                "content_hash": content_hash,
                "content_text": content_text,
                "first_seen": now,
                "last_changed": now,
            }
            notify_new(html_url, title, "Support Center")
            counts["new"] += 1
        elif stored["content_hash"] != content_hash:
            old_text = stored.get("content_text", "")
            state["support_articles"][article_id].update({
                "title": title,
                "html_url": html_url,
                "content_hash": content_hash,
                "content_text": content_text,
                "last_changed": now,
            })
            notify_edited(html_url, title, old_text, content_text, "Support Center", f"support-{article_id}")
            counts["edited"] += 1
        else:
            log.debug("(Support Center) No change: %s", article_id)
            counts["unchanged"] += 1

    if counts["new"] == 0 and counts["edited"] == 0 and counts["removed"] == 0:
        log.info("(Support Center) No changes found (%d checked)", counts["unchanged"])
    else:
        log.info("(Support Center) Done: %d new, %d edited, %d removed, %d unchanged",
                 counts["new"], counts["edited"], counts["removed"], counts["unchanged"])


# ---------------------------------------------------------------------------
# App Store monitor
# ---------------------------------------------------------------------------

def notify_app_update(version: str, release_date: str, notes: str, store_url: str, notes_revised: bool = False) -> None:
    formatted_date = release_date[:10]  # YYYY-MM-DD from ISO timestamp
    if notes_revised:
        header = f'📱 <b>[Ingress iOS]</b> Release notes revised for <b>v{version}</b> ({formatted_date})\n\n'
    else:
        header = f'📱 <b>[Ingress iOS]</b> New update available: <b>v{version}</b> ({formatted_date})\n\n'

    full_text = header + (notes or "No release notes provided.")

    if len(full_text) <= 4096:
        send_message(full_text)
    else:
        caption = header.strip()
        send_document(caption, f"changelog-v{version}.txt", (notes or "").encode("utf-8"))


def check_app_store(state: dict) -> None:
    """Fetch the latest Ingress iOS version from the iTunes Lookup API and notify on updates."""
    if "app_store" not in state:
        state["app_store"] = {}

    log.info("(App Store) Fetching latest Ingress iOS version...")
    try:
        resp = fetch(ITUNES_LOOKUP_URL)
    except RateLimitError as exc:
        log.error("(App Store) Rate limited, skipping: %s", exc)
        return
    if not resp or resp.status_code != 200:
        log.warning("(App Store) Failed to fetch iTunes data (HTTP %s)", resp.status_code if resp else "none")
        return

    try:
        result = resp.json().get("results", [{}])[0]
        version = result["version"]
        notes = result.get("releaseNotes", "")
        release_date = result.get("currentVersionReleaseDate", "")
        store_url = result.get("trackViewUrl", "https://apps.apple.com/app/id576505181")
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("(App Store) Could not parse iTunes response: %s", exc)
        return

    notes_hash = hashlib.sha256(notes.encode()).hexdigest()
    now = now_iso()
    stored = state["app_store"].get("ingress_ios")

    if stored is None:
        state["app_store"]["ingress_ios"] = {
            "version": version,
            "release_notes_hash": notes_hash,
            "release_notes": notes,
            "release_date": release_date,
            "first_seen": now,
            "last_changed": now,
        }
        log.info("(App Store) Baseline saved: Ingress iOS v%s — no notification on first run", version)
    elif stored["version"] != version:
        log.info("(App Store) New version detected: v%s -> v%s", stored["version"], version)
        state["app_store"]["ingress_ios"].update({
            "version": version,
            "release_notes_hash": notes_hash,
            "release_notes": notes,
            "release_date": release_date,
            "last_changed": now,
        })
        notify_app_update(version, release_date, notes, store_url)
    elif stored["release_notes_hash"] != notes_hash:
        log.info("(App Store) Release notes revised for v%s", version)
        state["app_store"]["ingress_ios"].update({
            "release_notes_hash": notes_hash,
            "release_notes": notes,
            "last_changed": now,
        })
        notify_app_update(version, release_date, notes, store_url, notes_revised=True)
    else:
        log.info("(App Store) No change: Ingress iOS v%s", version)


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seconds_since(iso_ts: str | None) -> float:
    if not iso_ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_ts)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return float("inf")


def _fetch_article(slug: str) -> tuple[str, str, str]:
    """Fetch and extract a single article. Runs in worker threads.
    Returns (title, content_hash, content_text), or raises RateLimitError / returns ("","","") on error."""
    try:
        resp = fetch(f"{BASE_URL}{slug}")
    except RateLimitError:
        raise
    if not resp:
        log.warning("(News Blog) Skipping %s (fetch failed)", slug)
        return "", "", ""
    if resp.status_code != 200:
        log.warning("(News Blog) Skipping %s (HTTP %d)", slug, resp.status_code)
        return "", "", ""
    title, content_hash, content_text = extract_content(resp.text)
    if not content_hash:
        log.warning("(News Blog) Could not extract content from %s", slug)
        return "", "", ""
    return title, content_hash, content_text


def _process_article(slug: str, title: str, content_hash: str, content_text: str, state: dict, now: str) -> str:
    """Compare pre-fetched article content against state, notify if changed, update state.
    Runs in the main thread. Returns 'new', 'edited', or 'unchanged'."""
    stored = state["articles"].get(slug)
    if stored is None:
        state["articles"][slug] = {
            "title": title,
            "content_hash": content_hash,
            "content_text": content_text,
            "first_seen": now,
            "last_changed": now,
        }
        notify_new(f"{BASE_URL}{slug}", title, "Ingress News")
        return "new"
    elif stored["content_hash"] != content_hash:
        old_text = stored.get("content_text", "")
        state["articles"][slug].update({
            "title": title,
            "content_hash": content_hash,
            "content_text": content_text,
            "last_changed": now,
        })
        notify_edited(f"{BASE_URL}{slug}", title, old_text, content_text, "Ingress News", slug)
        return "edited"
    else:
        log.debug("(News Blog) No change: %s", slug)
        return "unchanged"


def run() -> None:
    state = load_state()

    full_scan = seconds_since(state.get("last_full_scan")) >= FULL_SCAN_INTERVAL
    scan_type = "full" if full_scan else "quick"
    log.info("Starting %s scan", scan_type)

    try:
        current_slugs, _ = crawl_listing(full_scan)
    except RateLimitError as exc:
        log.error("Aborting run: %s", exc)
        send_message("⚠️ <b>[Ingress News]</b> Rate limited by ingress.com while fetching the news listing. Run aborted.")
        return
    except RuntimeError as exc:
        log.error("Aborting run: %s", exc)
        return

    current_set = set(current_slugs)
    known_set = set(state["articles"].keys())

    # Detect deletions: known articles no longer appearing in any listing page
    if full_scan:
        missing = known_set - current_set
        if missing:
            log.info("(News Blog) Checking %d article(s) missing from listing...", len(missing))
        for slug in list(missing):
            resp = fetch(f"{BASE_URL}{slug}", method="HEAD")
            if resp and resp.status_code == 404:
                title = state["articles"][slug].get("title", slug)
                notify_deleted(f"{BASE_URL}{slug}", title, "Ingress News")
                del state["articles"][slug]
            else:
                log.debug("(News Blog) %s missing from listing but still reachable, keeping", slug)

    # Fetch all article pages concurrently, then process results in the main thread
    log.info("(News Blog) Fetching %d articles concurrently (%d workers)...", len(current_slugs), NEWS_FETCH_WORKERS)
    fetched: dict[str, tuple[str, str, str]] = {}
    rate_limited_slugs: list[str] = []

    with ThreadPoolExecutor(max_workers=NEWS_FETCH_WORKERS) as executor:
        future_to_slug = {executor.submit(_fetch_article, slug): slug for slug in current_slugs}
        for future in as_completed(future_to_slug):
            slug = future_to_slug[future]
            try:
                fetched[slug] = future.result()
            except RateLimitError:
                log.error("(News Blog) Rate limited fetching %s", slug)
                rate_limited_slugs.append(slug)

    if rate_limited_slugs:
        send_message(
            f"⚠️ <b>[Ingress News]</b> Rate limited by ingress.com for "
            f"{len(rate_limited_slugs)} article(s). They will be retried next run."
        )

    log.info("(News Blog) Checking %d articles for changes...", len(fetched))
    now = now_iso()
    news_counts: dict[str, int] = {"new": 0, "edited": 0, "unchanged": 0, "error": len(rate_limited_slugs)}
    for slug in current_slugs:
        if slug not in fetched:
            continue  # was rate limited
        title, content_hash, content_text = fetched[slug]
        if not content_hash:
            news_counts["error"] += 1
            continue
        result = _process_article(slug, title, content_hash, content_text, state, now)
        news_counts[result] += 1

    if news_counts["new"] == 0 and news_counts["edited"] == 0:
        log.info("(News Blog) No changes found (%d checked)", news_counts["unchanged"])
    else:
        log.info("(News Blog) Done: %d new, %d edited, %d unchanged",
                 news_counts["new"], news_counts["edited"], news_counts["unchanged"])

    # Support center and App Store are checked every run (single lightweight requests each)
    check_support_articles(state)
    check_app_store(state)

    if full_scan:
        state["last_full_scan"] = now_iso()

    save_state(state)
    log.info("Scan complete (%s)", scan_type)


if __name__ == "__main__":
    if _FCNTL_AVAILABLE:
        _lock_fh = open(LOCK_FILE, "w")
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("Another instance is still running, skipping this run.")
            _lock_fh.close()
            sys.exit(0)
    run()
