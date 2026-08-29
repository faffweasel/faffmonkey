#!/usr/bin/env python3
"""
GitHub release monitor via Atom feeds.

Fetches and parses Atom XML from GitHub releases feeds using stdlib only.
Handles namespace-prefixed Atom XML, classifies semver changes, filters
by date range.

Usage:
  github_releases.py                     — check all repos from config, last 7 days
  github_releases.py --days 14           — check all repos, last 14 days
  github_releases.py --repo react        — check single repo by label
  github_releases.py --owner facebook --repo react  — check by owner/repo
  github_releases.py --list              — list configured repos
  github_releases.py --json              — output as JSON

Config: reads repos from skills-data/github-deps/repos.json (legacy config/repos.json still honoured).
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

WORKSPACE = os.environ.get("WORKSPACE", "")
if not WORKSPACE:
    WORKSPACE = os.path.dirname(os.path.dirname(SKILL_DIR))
SKILL_DATA = os.environ.get(
    "SKILL_DATA", os.path.join(WORKSPACE, "skills-data", "github-deps"),
)
# Single-consumer config belongs in the skill's own data dir, like
# memory-search and selfie; config/ is for cross-skill files such as
# location.json. The old location still reads, with a nudge to move it.
REPOS_FILE = os.path.join(SKILL_DATA, "repos.json")
LEGACY_REPOS_FILE = os.path.join(WORKSPACE, "config", "repos.json")

USER_AGENT = "faffmonkey"

# Atom namespace
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def load_repos():
    """Load repos config. Returns list of repo dicts."""
    repos_file = REPOS_FILE
    if not os.path.isfile(repos_file):
        if os.path.isfile(LEGACY_REPOS_FILE):
            repos_file = LEGACY_REPOS_FILE
            print(
                f"note: reading legacy {LEGACY_REPOS_FILE}; move it to "
                f"{REPOS_FILE}",
                file=sys.stderr,
            )
        else:
            print(f"Config not found: {REPOS_FILE}", file=sys.stderr)
            return []
    try:
        with open(repos_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Cannot read {repos_file}: {e}", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        print(f"{repos_file} must contain a JSON object", file=sys.stderr)
        return []
    repos = data.get("repos", [])
    return repos if isinstance(repos, list) else []


def fetch_atom(url, timeout=20):
    """Fetch Atom XML from URL. Returns bytes or None."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml, application/xml, text/xml",
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


def parse_atom_entries(xml_bytes):
    """
    Parse Atom XML bytes into a list of release dicts.
    Handles both namespaced and non-namespaced Atom feeds.
    """
    if xml_bytes is None:
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  XML parse error: {e}", file=sys.stderr)
        return []

    # Detect namespace — GitHub feeds use the Atom namespace
    tag = root.tag
    ns = ""
    if tag.startswith("{"):
        ns = tag[1:tag.index("}")]

    def find(element, name):
        """Find child element with or without namespace."""
        if ns:
            child = element.find(f"{{{ns}}}{name}")
        else:
            child = element.find(name)
        return child

    def findall(element, name):
        if ns:
            return element.findall(f"{{{ns}}}{name}")
        return element.findall(name)

    entries = []
    for entry_el in findall(root, "entry"):
        title_el = find(entry_el, "title")
        updated_el = find(entry_el, "updated")
        link_el = find(entry_el, "link")
        id_el = find(entry_el, "id")
        author_el = find(entry_el, "author")
        content_el = find(entry_el, "content")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        updated = updated_el.text.strip() if updated_el is not None and updated_el.text else ""
        link = link_el.get("href", "") if link_el is not None else ""
        entry_id = id_el.text.strip() if id_el is not None and id_el.text else ""

        author_name = ""
        if author_el is not None:
            name_el = find(author_el, "name")
            if name_el is not None and name_el.text:
                author_name = name_el.text.strip()

        # Content may be HTML — just store raw for now
        content = ""
        if content_el is not None and content_el.text:
            content = content_el.text.strip()

        # Parse date
        parsed_date = None
        if updated:
            try:
                # GitHub format: 2026-03-20T15:30:00Z or 2026-03-20T15:30:00+00:00
                cleaned = updated.replace("Z", "+00:00")
                parsed_date = datetime.fromisoformat(cleaned)
            except ValueError:
                pass

        entries.append({
            "title": title,
            "updated": updated,
            "parsed_date": parsed_date,
            "link": link,
            "id": entry_id,
            "author": author_name,
            "content_preview": content[:200] if content else "",
        })

    return entries


