#!/usr/bin/env python3
"""
Word-of-the-day picker with spaced repetition. Language-agnostic.

Selects words using simple SRS: hard-marked reviews first, then new words
(by level), then due reviews. The language pair comes from the wordlist's
_meta block, so the same engine serves any learning/bridge language pair.

Usage:
  pick_word.py                    — pick today's word (JSON output)
  pick_word.py --feedback ID 3    — record score (0-5) for a word
  pick_word.py --stats            — learning progress
  pick_word.py --history N        — last N words with feedback
  pick_word.py --categories       — categories and counts
  pick_word.py --reset --confirm  — reset all progress

Words: skills-data/word-daily/words.json (seeded from the skill's seed/ on
first run). State: skills-data/word-daily/word-state.json.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

WORKSPACE = os.environ.get("WORKSPACE", "")
if not WORKSPACE:
    WORKSPACE = os.path.dirname(os.path.dirname(SKILL_DIR))
SKILL_DATA = os.environ.get(
    "SKILL_DATA", os.path.join(WORKSPACE, "skills-data", "word-daily"),
)

WORDS_PATH = os.path.join(SKILL_DATA, "words.json")
STATE_PATH = os.path.join(SKILL_DATA, "word-state.json")
SEED_WORDS = os.path.join(SKILL_DIR, "seed", "words.json")

# SRS intervals (days) by score: 0=skip, 1=no idea, 2=hard, 3=ok, 4=easy, 5=known
SCORE_INTERVALS = {0: 999, 1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
SCORE_STATUS = {
    0: "skip", 1: "hard", 2: "learning", 3: "familiar", 4: "easy", 5: "known",
}
LEVEL_ORDER = ["beginner", "elementary", "intermediate"]
TODAY = datetime.now().strftime("%Y-%m-%d")


def _seed_words() -> None:
    if os.path.isfile(WORDS_PATH) or not os.path.isfile(SEED_WORDS):
        return
    os.makedirs(os.path.dirname(WORDS_PATH), exist_ok=True)
    shutil.copy2(SEED_WORDS, WORDS_PATH)
    print(f"seeded {WORDS_PATH}", file=sys.stderr)


def _normalise(w: dict) -> dict:
    """Accept both neutral (word/translation) and legacy field names."""
    out = dict(w)
    out["word"] = w.get("word") or w.get("vietnamese") or ""
    out["translation"] = w.get("translation") or w.get("chinese") or ""
    return out


def load_words() -> tuple[dict, dict]:
    _seed_words()
    try:
        with open(WORDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"cannot read words.json: {e}"}))
        sys.exit(1)
    if not isinstance(data, dict):
        print(json.dumps({"error": "words.json must contain a JSON object"}))
        sys.exit(1)
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    languages = {
        "learning": meta.get("learning_language", "the learning language"),
        "bridge": meta.get("bridge_language", "English"),
    }
    # words.json is hand-maintained, so an entry can be missing its id;
    # skip it by name rather than fail the whole file.
    words = {}
    for entry in data.get("words", []):
        if not isinstance(entry, dict) or not entry.get("id"):
            print(f"skipping word entry with no id: {entry!r}", file=sys.stderr)
            continue
        words[entry["id"]] = _normalise(entry)
    return words, languages


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "words": {},
            "stats": {"total_sent": 0, "last_sent_date": None, "last_word_id": None},
        }


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def word_status(state: dict, word_id: str) -> str:
    return state["words"].get(word_id, {}).get("status", "new")


def is_due(state: dict, word_id: str) -> bool:
    ws = state["words"].get(word_id)
    if not ws:
        return True
    if ws.get("status") == "skip":
        return False
    next_review = ws.get("next_review")
    if not next_review:
        return True
    return TODAY >= next_review


def pick_word(words: dict, state: dict):
    last_id = state["stats"].get("last_word_id")

    hard_due: list[str] = []
    review_due: list[str] = []
    new_words: dict[str, list[str]] = {level: [] for level in LEVEL_ORDER}

    for wid, word in words.items():
        if wid == last_id:
            continue
        status = word_status(state, wid)
        if status == "skip":
            continue
        elif status == "known" and not is_due(state, wid):
            continue
        elif status == "new":
            level = word.get("level", "beginner")
            new_words.setdefault(level if level in new_words else "beginner", []).append(wid)
        elif status == "hard" and is_due(state, wid):
            hard_due.append(wid)
        elif is_due(state, wid):
            review_due.append(wid)

    if hard_due:
        return random.choice(hard_due), "review_hard"
    for level in LEVEL_ORDER:
        if new_words[level]:
            return random.choice(new_words[level]), "new"
    if review_due:
        return random.choice(review_due), "review"

    all_seen = [
        (wid, state["words"][wid].get("next_review", "9999"))
        for wid in words
        if wid != last_id and word_status(state, wid) != "skip"
    ]
    if all_seen:
        all_seen.sort(key=lambda x: x[1])
        return all_seen[0][0], "refresh"
    return None, None


def apply_feedback(state: dict, word_id: str, feedback: str) -> dict:
    ws = state["words"].get(word_id, {})
    feedback = feedback.strip()
    try:
        score = int(feedback)
    except ValueError:
        aliases = {
            "skip": 0, "ignore": 0,
            "hard": 1, "again": 1, "difficult": 1,
            "ok": 3, "good": 3,
            "easy": 4,
            "known": 5, "know": 5, "already": 5,
        }
        score = aliases.get(feedback.lower())

    if score is None or score not in SCORE_INTERVALS:
        return {
            "error": f"Unknown feedback: {feedback}."
                     " Use 0-5 (0=skip, 1=no idea, 5=already know)",
        }

    ws["status"] = SCORE_STATUS[score]
    ws["last_score"] = score
    ws["next_review"] = (
        datetime.now() + timedelta(days=SCORE_INTERVALS[score])
    ).strftime("%Y-%m-%d")
    ws["last_feedback"] = TODAY
    ws["feedback_count"] = ws.get("feedback_count", 0) + 1

    state["words"][word_id] = ws
    save_state(state)

    labels = {0: "skipped", 1: "no idea", 2: "hard", 3: "ok", 4: "easy", 5: "known"}
    return {
        "word_id": word_id,
        "score": score,
        "label": labels[score],
        "status": ws["status"],
        "next_review": ws["next_review"],
    }


def get_stats(words: dict, state: dict) -> dict:
    statuses = {
        "new": 0, "hard": 0, "learning": 0, "familiar": 0,
        "easy": 0, "known": 0, "skip": 0,
    }
    for wid in words:
        s = word_status(state, wid)
        statuses[s] = statuses.get(s, 0) + 1
    due_count = sum(
        1 for wid in words
        if is_due(state, wid) and word_status(state, wid) != "skip"
    )
    return {
        "total_words": len(words),
        "total_sent": state["stats"].get("total_sent", 0),
        "statuses": statuses,
        "due_for_review": due_count,
        "last_sent": state["stats"].get("last_sent_date"),
    }


def main() -> None:
    args = sys.argv[1:]
    words, languages = load_words()
    state = load_state()

    if "--stats" in args:
        print(json.dumps(get_stats(words, state), indent=2, ensure_ascii=False))
        return

    if "--categories" in args:
        cats: dict[str, int] = {}
        for w in words.values():
            cat = w.get("category", "other")
            cats[cat] = cats.get(cat, 0) + 1
        for cat, count in sorted(cats.items()):
            new = sum(
                1 for w in words.values()
                if w.get("category") == cat and word_status(state, w["id"]) == "new"
            )
            print(f"  {cat}: {count} words ({new} new)")
        return

    if "--reset" in args:
        if "--confirm" not in args:
            print("This will reset all learning progress. Add --confirm to proceed.")
            return
        save_state({
            "words": {},
            "stats": {"total_sent": 0, "last_sent_date": None, "last_word_id": None},
        })
        print("Progress reset.")
        return

    if "--feedback" in args:
        idx = args.index("--feedback")
        if idx + 2 >= len(args):
            print("Usage: pick_word.py --feedback WORD_ID SCORE  (0=skip, 1-5)")
            sys.exit(1)
        print(json.dumps(
            apply_feedback(state, args[idx + 1], args[idx + 2]),
            ensure_ascii=False,
        ))
        return

    if "--history" in args:
        idx = args.index("--history")
        n = int(args[idx + 1]) if idx + 1 < len(args) else 10
        history = []
        for wid, ws in state["words"].items():
            if "last_feedback" in ws and wid in words:
                w = words[wid]
                history.append({
                    "id": wid,
                    "word": w["word"],
                    "translation": w["translation"],
                    "status": ws["status"],
                    "last_score": ws.get("last_score"),
                    "last_feedback": ws["last_feedback"],
                })
        history.sort(key=lambda x: x["last_feedback"], reverse=True)
        print(json.dumps(history[:n], indent=2, ensure_ascii=False))
        return

    word_id, reason = pick_word(words, state)
    if not word_id:
        print(json.dumps({"error": "No words available"}))
        return

    word = words[word_id]
    ws = state["words"].get(word_id, {})
    ws["last_sent"] = TODAY
    ws["times_sent"] = ws.get("times_sent", 0) + 1
    if "status" not in ws:
        ws["status"] = "learning"
        ws["next_review"] = (
            datetime.now() + timedelta(days=1)
        ).strftime("%Y-%m-%d")
    state["words"][word_id] = ws
    state["stats"]["total_sent"] = state["stats"].get("total_sent", 0) + 1
    state["stats"]["last_sent_date"] = TODAY
    state["stats"]["last_word_id"] = word_id
    save_state(state)

    print(json.dumps({
        "id": word_id,
        "word": word["word"],
        "translation": word["translation"],
        "pronunciation": word.get("pronunciation", ""),
        "category": word.get("category", ""),
        "level": word.get("level", ""),
        "notes": word.get("notes", ""),
        "reason": reason,
        "status": ws.get("status", "learning"),
        "times_sent": ws.get("times_sent", 1),
        "total_sent": state["stats"]["total_sent"],
        "languages": languages,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
