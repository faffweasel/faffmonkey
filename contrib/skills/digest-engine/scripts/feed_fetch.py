#!/usr/bin/env python3
"""
RSS/Atom feed fetcher for the digest engine — with dedup tracking.

Fetches and parses RSS and Atom feeds using the stdlib XML parser.
Returns structured items for the agent to filter and compose into digests.
Tracks seen items to avoid surfacing the same content across runs.

Usage:
  feed_fetch.py                          — fetch all digests (dedup on)
  feed_fetch.py --digest Boxing          — fetch feeds for one digest
  feed_fetch.py --url <feed_url>         — fetch a single feed URL (no dedup)
  feed_fetch.py --list                   — list configured digests and feeds
  feed_fetch.py --days 7                 — only show items from last N days
  feed_fetch.py --json                   — output as JSON
  feed_fetch.py --include-seen           — show all items (ignore dedup)
  feed_fetch.py --reset Boxing           — clear seen history for a digest
  feed_fetch.py --seen-stats             — show dedup stats per digest

Config: workspace/skills-data/digest-engine/digests.json (legacy config/digests.json still honoured)
Seen data: skills-data/digest-engine/seen/DIGEST_NAME.json

This script handles the RSS/Atom parsing that web_fetch does poorly. The
agent calls it for feed items, then handles web_search terms and filtering.
"""

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

WORKSPACE = os.environ.get("WORKSPACE", "")
if not WORKSPACE:
    WORKSPACE = os.path.dirname(os.path.dirname(SKILL_DIR))

SKILL_DATA = os.environ.get(
    "SKILL_DATA", os.path.join(WORKSPACE, "skills-data", "digest-engine"),
)
# Single-consumer config lives in the skill's own data dir; config/ is for
# cross-skill files like location.json. Legacy location still reads.
DIGESTS_FILE = os.path.join(SKILL_DATA, "digests.json")
LEGACY_DIGESTS_FILE = os.path.join(WORKSPACE, "config", "digests.json")
SEEN_DIR = os.path.join(SKILL_DATA, "seen")

USER_AGENT = "faffmonkey/0.1.0"

# Auto-prune seen entries older than this
SEEN_MAX_AGE_DAYS = 90


# --- Seen item tracking ---

def _seen_path(digest_name):
    """Path to the seen-items file for a digest."""
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', digest_name.lower())
    return os.path.join(SEEN_DIR, f"{safe_name}.json")


def load_seen(digest_name):
    """
    Load seen items for a digest.
    Returns dict: {guid_hash: {"title": str, "first_seen": str, "last_seen": str}}
    """
    path = _seen_path(digest_name)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: Could not read {path}: {e}", file=sys.stderr)
        return {}


def save_seen(digest_name, seen):
    """Save seen items for a digest. Auto-prunes old entries."""
    os.makedirs(SEEN_DIR, exist_ok=True)
    path = _seen_path(digest_name)

    # Prune entries older than SEEN_MAX_AGE_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_MAX_AGE_DAYS)).isoformat()
    pruned = {
        k: v for k, v in seen.items()
        if v.get("last_seen", "") >= cutoff[:10]  # Compare date prefix
    }

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pruned, f, indent=2)
    except OSError as e:
        print(f"Warning: Could not write {path}: {e}", file=sys.stderr)


def reset_seen(digest_name):
    """Clear seen history for a digest."""
    path = _seen_path(digest_name)
    if os.path.isfile(path):
        os.remove(path)
        print(f"Cleared seen history: {path}")
    else:
        print(f"No seen history found for: {digest_name}")


def item_hash(item):
    """Generate a stable hash for an item. Uses guid, falling back to title+link."""
    key = item.get("guid") or f"{item.get('title', '')}|{item.get('link', '')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def mark_seen(seen, items):
    """Mark items as seen. Returns the updated seen dict."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for item in items:
        h = item_hash(item)
        if h in seen:
            seen[h]["last_seen"] = now
        else:
            seen[h] = {
                "title": (item.get("title") or "")[:100],
                "first_seen": now,
                "last_seen": now,
            }
    return seen


def filter_unseen(items, seen):
    """Filter items to only those not in the seen set. Returns (new_items, seen_count)."""
    new_items = []
    seen_count = 0
    for item in items:
        h = item_hash(item)
        if h in seen:
            seen_count += 1
        else:
            new_items.append(item)
    return new_items, seen_count


# --- Feed fetching and parsing ---

def load_digests():
    """Load digests config."""
    digests_file = DIGESTS_FILE
    if not os.path.isfile(digests_file):
        if os.path.isfile(LEGACY_DIGESTS_FILE):
            digests_file = LEGACY_DIGESTS_FILE
            print(
                f"note: reading legacy {LEGACY_DIGESTS_FILE}; move it to "
                f"{DIGESTS_FILE}",
                file=sys.stderr,
            )
        else:
            print(f"Config not found: {DIGESTS_FILE}", file=sys.stderr)
            return []
    try:
        with open(digests_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Cannot read {digests_file}: {e}", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        print(f"{digests_file} must contain a JSON object", file=sys.stderr)
        return []
    digests = data.get("digests", [])
    return digests if isinstance(digests, list) else []


def fetch_feed(url, timeout=20):
    """Fetch feed XML from URL. Returns bytes or None."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} fetching {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Error fetching {url}: {e}", file=sys.stderr)
        return None


