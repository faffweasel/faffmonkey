from __future__ import annotations

from dataclasses import dataclass

GOAL_DONE_TOKEN = "GOAL_DONE"


@dataclass
class GoalState:
    text: str
    turn_count: int = 0
    max_turns: int = 20
    active: bool = True


def handle_goal_command(
    args: str, current_goal: GoalState | None,
) -> tuple[str, GoalState | None]:
    args = args.strip()
    if not args:
        return "Usage: /goal <text>, /goal status, /goal stop", None

    first_word = args.split(None, 1)[0].lower()

    if first_word == "status":
        if current_goal is None or not current_goal.active:
            return "No active goal.", None
        remaining = current_goal.max_turns - current_goal.turn_count
        return (
            f"Goal: {current_goal.text}\n"
            f"Turns: {current_goal.turn_count}/{current_goal.max_turns}"
            f" (budget remaining: {remaining})"
        ), None

    if first_word == "stop":
        if current_goal is None or not current_goal.active:
            return "No active goal to stop.", None
        turns = current_goal.turn_count
        current_goal.active = False
        return f"Goal stopped after {turns} turns.", None

    if current_goal is not None and current_goal.active:
        return "A goal is already active. Use /goal stop to cancel it first.", None

    new_goal = GoalState(text=args)
    return (
        f"Goal set: {args}\n"
        f"Starting autonomous execution (budget: {new_goal.max_turns} turns)."
    ), new_goal


def check_goal_done(response_text: str) -> bool:
    return GOAL_DONE_TOKEN in response_text


def make_continuation_prompt(goal_text: str) -> str:
    return f"[Continuing toward goal: {goal_text}]"
