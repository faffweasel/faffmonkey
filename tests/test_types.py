import json
from pathlib import Path


from faffmonkey.types import (
    Message,
    TokenUsage,
    ToolCall,
    dict_to_message,
    message_to_dict,
    usage_from_dict,
)


class TestMessageToDict:
    def test_basic_roundtrip(self):
        msg = Message(role="user", content="hello")
        d = message_to_dict(msg)
        assert d == {"role": "user", "content": "hello"}

    def test_tool_calls_serialised(self):
        msg = Message(
            role="assistant",
            tool_calls=[ToolCall(id="1", name="test", arguments={"a": 1})],
        )
        d = message_to_dict(msg)
        assert json.loads(d["tool_calls"][0]["function"]["arguments"]) == {"a": 1}

    def test_non_serialisable_arguments_falls_back(self):
        msg = Message(
            role="assistant",
            tool_calls=[ToolCall(
                id="1", name="test",
                arguments={"path": Path("/tmp/secret")},
            )],
        )
        d = message_to_dict(msg)
        assert d["tool_calls"][0]["function"]["arguments"] == "{}"


class TestDictToMessage:
    def test_basic(self):
        msg = dict_to_message({"role": "user", "content": "hi"})
        assert msg.role == "user"
        assert msg.content == "hi"

    def test_empty_role_returns_none(self):
        assert dict_to_message({"role": "", "content": "hi"}) is None

    def test_missing_role_returns_none(self):
        assert dict_to_message({"content": "hi"}) is None

    def test_deeply_nested_arguments_no_crash(self):
        nested = {"a": None}
        current = nested
        for _ in range(200):
            current["a"] = {"a": None}
            current = current["a"]
        args_str = json.dumps(nested)
        d = {
            "role": "assistant",
            "tool_calls": [{
                "id": "1",
                "function": {"name": "test", "arguments": args_str},
            }],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None


class TestUsageFromDict:
    def test_basic(self):
        usage = usage_from_dict({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 20
        assert usage.total_tokens == 30

    def test_string_values_coerced(self):
        usage = usage_from_dict({"prompt_tokens": "10", "completion_tokens": "20", "total_tokens": "30"})
        assert usage.prompt_tokens == 10

    def test_non_numeric_string_returns_default(self):
        usage = usage_from_dict({"prompt_tokens": "not_a_number"})
        assert usage == TokenUsage()

    def test_none_value_returns_default(self):
        usage = usage_from_dict({"prompt_tokens": None})
        assert usage == TokenUsage()


class TestDefensiveDictToMessage:
    def test_none_tool_calls(self):
        msg = dict_to_message({"role": "assistant", "tool_calls": None})
        assert msg.tool_calls is None

    def test_string_tool_calls_ignored(self):
        msg = dict_to_message({"role": "assistant", "tool_calls": "not a list"})
        assert msg.tool_calls is None

    def test_non_dict_elements_skipped(self):
        d = {
            "role": "assistant",
            "tool_calls": [
                "garbage",
                42,
                {"id": "1", "function": {"name": "real", "arguments": "{}"}},
            ],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "real"

    def test_non_dict_function_uses_fallback(self):
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "1", "function": "not a dict", "name": "f", "arguments": {"x": 1}}],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].name == "f"
        assert msg.tool_calls[0].arguments == {"x": 1}

    def test_null_content(self):
        msg = dict_to_message({"role": "assistant", "content": None})
        assert msg.content == ""


class TestDefensiveUsageFromDict:
    def test_none_input(self):
        assert usage_from_dict(None) == TokenUsage()

    def test_list_input(self):
        assert usage_from_dict([1, 2, 3]) == TokenUsage()

    def test_string_input(self):
        assert usage_from_dict("bad") == TokenUsage()


class TestOversizedToolCallSkipped:
    def test_tool_call_over_256kb_skipped(self):
        big_args = json.dumps({"data": "x" * 300_000})
        d = {
            "role": "assistant",
            "tool_calls": [
                {"id": "1", "function": {"name": "big_tool", "arguments": big_args}},
                {"id": "2", "function": {"name": "small_tool", "arguments": "{}"}},
            ],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "small_tool"

    def test_tool_call_under_256kb_kept(self):
        args = json.dumps({"data": "x" * 100})
        d = {
            "role": "assistant",
            "tool_calls": [
                {"id": "1", "function": {"name": "ok_tool", "arguments": args}},
            ],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "ok_tool"


class TestFallbackBranchArgumentsCoerced:
    def test_list_arguments_coerced_to_empty_dict(self):
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "1", "name": "f", "arguments": [1, 2]}],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_string_arguments_coerced_to_empty_dict(self):
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "1", "name": "f", "arguments": "bad"}],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_int_arguments_coerced_to_empty_dict(self):
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "1", "name": "f", "arguments": 42}],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_dict_arguments_preserved(self):
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "1", "name": "f", "arguments": {"key": "val"}}],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {"key": "val"}

    def test_missing_arguments_defaults_to_empty_dict(self):
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "1", "name": "f"}],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_none_arguments_coerced(self):
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "1", "name": "f", "arguments": None}],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_logs_warning_on_coercion(self, caplog):
        import logging
        d = {
            "role": "assistant",
            "tool_calls": [{"id": "tc1", "name": "f", "arguments": [1]}],
        }
        with caplog.at_level(logging.WARNING):
            dict_to_message(d)
        assert any("arguments is list" in r.message for r in caplog.records)


