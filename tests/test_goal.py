from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from faffmonkey.config import CompactionConfig, Config, HeartbeatConfig, ModelConfig
from faffmonkey.runtime.goal import (
    GOAL_DONE_TOKEN,
    GoalState,
    check_goal_done,
    handle_goal_command,
    make_continuation_prompt,
)
from faffmonkey.runtime.loop import AgentLoop
from faffmonkey.seams.channel_noop import NoopChannel
from faffmonkey.types import CompletionResponse, InboundMessage


def _make_config(**overrides) -> Config:
    defaults = {
        "models": {
            "main": ModelConfig(
                provider="ollama-local", model="llama3",
                base_url="http://localhost:11434/v1", api_key="",
            ),
        },
        "routing": {"conversation": "main"},
        "fallback_models": [],
        "timezone": ZoneInfo("UTC"),
        "heartbeat": HeartbeatConfig(),
        "compaction": CompactionConfig(),
        "channels": {},
        "tool_permissions": {},
    }
    defaults.update(overrides)
    return Config(**defaults)


def _make_provider(response_text: str = "working on it") -> MagicMock:
    provider = MagicMock()
    provider.complete.return_value = CompletionResponse(
        text=response_text, model="llama3",
    )
    return provider


def _make_channel(messages, poll_returns=None):
    channel = MagicMock()
    channel.receive.side_effect = messages
    channel.poll.side_effect = poll_returns or (lambda: None)
    channel.is_allowed.return_value = True
    sent = []
    channel.send.side_effect = lambda m: sent.append(m)
    return channel, sent


class TestGoalState:
    def test_default_values(self):
        goal = GoalState(text="write a poem")
        assert goal.text == "write a poem"
        assert goal.turn_count == 0
        assert goal.max_turns == 20
        assert goal.active is True


class TestHandleGoalCommand:
    def test_start_goal(self):
        response, goal = handle_goal_command("write a poem", None)
        assert goal is not None
        assert goal.text == "write a poem"
        assert goal.active is True
        assert "Goal set" in response
        assert "20" in response

    def test_reject_second_goal(self):
        existing = GoalState(text="first goal")
        response, goal = handle_goal_command("second goal", existing)
        assert goal is None
        assert "already active" in response

    def test_start_after_inactive_goal(self):
        old = GoalState(text="old", active=False)
        response, goal = handle_goal_command("new goal", old)
        assert goal is not None
        assert goal.text == "new goal"

    def test_status_active(self):
        existing = GoalState(text="test goal", turn_count=5, max_turns=20)
        response, goal = handle_goal_command("status", existing)
        assert goal is None
        assert "test goal" in response
        assert "5/20" in response
        assert "15" in response

    def test_status_no_goal(self):
        response, goal = handle_goal_command("status", None)
        assert "No active goal" in response

    def test_status_inactive_goal(self):
        old = GoalState(text="done", active=False)
        response, _ = handle_goal_command("status", old)
        assert "No active goal" in response

    def test_stop_active(self):
        existing = GoalState(text="test goal", turn_count=7)
        response, goal = handle_goal_command("stop", existing)
        assert goal is None
        assert not existing.active
        assert "7" in response

    def test_stop_no_goal(self):
        response, goal = handle_goal_command("stop", None)
        assert "No active goal" in response

    def test_stop_inactive_goal(self):
        old = GoalState(text="done", active=False)
        response, _ = handle_goal_command("stop", old)
        assert "No active goal" in response

    def test_no_args(self):
        response, goal = handle_goal_command("", None)
        assert "Usage" in response
        assert goal is None


class TestGoalDoneDetection:
    def test_at_start(self):
        assert check_goal_done(f"{GOAL_DONE_TOKEN} I finished the task")

    def test_in_middle(self):
        assert check_goal_done(f"I finished. {GOAL_DONE_TOKEN}. That's all.")

    def test_at_end(self):
        assert check_goal_done(f"All done! {GOAL_DONE_TOKEN}")

    def test_not_present(self):
        assert not check_goal_done("Still working on it")

    def test_partial_token_not_detected(self):
        assert not check_goal_done("GOAL_DON")


class TestContinuationPrompt:
    def test_format(self):
        prompt = make_continuation_prompt("write a poem")
        assert prompt == "[Continuing toward goal: write a poem]"


