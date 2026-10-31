"""Check news sources and post new articles to Telegram.

Environment variables:
  TELEGRAM_BOT_TOKEN  bot token from @BotFather
  TELEGRAM_CHAT_ID    chat, group or channel to post to
  TELEGRAM_THREAD_ID  optional: topic ID inside a group that has Topics turned on
  DRY_RUN=1           print messages instead of sending them

State (which articles were already sent) is kept in seen.json.
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.yaml"
SEEN_FILE = ROOT / "seen.json"

# Forget sent items after this long so seen.json does not grow forever.
SEEN_TTL = timedelta(days=21)
# Safety cap so a broken source can never flood the chat in one run.
MAX_PER_SOURCE_PER_RUN = 25
# Telegram allows roughly 20 messages per minute into a group or channel.
SEND_DELAY_SECONDS = 3.1

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def fetch(url):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp


def clean_text(raw):
    """Strip HTML tags and entities from a feed summary."""
    return " ".join(BeautifulSoup(raw or "", "html.parser").get_text(" ").split())


def items_from_rss(source):
    feed = feedparser.parse(fetch(source["url"]).content)
    suffix = source.get("strip_title_suffix")
    items = []
    for entry in feed.entries:
        link = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        if suffix and title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        media = entry.get("media_content") or entry.get("media_thumbnail") or []
        images = [m["url"] for m in media if m.get("url")]
        images += [e["href"] for e in entry.get("enclosures", []) if e.get("type", "").startswith("image")]
        items.append({
            "id": entry.get("id") or link,
            "title": title,
            "link": link,
            "summary": clean_text(entry.get("summary")) if source.get("summary", True) else "",
            "author": entry.get("author") or (entry.get("source") or {}).get("title", ""),
            "published": datetime(*published[:6], tzinfo=timezone.utc) if published else None,
            "image": images[0] if images else "",
        })
    return items


def items_from_scrape(source):
    soup = BeautifulSoup(fetch(source["url"]).text, "html.parser")
    pattern = re.compile(source["link_pattern"])
    titles = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(source["url"], a["href"]).split("?")[0].split("#")[0]
        if not pattern.match(link):
            continue
        text = a.get_text(" ", strip=True)
        # The same article is often linked several times (image, headline); keep the longest text.
        if len(text) > len(titles.get(link, "")):
            titles[link] = text
    return [{"id": link, "title": title, "link": link} for link, title in titles.items()]


def page_meta(link):
    """Read headline, summary, image, author and date from an article page's meta tags."""
    try:
        soup = BeautifulSoup(fetch(link).text, "html.parser")
    except requests.RequestException:
        return {}

    def meta(*keys):
        for key in keys:
            tag = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content", "").strip():
                return tag["content"].strip()
        return ""

    author = meta("author", "article:author", "parsely-author")
    if author.startswith("http"):  # some sites put a profile URL here
        author = ""
    if not author:  # fall back to structured data: {"author": {"@type": "Person", "name": ...}}
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except ValueError:
                continue
            for obj in data if isinstance(data, list) else [data]:
                people = obj.get("author") if isinstance(obj, dict) else None
                people = people if isinstance(people, list) else [people]
                names = [p["name"] for p in people if isinstance(p, dict) and p.get("@type") == "Person" and p.get("name")]
                if names:
                    author = ", ".join(names)
                    break
            if author:
                break

    published = None
    raw_date = meta("article:published_time", "datePublished")
    if raw_date:
        try:
            published = datetime.fromisoformat(re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", raw_date))
        except ValueError:
            pass

    return {
        "title": meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else ""),
        "summary": meta("og:description", "description"),
        "image": meta("og:image", "twitter:image"),
        "author": author,
        "published": published,
    }


def enrich(item, source):
    """Fill in whatever the feed left out (photo, summary, author, date) from the article page."""
    item = {"summary": "", "author": "", "published": None, "image": "", **item}
    if source.get("fetch_page", True):
        page = page_meta(item["link"])
        item["image"] = page.get("image") or item["image"]  # page images are usually larger
        for key in ("title", "summary", "author", "published"):
            item[key] = item[key] or page.get(key)
    item["title"] = item["title"] or item["link"].rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()
    item["author"] = source.get("author") or item["author"] or source["name"]
    return item


# Dates are shown in Singapore time.
LOCAL_TZ = timezone(timedelta(hours=8))
# Telegram caps photo captions at 1024 characters.
CAPTION_LIMIT = 1024


def format_post(item, source):
    """Same layout as rss.app: linked headline, summary, then 'Author | site • Mon D, YYYY'."""
    domain = source.get("domain") or urlparse(item["link"]).netloc.removeprefix("www.")
    when = item["published"] or datetime.now(timezone.utc)
    when = when.astimezone(LOCAL_TZ)
    footer = f"{item['author']} | {domain} • {when:%b} {when.day}, {when.year}"
    head = f'<a href="{html.escape(item["link"], quote=True)}">{html.escape(item["title"])}</a>'

    summary = item["summary"]
    if summary == item["title"]:
        summary = ""
    room = CAPTION_LIMIT - len(item["title"]) - len(footer) - 10
    if len(summary) > room:
        summary = summary[: max(0, room - 1)].rsplit(" ", 1)[0] + "…"
    parts = [head] + ([html.escape(summary)] if summary else []) + [html.escape(footer)]
    return "\n\n".join(parts)