def _get_text(element, tag, ns=""):
    """Get text content of a child element, with optional namespace."""
    if ns:
        child = element.find(f"{{{ns}}}{tag}")
    else:
        child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _get_link(element, ns=""):
    """Extract link from element — handles both RSS and Atom patterns."""
    if ns:
        link_el = element.find(f"{{{ns}}}link")
    else:
        link_el = element.find("link")

    if link_el is not None:
        href = link_el.get("href", "")
        if href:
            return href
        if link_el.text:
            return link_el.text.strip()
    return ""


def _parse_date(date_str):
    """Parse various date formats found in feeds."""
    if not date_str:
        return None

    for parse in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            dt = parse(date_str)
        except Exception:
            continue
        # Both parsers return naive datetimes for a bare date or a -0000
        # offset, and the freshness cutoff is aware, so normalise to UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    return None


def _clean_html(text):
    """Strip HTML tags for plain text preview."""
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def parse_feed(xml_bytes, source_url=""):
    """Parse RSS or Atom feed XML. Auto-detects format."""
    if xml_bytes is None:
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  XML parse error for {source_url}: {e}", file=sys.stderr)
        return []

    tag = root.tag.lower()

    if "rss" in tag or root.find("channel") is not None:
        return _parse_rss(root, source_url)
    elif "feed" in tag:
        return _parse_atom(root, source_url)
    else:
        items = _parse_rss(root, source_url)
        if items:
            return items
        return _parse_atom(root, source_url)


def _parse_rss(root, source_url):
    """Parse RSS feed."""
    items = []
    channel = root.find("channel")
    if channel is None:
        channel = root

    feed_title = _get_text(channel, "title") or source_url

    for item in channel.findall("item"):
        title = _get_text(item, "title")
        link = _get_text(item, "link") or _get_link(item)
        description = _get_text(item, "description")
        pub_date = _get_text(item, "pubDate")
        guid = _get_text(item, "guid")
        author = _get_text(item, "author") or _get_text(item, "{http://purl.org/dc/elements/1.1/}creator")

        items.append({
            "title": title,
            "link": link,
            "description": _clean_html(description)[:300],
            "date_str": pub_date,
            "date": _parse_date(pub_date),
            "author": author,
            "guid": guid or link,
            "feed": feed_title,
            "source_url": source_url,
        })

    return items


def _parse_atom(root, source_url):
    """Parse Atom feed."""
    items = []
    tag = root.tag
    ns = ""
    if tag.startswith("{"):
        ns = tag[1:tag.index("}")]

    feed_title = _get_text(root, "title", ns) or source_url

    entry_tag = f"{{{ns}}}entry" if ns else "entry"
    for entry in root.findall(entry_tag):
        title = _get_text(entry, "title", ns)
        link = _get_link(entry, ns)
        updated = _get_text(entry, "updated", ns)
        published = _get_text(entry, "published", ns)
        entry_id = _get_text(entry, "id", ns)
        content = _get_text(entry, "content", ns) or _get_text(entry, "summary", ns)

        author = ""
        author_tag = f"{{{ns}}}author" if ns else "author"
        author_el = entry.find(author_tag)
        if author_el is not None:
            author = _get_text(author_el, "name", ns)

        date_str = published or updated
        items.append({
            "title": title,
            "link": link,
            "description": _clean_html(content)[:300],
            "date_str": date_str,
            "date": _parse_date(date_str),
            "author": author,
            "guid": entry_id or link,
            "feed": feed_title,
            "source_url": source_url,
        })

    return items


def fetch_digest_feeds(digest, days=None):
    """Fetch all RSS feeds for a digest. Returns list of items."""
    rss_urls = digest.get("sources", {}).get("rss", [])
    all_items = []

    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for url in rss_urls:
        print(f"  Fetching: {url}", file=sys.stderr)
        xml_bytes = fetch_feed(url)
        items = parse_feed(xml_bytes, source_url=url)

        for item in items:
            if cutoff and item["date"] and item["date"] < cutoff:
                continue
            all_items.append(item)

    all_items.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return all_items