class TestUnrecognisedRoleDropped:
    def test_unknown_role_returns_none(self):
        assert dict_to_message({"role": "function", "content": "hi"}) is None

    def test_arbitrary_role_returns_none(self):
        assert dict_to_message({"role": "admin", "content": "hi"}) is None

    def test_valid_roles_accepted(self):
        for role in ("system", "user", "assistant", "tool"):
            msg = dict_to_message({"role": role, "content": "hi"})
            assert msg is not None
            assert msg.role == role

    def test_logs_warning_on_unrecognised_role(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            dict_to_message({"role": "function", "content": "hi"})
        assert any("unrecognised role" in r.message for r in caplog.records)


class TestNonDictToolCallArguments:
    def test_json_array_arguments_coerced(self):
        d = {
            "role": "assistant",
            "tool_calls": [{
                "id": "1",
                "function": {"name": "test", "arguments": '[1, 2, 3]'},
            }],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_json_scalar_string_arguments_coerced(self):
        d = {
            "role": "assistant",
            "tool_calls": [{
                "id": "1",
                "function": {"name": "test", "arguments": '"just a string"'},
            }],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_json_integer_arguments_coerced(self):
        d = {
            "role": "assistant",
            "tool_calls": [{
                "id": "1",
                "function": {"name": "test", "arguments": '42'},
            }],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_pre_parsed_list_arguments_coerced(self):
        d = {
            "role": "assistant",
            "tool_calls": [{
                "id": "1",
                "function": {"name": "test", "arguments": [1, 2]},
            }],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_dict_arguments_preserved(self):
        d = {
            "role": "assistant",
            "tool_calls": [{
                "id": "1",
                "function": {"name": "test", "arguments": '{"key": "val"}'},
            }],
        }
        msg = dict_to_message(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {"key": "val"}


class TestMalformedRoleLogsWarning:
    def test_missing_role_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            result = dict_to_message({"content": "orphan"})
        assert result is None
        assert any("missing 'role'" in r.message for r in caplog.records)

    def test_empty_role_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            result = dict_to_message({"role": "", "content": "orphan"})
        assert result is None
        assert any("missing 'role'" in r.message for r in caplog.records)


class TestMessageToDictLogsWarning:
    def test_unserializable_arguments_logs_warning(self, caplog):
        import logging
        msg = Message(
            role="assistant",
            tool_calls=[ToolCall(
                id="1", name="broken",
                arguments={"path": Path("/tmp/secret")},
            )],
        )
        with caplog.at_level(logging.WARNING):
            d = message_to_dict(msg)
        assert d["tool_calls"][0]["function"]["arguments"] == "{}"
        assert any("failed to serialize tool_call arguments for broken" in r.message for r in caplog.records)


class TestImageMessages:
    """D6a/D6e: images travel as paths and expand to content parts."""

    def _png(self, tmp_path, name="shot.png"):
        path = tmp_path / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
        return path

    def test_message_with_no_images_serialises_as_plain_text(self):
        d = message_to_dict(Message(role="user", content="hello"))
        assert d["content"] == "hello"

    def test_image_expands_to_content_parts(self, tmp_path):
        path = self._png(tmp_path)
        d = message_to_dict(Message(role="user", content="what is this?", images=[str(path)]))

        assert d["content"][0] == {"type": "text", "text": "what is this?"}
        assert d["content"][1]["type"] == "image_url"
        assert d["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_caption_free_image_still_serialises(self, tmp_path):
        d = message_to_dict(Message(role="user", images=[str(self._png(tmp_path))]))
        assert len(d["content"]) == 1
        assert d["content"][0]["type"] == "image_url"

    def test_missing_file_becomes_a_note_not_a_crash(self, tmp_path):
        d = message_to_dict(Message(role="user", content="hi", images=[str(tmp_path / "gone.png")]))
        assert d["content"][1]["type"] == "text"
        assert "image unavailable" in d["content"][1]["text"]

    def test_oversized_image_is_skipped(self, tmp_path):
        from faffmonkey.types import MAX_IMAGE_BYTES

        path = tmp_path / "huge.jpg"
        path.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
        d = message_to_dict(Message(role="user", images=[str(path)]))
        assert "image unavailable" in d["content"][0]["text"]

    def test_unsupported_extension_is_skipped(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("hello")
        d = message_to_dict(Message(role="user", images=[str(path)]))
        assert "image unavailable" in d["content"][0]["text"]

    def test_images_round_trip_through_dict_to_message(self):
        msg = dict_to_message({"role": "user", "content": "x", "images": ["a.png", "b.png"]})
        assert msg.images == ["a.png", "b.png"]

    def test_non_list_images_are_ignored(self):
        assert dict_to_message({"role": "user", "content": "x", "images": "a.png"}).images == []
