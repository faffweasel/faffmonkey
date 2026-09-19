#!/usr/bin/env python3
"""
Word-of-the-day picker with spaced repetition. Language-agnostic.

Schedules with SM-2: each word keeps its own ease factor and interval, a
pass grows the interval and a fail resets it to one day. The daily pick is
the most overdue due word, then a new word (by level) when nothing is due,
and at most one word per day. The language pair comes from the wordlist's
_meta block, so the same engine serves any learning/bridge language pair.

Usage:
  pick_word.py                    — pick today's word (JSON output)
  pick_word.py --feedback ID 3    — record score (0-5) for a word
  pick_word.py --feedback last 3  — record score for the last word sent
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

# User scores: 0=skip, 1=no idea, 2=hard, 3=ok, 4=easy, 5=already know.
# 1 is an SM-2 fail; 2-5 are passes at this SM-2 quality (0-5 scale).
SM2_QUALITY = {2: 3, 3: 4, 4: 5, 5: 5}
DEFAULT_EASE = 2.5
MIN_EASE = 1.3
# "Already know" skips the short early intervals.
KNOWN_MIN_INTERVAL = 30
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
    """A word already sent whose review date has arrived. New words are not
    reviews, so they are never due."""
    ws = state["words"].get(word_id)
    if not ws or ws.get("status") == "skip":
        return False
    next_review = ws.get("next_review")
    return not next_review or TODAY >= next_review


def _add_days(date: str, days: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def pick_word(words: dict, state: dict):
    last_id = state["stats"].get("last_word_id")
    if state["stats"].get("last_sent_date") == TODAY and last_id in words:
        return last_id, "already_sent"

    due: list[str] = []
    new_words: dict[str, list[str]] = {level: [] for level in LEVEL_ORDER}

    for wid, word in words.items():
        status = word_status(state, wid)
        if status == "new":
            level = word.get("level", "beginner")
            new_words.setdefault(level if level in new_words else "beginner", []).append(wid)
        elif is_due(state, wid):
            due.append(wid)

    if due:
        # Most overdue first; on a tie, a word the user could not recall.
        due.sort(key=lambda wid: (
            state["words"][wid].get("next_review") or "",
            state["words"][wid].get("last_score") != 1,
        ))
        wid = due[0]
        reason = "review_hard" if state["words"][wid].get("last_score") == 1 else "review"
        return wid, reason
    for level in LEVEL_ORDER:
        if new_words[level]:
            return random.choice(new_words[level]), "new"

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
    # The daily word is sent from a cron session, so the session handling
    # the score reply has not seen its id; "last" resolves to what was sent.
    if word_id == "last":
        word_id = state["stats"].get("last_word_id")
        if not word_id:
            return {"error": "No word has been sent yet"}
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

    if score is None or score not in SCORE_STATUS:
        return {
            "error": f"Unknown feedback: {feedback}."
                     " Use 0-5 (0=skip, 1=no idea, 5=already know)",
        }

    ws["status"] = SCORE_STATUS[score]
    ws["last_score"] = score
    if score == 0:
        ws.pop("next_review", None)
    else:
        schedule(ws, score)
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
        "next_review": ws.get("next_review"),
        "interval_days": ws.get("interval"),
    }


def schedule(ws: dict, score: int) -> None:
    """SM-2 update for a score of 1-5. A fail restarts the repetitions and
    keeps the ease; a pass grows the interval 1, 6, then previous x ease,
    and adjusts the ease by how easy the recall was. The interval counts
    from the day the word was sent, so a late reply does not push it back."""
    ease = ws.get("ease", DEFAULT_EASE)
    reps = ws.get("reps", 0)
    interval = ws.get("interval", 0)
    if score == 1:
        reps, interval = 0, 1
    else:
        reps += 1
        if reps == 1:
            interval = 1
        elif reps == 2:
            interval = 6
        else:
            interval = round(interval * ease)
        q = SM2_QUALITY[score]
        ease = max(MIN_EASE, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
        if score == 5:
            interval = max(interval, KNOWN_MIN_INTERVAL)
    ws["ease"] = round(ease, 2)
    ws["reps"] = reps
    ws["interval"] = interval
    ws["next_review"] = _add_days(ws.get("last_sent", TODAY), interval)


def get_stats(words: dict, state: dict) -> dict:
    statuses = {
        "new": 0, "hard": 0, "learning": 0, "familiar": 0,
        "easy": 0, "known": 0, "skip": 0,
    }
    for wid in words:
        s = word_status(state, wid)
        statuses[s] = statuses.get(s, 0) + 1
    due_count = sum(1 for wid in words if is_due(state, wid))
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
            print("Usage: pick_word.py --feedback WORD_ID|last SCORE  (0=skip, 1-5)")
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
    if reason != "already_sent":
        ws["last_sent"] = TODAY
        ws["times_sent"] = ws.get("times_sent", 0) + 1
        if "status" not in ws:
            ws["status"] = "learning"
            ws["next_review"] = _add_days(TODAY, 1)
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