def format_items(items, max_items=None):
    """Format items for display."""
    if not items:
        print("  No items found.")
        return

    shown = 0
    for item in items:
        if max_items and shown >= max_items:
            remaining = len(items) - shown
            if remaining > 0:
                print(f"\n  ... and {remaining} more items")
            break

        date_display = item["date"].strftime("%Y-%m-%d") if item["date"] else "no date"
        print(f"\n  [{date_display}] {item['title']}")
        if item["link"]:
            print(f"    {item['link']}")
        if item["description"]:
            desc = item["description"][:150]
            print(f"    {desc}{'...' if len(item['description']) > 150 else ''}")
        shown += 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Digest feed fetcher with dedup")
    parser.add_argument("--digest", type=str, help="Fetch feeds for a specific digest by name")
    parser.add_argument("--url", type=str, help="Fetch a single feed URL (no dedup)")
    parser.add_argument("--days", type=int, help="Only show items from last N days")
    parser.add_argument("--list", action="store_true", help="List configured digests")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--max", type=int, default=20, help="Max items to show (default: 20)")
    parser.add_argument("--include-seen", action="store_true", help="Show all items including previously seen")
    parser.add_argument("--reset", type=str, metavar="DIGEST", help="Clear seen history for a digest")
    parser.add_argument("--seen-stats", action="store_true", help="Show dedup stats per digest")

    args = parser.parse_args()

    # Reset mode
    if args.reset:
        reset_seen(args.reset)
        sys.exit(0)

    # Stats mode
    if args.seen_stats:
        digests = load_digests()
        for d in digests:
            seen = load_seen(d["name"])
            if seen:
                dates = [v.get("first_seen", "") for v in seen.values()]
                oldest = min(dates) if dates else "?"
                newest = max(dates) if dates else "?"
                print(f"{d['name']:20s}  {len(seen):>4} seen items  (oldest: {oldest}, newest: {newest})")
            else:
                print(f"{d['name']:20s}     0 seen items")
        print(f"\nSeen data location: {SEEN_DIR}")
        sys.exit(0)

    # Single URL mode (no dedup)
    if args.url:
        print(f"Fetching: {args.url}", file=sys.stderr)
        xml_bytes = fetch_feed(args.url)
        items = parse_feed(xml_bytes, source_url=args.url)

        if args.days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
            items = [i for i in items if not i["date"] or i["date"] >= cutoff]

        if args.json:
            serialisable = [{k: (v.isoformat() if isinstance(v, datetime) else v)
                            for k, v in item.items()} for item in items]
            print(json.dumps(serialisable, indent=2))
        else:
            print(f"Feed: {items[0]['feed'] if items else 'unknown'} ({len(items)} items)")
            format_items(items, max_items=args.max)
        sys.exit(0)

    digests = load_digests()

    if args.list:
        if not digests:
            print("No digests configured.")
        else:
            for d in digests:
                rss = d.get("sources", {}).get("rss", [])
                web = d.get("sources", {}).get("web_search", [])
                seen = load_seen(d["name"])
                print(f"\n{d['name']} (cron: {d.get('schedule', 'none')}, {len(seen)} seen)")
                if rss:
                    print(f"  RSS feeds ({len(rss)}):")
                    for url in rss:
                        print(f"    {url}")
                if web:
                    print(f"  Web searches ({len(web)}):")
                    for q in web:
                        print(f"    \"{q}\"")
                print(f"  Filter: {d.get('filter', 'none')}")
        sys.exit(0)

    # Filter to specific digest
    if args.digest:
        digests = [d for d in digests if d["name"].lower() == args.digest.lower()]
        if not digests:
            print(f"No digest named: {args.digest}")
            sys.exit(1)

    if not digests:
        print("No digests to process.")
        sys.exit(1)

    for digest in digests:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"Digest: {digest['name']}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        all_items = fetch_digest_feeds(digest, days=args.days)

        # Dedup
        seen = load_seen(digest["name"])
        if args.include_seen:
            items = all_items
            seen_count = 0
        else:
            items, seen_count = filter_unseen(all_items, seen)

        # Mark all fetched items as seen (including ones we filtered out)
        seen = mark_seen(seen, all_items)
        save_seen(digest["name"], seen)

        if seen_count > 0:
            print(f"  Filtered {seen_count} previously seen item(s)", file=sys.stderr)

        if args.json:
            serialisable = [{k: (v.isoformat() if isinstance(v, datetime) else v)
                            for k, v in item.items()} for item in items]
            output = {
                "digest": digest["name"],
                "filter": digest.get("filter", ""),
                "web_searches": digest.get("sources", {}).get("web_search", []),
                "rss_items": serialisable,
                "rss_item_count": len(serialisable),
                "seen_filtered": seen_count,
                "total_fetched": len(all_items),
            }
            print(json.dumps(output, indent=2))
        else:
            new_label = f" ({len(items)} new)" if seen_count > 0 else ""
            print(f"\n{digest['name']} — {len(all_items)} RSS items, {seen_count} seen{new_label}")
            if digest.get("sources", {}).get("web_search"):
                searches = digest["sources"]["web_search"]
                print(f"  (also needs {len(searches)} web search(es) — run those separately)")
            format_items(items, max_items=digest.get("max_items", args.max))
