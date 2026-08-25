import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

STALE_DAYS = 30


def _parse_entries(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    lines = text.splitlines()
    entry_starts: list[int] = []
    for i, line in enumerate(lines):
        if re.match(r"^## \[\w+-\d{8}-\d{3}\]", line):
            entry_starts.append(i)

    for pos, start in enumerate(entry_starts):
        end = entry_starts[pos + 1] if pos + 1 < len(entry_starts) else len(lines)
        header = lines[start]
        id_match = re.match(r"^## \[(\w+-\d{8}-\d{3})\]\s*(.*)", header)
        if not id_match:
            continue

        entry: dict[str, str] = {
            "id": id_match.group(1),
            "label": id_match.group(2).strip(),
            "status": "",
            "priority": "",
            "area": "",
            "summary": "",
            "details": "",
            "promoted": "",
            "date": "",
        }

        date_match = re.search(r"-(\d{8})-", entry["id"])
        if date_match:
            entry["date"] = date_match.group(1)

        for line in lines[start + 1 : end]:
            stripped = line.strip()
            for field in ("Status", "Priority", "Area", "Summary", "Details", "Promoted"):
                if stripped.startswith(f"**{field}**:"):
                    entry[field.lower()] = stripped.split(":", 1)[1].strip()
                    break

        entries.append(entry)

    return entries


def _extract_keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z]{3,}", text.lower())
    stop = {
        "the", "and", "for", "that", "this", "with", "from", "are",
        "was", "were", "been", "have", "has", "had", "not", "but",
        "can", "will", "should", "would", "could", "about", "into",
        "when", "than", "then", "also", "just", "more", "some",
        "other", "what", "which", "their", "there", "these", "those",
        "does", "did", "its", "too", "very", "after", "before",
    }
    return {w for w in words if w not in stop}


def _check_internalised(summary: str, agents_text: str, memory_text: str) -> bool:
    keywords = _extract_keywords(summary)
    if len(keywords) < 2:
        return False
    combined = (agents_text + " " + memory_text).lower()
    matches = sum(1 for kw in keywords if kw in combined)
    return matches / len(keywords) >= 0.6


def _find_similar_groups(entries: list[dict[str, str]]) -> list[list[int]]:
    groups: list[list[int]] = []
    used: set[int] = set()
    for i, a in enumerate(entries):
        if i in used:
            continue
        kw_a = _extract_keywords(a["summary"])
        if len(kw_a) < 2:
            continue
        group = [i]
        for j in range(i + 1, len(entries)):
            if j in used:
                continue
            kw_b = _extract_keywords(entries[j]["summary"])
            if len(kw_b) < 2:
                continue
            overlap = kw_a & kw_b
            smaller = min(len(kw_a), len(kw_b))
            if smaller > 0 and len(overlap) / smaller >= 0.5:
                group.append(j)
        if len(group) > 1:
            groups.append(group)
            used.update(group)
    return groups


def _find_stale(entries: list[dict[str, str]]) -> list[int]:
    stale: list[int] = []
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        today_dt = datetime.strptime(today, "%Y%m%d")
    except ValueError:
        return stale

    for i, entry in enumerate(entries):
        if entry["status"] != "pending" or not entry["date"]:
            continue
        try:
            entry_dt = datetime.strptime(entry["date"], "%Y%m%d")
            age_days = (today_dt - entry_dt).days
            if age_days > STALE_DAYS:
                stale.append(i)
        except ValueError:
            pass
    return stale


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    learnings_path = workspace / "LEARNINGS.md"

    if not learnings_path.exists():
        print("No LEARNINGS.md found.")
        return

    text = learnings_path.read_text()
    if not text.strip():
        print("LEARNINGS.md is empty.")
        return

    entries = _parse_entries(text)
    if not entries:
        print("No entries found in LEARNINGS.md.")
        return

    agents_text = ""
    agents_path = workspace / "AGENTS.md"
    if agents_path.exists():
        agents_text = agents_path.read_text()

    memory_text = ""
    memory_path = workspace / "MEMORY.md"
    if memory_path.exists():
        memory_text = memory_path.read_text()

    pending = [e for e in entries if e["status"] == "pending"]

    internalised: list[int] = []
    for i, entry in enumerate(entries):
        if entry["status"] != "pending":
            continue
        if _check_internalised(entry["summary"], agents_text, memory_text):
            internalised.append(i)

    similar_groups = _find_similar_groups(entries)
    duplicates = [g for g in similar_groups if len(g) == 2]
    promotion_candidates = [g for g in similar_groups if len(g) >= 3]

    stale = _find_stale(entries)

    print("# Self-Review Report")
    print(f"\nTotal entries: {len(entries)} ({len(pending)} pending)")

    if internalised:
        print(f"\n## Already Internalised ({len(internalised)} entries)")
        print("These appear to already be reflected in AGENTS.md or MEMORY.md.")
        print("Consider removing them from LEARNINGS.md.")
        for idx in internalised:
            print(f"  - [{entries[idx]['id']}] {entries[idx]['summary'][:80]}")

    if duplicates:
        print(f"\n## Duplicates ({len(duplicates)} groups)")
        print("These entries overlap significantly. Consider merging.")
        for group in duplicates:
            print()
            print("  Group:")
            for idx in group:
                print(f"    - [{entries[idx]['id']}] {entries[idx]['summary'][:80]}")

    if promotion_candidates:
        print(f"\n## Promotion Candidates ({len(promotion_candidates)} groups)")
        print("3+ similar entries suggest a recurring pattern. Promote to AGENTS.md or MEMORY.md.")
        for group in promotion_candidates:
            print()
            print("  Group:")
            for idx in group:
                print(f"    - [{entries[idx]['id']}] {entries[idx]['summary'][:80]}")

    if stale:
        print(f"\n## Stale ({len(stale)} entries)")
        print(f"Pending entries older than {STALE_DAYS} days. Verify or remove.")
        for idx in stale:
            print(f"  - [{entries[idx]['id']}] {entries[idx]['summary'][:80]}")

    if not internalised and not duplicates and not promotion_candidates and not stale:
        print("\n## No Issues Found")
        print("LEARNINGS.md looks clean. No pruning needed right now.")


if __name__ == "__main__":
    main()
