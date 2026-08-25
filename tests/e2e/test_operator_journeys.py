"""Round trips an operator actually performs, asserted on observable state.

Every test here drives a real install through the real code path and then
asks what is true afterwards: what is in sessions.db, what is on disk, what
went out to the channel. None of them assert on call counts, because a call
count says a function ran, not that it did anything.
"""

from __future__ import annotations

from tests.e2e.scripted_provider import message, tool_call
from faffmonkey.runtime.session import MAIN_SESSION_KEY
from tests.fakes import FakeChannel, inbound


class TestATurnWorksAtAll:
    """The harness itself: init, config parse, wiring, provider, persistence."""

    def test_a_reply_reaches_the_user_and_the_history(self, install_factory):
        with install_factory([message("Lisbon is 21 degrees.")]) as install:
            loop = install.loop()
            reply = loop.handle_message("what is the weather")

            assert reply == "Lisbon is 21 degrees."
            assert "Lisbon is 21 degrees." in install.history_text()
            assert "what is the weather" in install.history_text()
            install.script.assert_exhausted()

    def test_the_system_prompt_carries_every_load_bearing_section(
        self, install_factory,
    ):
        """init copies the templates and bootstrap assembles them.

        Each of these is separately tested; nothing checked that they all
        arrive in one prompt on a real install.
        """
        with install_factory([message("ok")]) as install:
            install.loop().handle_message("hello")

            system = install.script.requests[0]["messages"][0]
            assert system["role"] == "system"
            prompt = system["content"]

            # The identity templates init wrote.
            assert "You are a personal AI agent" in prompt
            # The tool summary, without which the model does not know its tools.
            assert "Tools:" in prompt
            assert "file_read" in prompt
            # The injection defence: the policy and the wrapper explainer.
            assert "## Instruction sources" in prompt
            assert "<untrusted nonce=" in prompt


class TestCronDeliveryEntersTheConversation:
    """C8: the agent had no record of the message it had just sent.

    Replying to a morning briefing landed in a history containing no
    briefing. Code and docs agreed with each other and both were wrong, so
    only a round trip catches it.
    """

    def test_a_delivered_briefing_is_in_the_history_the_user_replies_into(
        self, install_factory,
    ):
        with install_factory([
            message("Morning. One thing today: the visa appointment."),
            message("I told you about the visa appointment."),
        ]) as install:
            channel = FakeChannel(allowed_users=["me"])
            scheduler = install.scheduler(channels={"cli": channel})
            install.set_jobs([{
                "id": "briefing",
                "schedule": "0 7 * * *",
                "prompt": "brief me",
                "session": "agent",
                "deliver": {"mode": "announce", "channel": "cli"},
            }])

            from faffmonkey.runtime.scheduler import load_jobs
            job = load_jobs(install.workspace)[0]
            result = scheduler.run_job(job)
            assert result.status == "success"

            # The operator saw it.
            assert any("visa appointment" in m.text for m in channel.sent)

            # And so did the conversation they are about to reply into: the
            # one main session every channel loop under faff run shares.
            assert "visa appointment" in install.history_text(MAIN_SESSION_KEY)

            reply = install.loop(session_key=MAIN_SESSION_KEY).handle_message(
                "what did you tell me this morning?",
            )
            assert reply == "I told you about the visa appointment."

            # The briefing was actually in the request, not merely in the db.
            sent = install.script.requests[-1]["messages"]
            assert any(
                "visa appointment" in (m.get("content") or "")
                for m in sent
            )


class TestCompactionIsWiredToTheLoop:
    """An early return at the top of _maybe_compact left 2474 tests green.

    Every unit worked; nothing drove the loop far enough to need it.
    """

    def test_a_long_conversation_gets_compacted(self, install_factory):
        turns = 60
        with install_factory(
            [message(f"reply {i}") for i in range(turns)]
            + [message("SUMMARY: the user asked many things.")] * 5
        ) as install:
            config = install.read_config()
            config["compaction"] = {
                "threshold": 0.8,
                "hard_message_limit": 20,
                "protect_last_n": 4,
            }
            install.write_config(config)

            loop = install.loop()
            for i in range(turns):
                loop.handle_message(f"question {i}")

            history = install.history()
            # Compaction ran: the history is bounded well below the turns
            # that went into it, and a summary is present.
            assert len(history) < turns, (
                f"history grew to {len(history)} messages, compaction never ran"
            )
            assert any(m.role == "system" for m in history)