def classify_version(title):
    """
    Extract version from title and classify as major/minor/patch.
    Returns (version_str, change_type) or (title, "unknown").
    """
    # Match common version patterns: v1.2.3, 1.2.3, v1.2.3-beta.1, etc.
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", title)
    if not match:
        # Try two-part version: v1.2
        match2 = re.search(r"v?(\d+)\.(\d+)", title)
        if match2:
            return f"{match2.group(1)}.{match2.group(2)}", "minor"
        return title, "unknown"

    major, minor, patch = match.group(1), match.group(2), match.group(3)
    version = f"{major}.{minor}.{patch}"

    # Classify based on which component changed (heuristic — we don't know
    # the previous version, so we use zero-checks as proxy):
    #   - x.0.0 → likely major
    #   - x.y.0 → likely minor
    #   - x.y.z → likely patch
    if minor == "0" and patch == "0":
        return version, "major"
    elif patch == "0":
        return version, "minor"
    else:
        return version, "patch"


def check_repo(owner, repo, label, days=7):
    """
    Check a single repo for releases in the last N days.
    Returns list of release dicts (may be empty).
    """
    url = f"https://github.com/{owner}/{repo}/releases.atom"
    xml_bytes = fetch_atom(url)
    entries = parse_atom_entries(xml_bytes)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    results = []
    for entry in entries:
        if entry["parsed_date"] and entry["parsed_date"] >= cutoff:
            version, change_type = classify_version(entry["title"])
            results.append({
                "label": label,
                "owner": owner,
                "repo": repo,
                "title": entry["title"],
                "version": version,
                "change_type": change_type,
                "date": entry["updated"][:10] if entry["updated"] else "unknown",
                "link": entry["link"],
                "author": entry["author"],
            })
        elif entry["parsed_date"] is None:
            # Can't filter by date — include with a warning
            version, change_type = classify_version(entry["title"])
            results.append({
                "label": label,
                "owner": owner,
                "repo": repo,
                "title": entry["title"],
                "version": version,
                "change_type": change_type,
                "date": "unknown",
                "link": entry["link"],
                "author": entry["author"],
                "warning": "Could not parse date",
            })

    return results


def check_all(repos, days=7):
    """Check all configured repos. Returns dict of label → releases."""
    all_results = {}
    for r in repos:
        owner = r.get("owner", "")
        repo = r.get("repo", "")
        label = r.get("label", f"{owner}/{repo}")
        if not owner or not repo:
            continue

        releases = check_repo(owner, repo, label, days=days)
        if releases:
            all_results[label] = releases

    return all_results


def format_results(results, quiet=False):
    """Format results for display."""
    if not results:
        print("No new releases in the configured timeframe.")
        return

    change_icons = {"major": "🔴", "minor": "🟡", "patch": "🟢", "unknown": "⚪"}

    for label, releases in results.items():
        for rel in releases:
            icon = change_icons.get(rel["change_type"], "⚪")
            print(f"{icon} {label}: {rel['title']} ({rel['change_type']}) — {rel['date']}")
            if not quiet:
                print(f"  {rel['link']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GitHub release monitor")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default: 7)")
    parser.add_argument("--repo", type=str, help="Filter to a specific repo label")
    parser.add_argument("--owner", type=str, help="GitHub owner (use with --repo for ad-hoc check)")
    parser.add_argument("--list", action="store_true", help="List configured repos")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--quiet", action="store_true", help="Compact output (no links)")

    args = parser.parse_args()

    repos = load_repos()

    if args.list:
        if not repos:
            print("No repos configured.")
        else:
            print(f"Configured repos ({len(repos)}):")
            for r in repos:
                print(f"  {r.get('label', '?'):20s}  {r['owner']}/{r['repo']}")
                print(f"    https://github.com/{r['owner']}/{r['repo']}/releases.atom")
        sys.exit(0)

    # Ad-hoc single repo check
    if args.owner and args.repo:
        releases = check_repo(args.owner, args.repo, f"{args.owner}/{args.repo}", days=args.days)
        if args.json:
            print(json.dumps(releases, indent=2, default=str))
        elif releases:
            results = {f"{args.owner}/{args.repo}": releases}
            format_results(results, quiet=args.quiet)
        else:
            print(f"No releases for {args.owner}/{args.repo} in the last {args.days} days.")
        sys.exit(0)

    # Filter by label
    if args.repo:
        repos = [r for r in repos if r.get("label", "").lower() == args.repo.lower()
                 or r.get("repo", "").lower() == args.repo.lower()]
        if not repos:
            print(f"No configured repo matching: {args.repo}")
            sys.exit(1)

    if not repos:
        print("No repos to check. Add repos to skills-data/github-deps/repos.json.")
        sys.exit(1)

    print(f"Checking {len(repos)} repos for releases in the last {args.days} days...\n")
    results = check_all(repos, days=args.days)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        format_results(results, quiet=args.quiet)

        # Summary
        total = sum(len(v) for v in results.values())
        silent = len(repos) - len(results)
        if total > 0:
            print(f"\n{total} release(s) across {len(results)} repo(s). {silent} repo(s) quiet.")
        else:
            print(f"\nAll {len(repos)} repos quiet.")
