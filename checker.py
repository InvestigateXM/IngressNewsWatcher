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
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://ingress.com"
NEWS_URL = f"{BASE_URL}/news"
ZENDESK_API_URL = "https://support.ingress.com/api/v2/help_center/en-us/articles?page[size]=100"
STATE_FILE = Path(__file__).parent / "state.json"
REQUEST_DELAY = 0.3  # seconds between HTTP requests
FULL_SCAN_INTERVAL = 3600  # seconds (1 hour)
NON_ARTICLE_SLUGS = {"/news/events"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "IngressDiff/1.0 (https://github.com/InvestigateXM/IngressNewsWatcher)"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not read state file, starting fresh: %s", exc)
    return {"last_full_scan": None, "articles": {}, "support_articles": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch(url: str, method: str = "GET") -> requests.Response | None:
    try:
        resp = SESSION.request(method, url, timeout=15)
        time.sleep(REQUEST_DELAY)
        return resp
    except requests.RequestException as exc:
        log.warning("Request failed for %s: %s", url, exc)
        return None


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
    resp = fetch(NEWS_URL)
    if not resp or resp.status_code != 200:
        raise RuntimeError("Failed to fetch main news listing page")

    html_p1 = resp.text
    max_page = parse_max_page(html_p1)
    slugs = parse_article_slugs(html_p1)
    log.info("Page 1: %d slugs found (max_page=%d)", len(slugs), max_page)

    if full_scan and max_page > 1:
        for page in range(2, max_page + 1):
            resp = fetch(f"{NEWS_URL}?page={page}")
            if not resp or resp.status_code != 200:
                log.warning("Failed to fetch page %d, skipping", page)
                continue
            page_slugs = parse_article_slugs(resp.text)
            log.info("Page %d: %d slugs found", page, len(page_slugs))
            for s in page_slugs:
                if s not in slugs:
                    slugs.append(s)

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
    while url:
        resp = fetch(url)
        if not resp or resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch Zendesk articles: {url}")
        data = resp.json()
        articles.extend(data.get("articles", []))
        meta = data.get("meta", {})
        if meta.get("has_more") and meta.get("after_cursor"):
            url = f"{ZENDESK_API_URL}&page[after]={meta['after_cursor']}"
        else:
            url = None
    log.info("Zendesk: %d articles fetched", len(articles))
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
    except RuntimeError as exc:
        log.error("Skipping support center check: %s", exc)
        return

    current_ids = {str(a["id"]) for a in articles}
    known_ids = set(state["support_articles"].keys())
    now = now_iso()

    # Deletions: article IDs no longer returned by the API
    for article_id in list(known_ids - current_ids):
        stored = state["support_articles"][article_id]
        notify_deleted(stored["html_url"], stored["title"], "Support Center")
        del state["support_articles"][article_id]

    # New and edited articles
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
        else:
            log.debug("No change: support/%s", article_id)


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


def check_article(slug: str, state: dict) -> None:
    """Fetch article, compare to stored state, notify if changed, update state."""
    resp = fetch(f"{BASE_URL}{slug}")
    if not resp:
        log.warning("Skipping %s (fetch failed)", slug)
        return
    if resp.status_code != 200:
        log.warning("Skipping %s (HTTP %d)", slug, resp.status_code)
        return

    title, content_hash, content_text = extract_content(resp.text)
    if not content_hash:
        log.warning("Could not extract content from %s", slug)
        return

    now = now_iso()
    stored = state["articles"].get(slug)

    if stored is None:
        # Brand new article discovered
        state["articles"][slug] = {
            "title": title,
            "content_hash": content_hash,
            "content_text": content_text,
            "first_seen": now,
            "last_changed": now,
        }
        notify_new(f"{BASE_URL}{slug}", title, "Ingress News")
    elif stored["content_hash"] != content_hash:
        # Existing article was edited
        old_text = stored.get("content_text", "")
        state["articles"][slug].update({
            "title": title,
            "content_hash": content_hash,
            "content_text": content_text,
            "last_changed": now,
        })
        notify_edited(f"{BASE_URL}{slug}", title, old_text, content_text, "Ingress News", slug)
    else:
        log.debug("No change: %s", slug)


def run() -> None:
    state = load_state()

    full_scan = seconds_since(state.get("last_full_scan")) >= FULL_SCAN_INTERVAL
    scan_type = "full" if full_scan else "quick"
    log.info("Starting %s scan", scan_type)

    try:
        current_slugs, _ = crawl_listing(full_scan)
    except RuntimeError as exc:
        log.error("Aborting run: %s", exc)
        return

    current_set = set(current_slugs)
    known_set = set(state["articles"].keys())

    # Detect deletions: known articles no longer appearing in any listing page
    if full_scan:
        for slug in list(known_set - current_set):
            resp = fetch(f"{BASE_URL}{slug}", method="HEAD")
            if resp and resp.status_code == 404:
                title = state["articles"][slug].get("title", slug)
                notify_deleted(f"{BASE_URL}{slug}", title, "Ingress News")
                del state["articles"][slug]
            else:
                log.debug("Slug %s missing from listing but still reachable, keeping", slug)

    # Check content for all slugs in the current scan scope
    for slug in current_slugs:
        check_article(slug, state)

    # Support center is checked every run (single API call covers all articles)
    check_support_articles(state)

    if full_scan:
        state["last_full_scan"] = now_iso()

    save_state(state)
    log.info("Done (%s scan, %d news slugs processed)", scan_type, len(current_slugs))


if __name__ == "__main__":
    run()