class TestToolsActuallyRun:
    def test_a_file_write_tool_call_puts_a_file_in_the_workspace(
        self, install_factory,
    ):
        with install_factory([
            tool_call("file_write", {"path": "notes.md", "content": "buy milk"}),
            message("Written."),
        ]) as install:
            reply = install.loop().handle_message("note that I should buy milk")

            assert reply == "Written."
            assert (install.workspace / "notes.md").read_text() == "buy milk"
            install.script.assert_exhausted()

    def test_a_file_tool_cannot_reach_state(self, install_factory):
        """state/ holds .env. The container is the real boundary, but the
        path check is the one the agent meets first.

        Asserting only that the file is absent is not enough: with the
        containment check removed the write still fails, because
        `relative_to` raises further down. That is luck, not a guard, so
        this also pins the designed refusal.
        """
        with install_factory([
            tool_call("file_write", {"path": "../state/stolen.txt", "content": "x"}),
            message("Refused."),
        ]) as install:
            install.loop().handle_message("write outside the workspace")

            assert not (install.state / "stolen.txt").exists()

            results = [m for m in install.history() if m.role == "tool"]
            assert results, "the tool call never produced a result"
            assert "path rejected" in (results[-1].content or ""), (
                f"blocked, but not by the path check: {results[-1].content!r}"
            )

    def test_a_skill_runs_as_a_real_subprocess(self, install_factory):
        """Exercises skill discovery, the subprocess env, and stdout capture.

        The skills are installed by init, so this also proves the template
        sync put runnable scripts on disk.
        """
        with install_factory([
            tool_call("skill_invoke", {"skill_name": "carry-over", "action": "get"}),
            message("Nothing queued."),
        ]) as install:
            assert (install.workspace / "skills" / "carry-over").is_dir()

            reply = install.loop().handle_message("anything waiting for me?")
            assert reply == "Nothing queued."

            history = install.history()
            tool_results = [m for m in history if m.role == "tool"]
            assert tool_results, "the skill result never reached the history"


class TestRestart:
    def test_a_new_process_resumes_the_conversation(self, install_factory):
        with install_factory([
            message("Your visa is on the 16th."),
            message("I said the 16th."),
        ]) as install:
            install.loop().handle_message("when is my visa appointment?")

            # A second loop over the same install is what a restart is.
            reply = install.loop().handle_message("what date did you say?")

            assert reply == "I said the 16th."
            sent = install.script.requests[-1]["messages"]
            assert any(
                "16th" in (m.get("content") or "") for m in sent
            ), "the earlier answer was not resent after restart"


class TestAccessControl:
    """Driven through the real run loop, not by asking the fake."""

    def test_an_unknown_sender_reaches_neither_the_model_nor_the_store(
        self, install_factory,
    ):
        with install_factory([]) as install:
            channel = FakeChannel(
                allowed_users=["me"],
                inbound_queue=[inbound("delete everything", sender_id="stranger")],
            )
            install.loop(channel=channel).run()

            assert install.script.call_count == 0, "a stranger reached the model"
            assert "delete everything" not in install.history_text()
            assert channel.sent == []

    def test_an_allowed_sender_gets_through(self, install_factory):
        """The other half: proves the test above is not passing by accident."""
        with install_factory([message("Nothing deleted.")]) as install:
            channel = FakeChannel(
                allowed_users=["me"],
                inbound_queue=[inbound("what can you do", sender_id="me")],
            )
            install.loop(channel=channel).run()

            assert install.script.call_count == 1
            assert "what can you do" in install.history_text()
            assert channel.sent_text == ["Nothing deleted."]