class TestGoalLifecycle:
    def test_start_continue_done(self):
        config = _make_config()
        provider = MagicMock()
        provider.complete.side_effect = [
            CompletionResponse(text="working...", model="llama3"),
            CompletionResponse(text="done GOAL_DONE", model="llama3"),
        ]
        channel, sent = _make_channel(
            messages=[
                InboundMessage(
                    sender_id="u1", text="/goal write a poem",
                    channel_id="test", timestamp=None,
                ),
                None,
            ],
            poll_returns=[None, None],
        )

        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=channel,
        )
        loop.run()

        texts = [s.text for s in sent]
        assert "Goal set" in texts[0]
        assert "working" in texts[1]
        assert "GOAL_DONE" in texts[2]
        assert "completed in 2 turns" in texts[2].lower()

    def test_goal_stop(self):
        config = _make_config()
        provider = _make_provider("working")
        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=NoopChannel(),
        )

        start_result = loop.handle_message("/goal write something")
        assert "Goal set" in start_result
        assert loop._goal is not None
        assert loop._goal.active

        stop_result = loop.handle_message("/goal stop")
        assert "stopped" in stop_result.lower()
        assert not loop._goal.active


class TestTurnBudget:
    def test_exhaustion(self):
        config = _make_config()
        provider = _make_provider("still working")
        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=NoopChannel(),
        )
        loop.handle_message("/goal write a poem")
        loop._goal.max_turns = 3

        results = []
        for _ in range(4):
            r = loop._goal_turn()
            if r is not None:
                results.append(r)
            if not loop._goal.active:
                break

        assert not loop._goal.active
        assert len(results) == 4
        assert "budget exhausted" in results[-1].lower()
        assert provider.complete.call_count == 3

    def test_budget_checked_before_turn(self):
        config = _make_config()
        provider = _make_provider("working")
        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=NoopChannel(),
        )
        loop._goal = GoalState(text="test", turn_count=20, max_turns=20)

        result = loop._goal_turn()
        assert "budget exhausted" in result.lower()
        assert not loop._goal.active
        provider.complete.assert_not_called()


class TestUserPreemption:
    def test_user_message_pauses_goal(self):
        config = _make_config()
        provider = MagicMock()
        provider.complete.side_effect = [
            CompletionResponse(text="turn 1 work", model="llama3"),
            CompletionResponse(text="answer: 3pm", model="llama3"),
            CompletionResponse(text="turn 2 GOAL_DONE", model="llama3"),
        ]

        user_preempt = InboundMessage(
            sender_id="u1", text="what time is it?",
            channel_id="test", timestamp=None,
        )

        channel, sent = _make_channel(
            messages=[
                InboundMessage(
                    sender_id="u1", text="/goal do something",
                    channel_id="test", timestamp=None,
                ),
                None,
            ],
            poll_returns=[None, user_preempt, None],
        )

        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=channel,
        )
        loop.run()

        texts = [s.text for s in sent]
        assert "Goal set" in texts[0]
        assert "turn 1 work" in texts[1]
        assert "3pm" in texts[2]
        assert "GOAL_DONE" in texts[3]

    def test_goal_stop_during_preemption(self):
        config = _make_config()
        provider = MagicMock()
        provider.complete.side_effect = [
            CompletionResponse(text="turn 1", model="llama3"),
        ]

        stop_msg = InboundMessage(
            sender_id="u1", text="/goal stop",
            channel_id="test", timestamp=None,
        )

        channel, sent = _make_channel(
            messages=[
                InboundMessage(
                    sender_id="u1", text="/goal do something",
                    channel_id="test", timestamp=None,
                ),
                None,
            ],
            poll_returns=[None, stop_msg],
        )

        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=channel,
        )
        loop.run()

        texts = [s.text for s in sent]
        assert "Goal set" in texts[0]
        assert "turn 1" in texts[1]
        assert "stopped" in texts[2].lower()


class TestGoalRejectSecond:
    def test_reject_via_handle_message(self):
        config = _make_config()
        provider = _make_provider()
        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=NoopChannel(),
        )

        loop.handle_message("/goal first goal")
        result = loop.handle_message("/goal second goal")
        assert "already active" in result
        assert loop._goal.text == "first goal"


class TestGoalInHelp:
    def test_goal_shows_in_help(self):
        config = _make_config()
        provider = _make_provider()
        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=NoopChannel(),
        )
        result = loop.handle_message("/help")
        assert "/goal" in result


class TestGoalStatusViaLoop:
    def test_status_shows_turn_count(self):
        config = _make_config()
        provider = _make_provider()
        loop = AgentLoop(
            resolve_provider=lambda m: provider,
            config=config, channel=NoopChannel(),
        )
        loop.handle_message("/goal write something")
        loop._goal.turn_count = 3

        result = loop.handle_message("/goal status")
        assert "write something" in result
        assert "3/20" in result