def telegram(method, thread_id=None, files=None, **fields):
    """Call the Bot API, retrying when rate-limited. thread_id defaults to TELEGRAM_THREAD_ID."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    fields["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    thread_id = str(thread_id or os.environ.get("TELEGRAM_THREAD_ID", "")).strip()
    if thread_id:
        fields["message_thread_id"] = int(thread_id)
    for _ in range(3):
        url = f"https://api.telegram.org/bot{token}/{method}"
        if files:
            resp = requests.post(url, data={k: json.dumps(v) if isinstance(v, dict) else v for k, v in fields.items()},
                                 files=files, timeout=60)
        else:
            resp = requests.post(url, json=fields, timeout=60)
        if resp.status_code == 429:
            time.sleep(resp.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        return resp
    return resp


def send_telegram(text, thread_id=None, image=None):
    """Send a post: as a photo with caption when there's an image, otherwise as text."""
    if os.environ.get("DRY_RUN") == "1":
        print(f"[dry run, topic {thread_id or os.environ.get('TELEGRAM_THREAD_ID', '-')}, image {image or '-'}]\n{text}\n")
        return
    if image:
        resp = telegram("sendPhoto", thread_id, photo=image, caption=text, parse_mode="HTML")
        if resp.ok:
            return
        # Telegram couldn't fetch the image itself (blocked or odd format): download and upload it.
        try:
            img = fetch(image)
            resp = telegram("sendPhoto", thread_id, files={"photo": ("image.jpg", img.content)},
                            caption=text, parse_mode="HTML")
            if resp.ok:
                return
        except requests.RequestException:
            pass
        print(f"  photo failed, sending as text: {resp.text[:200]}", file=sys.stderr)
    resp = telegram("sendMessage", thread_id, text=text, parse_mode="HTML",
                    link_preview_options={"is_disabled": True})
    resp.raise_for_status()


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    cutoff = (datetime.now(timezone.utc) - SEEN_TTL).isoformat()
    for name in seen:
        seen[name] = {k: v for k, v in seen[name].items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True), encoding="utf-8")


def load_dotenv():
    """For local runs: read KEY=value lines from .env (never committed). GitHub uses Secrets instead."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def main():
    load_dotenv()
    sources = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]

    if "--test" in sys.argv:
        # One test message per topic in use, naming the sources that post there.
        topics = {}
        for source in sources:
            topics.setdefault(source.get("topic_id"), []).append(source["name"])
        for topic_id, names in topics.items():
            send_telegram(f"✅ RSS Hub test. This topic will receive: {', '.join(names)}", topic_id)
            print(f"Test message sent for: {', '.join(names)}")
        return

    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat()
    source_failures = 0
    send_failures = 0

    for source in sources:
        name = source["name"]
        try:
            if source["type"] == "rss":
                items = items_from_rss(source)
            elif source["type"] == "scrape":
                items = items_from_scrape(source)
            else:
                raise ValueError(f"unknown type {source['type']!r}")
        except Exception as exc:  # one broken site should not stop the others
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)
            source_failures += 1
            continue

        if not items:
            print(f"[{name}] WARNING: 0 items found, the site layout may have changed", file=sys.stderr)

        first_run = name not in seen
        source_seen = seen.setdefault(name, {})
        new = [i for i in items if i["id"] not in source_seen]

        if first_run:
            # Don't dump the whole backlog the first time; just remember what's there.
            for item in new:
                source_seen[item["id"]] = now
            print(f"[{name}] first run: remembered {len(new)} existing items, sent nothing")
            continue

        # Feeds list newest first; send oldest first so the chat reads in order.
        to_send = list(reversed(new[:MAX_PER_SOURCE_PER_RUN]))
        unsent_ids = {i["id"] for i in to_send}
        # Refresh everything still listed (so old items on slow pages never expire and repeat),
        # except items we are about to send, which are marked only once Telegram accepts them.
        for item in items:
            if item["id"] not in unsent_ids:
                source_seen[item["id"]] = now
        print(f"[{name}] {len(items)} items, {len(new)} new, sending {len(to_send)}")

        for item in to_send:
            item = enrich(item, source)
            try:
                send_telegram(format_post(item, source), source.get("topic_id"), item["image"])
                source_seen[item["id"]] = now
            except requests.RequestException as exc:
                # Left unmarked, so it is retried next run.
                print(f"[{name}] send failed: {exc}", file=sys.stderr)
                send_failures += 1
            time.sleep(0 if os.environ.get("DRY_RUN") == "1" else SEND_DELAY_SECONDS)

    save_seen(seen)
    # Fail the run (so GitHub emails you) if Telegram rejected us or every source broke.
    if send_failures or source_failures == len(sources):
        sys.exit(1)


if __name__ == "__main__":
    main()
