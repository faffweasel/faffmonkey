"""Offline tests for contrib skill scripts (no network)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_CONTRIB_SKILLS = Path(__file__).resolve().parent.parent / "contrib" / "skills"


def _run(skill: str, script: str, args: list[str], env: dict[str, str]):
    cmd = [sys.executable, str(_CONTRIB_SKILLS / skill / "scripts" / script)]
    cmd.extend(args)
    full_env = {"PATH": "/usr/bin:/bin", **env}
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, env=full_env,
    )


class TestOpenrouterImageOutput:
    """The skill required --output, so the agent invented a path and wrote a
    bare shared/images/<name>.png with no date, while venice-ai-media filed
    its output under shared/media/ by timestamp."""

    def test_default_output_is_dated_under_shared_media(self, tmp_path):
        sys.path.insert(0, str(_CONTRIB_SKILLS / "openrouter-image-simple" / "scripts"))
        try:
            import importlib
            import os
            import re
            import generate
            importlib.reload(generate)
            os.environ["WORKSPACE"] = str(tmp_path)
            try:
                out = Path(generate.default_output())
            finally:
                del os.environ["WORKSPACE"]
        finally:
            sys.path.pop(0)
        assert out.parent == tmp_path / "shared" / "media" / "openrouter"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}\.png", out.name), out.name

    def test_output_is_no_longer_required(self, tmp_path):
        result = _run(
            "openrouter-image-simple", "generate.py", ["a cat"],
            {"OPENROUTER_API_KEY": "", "WORKSPACE": str(tmp_path)},
        )
        assert "output is required" not in result.stderr


class TestAqi:
    def test_usage_without_args(self):
        result = _run("aqi", "aqi.py", [], {})
        assert result.returncode == 1
        assert "Usage" in result.stdout

    def test_missing_api_key_reported(self):
        result = _run("aqi", "aqi.py", ["london"], {"AQICN_API_KEY": ""})
        assert "AQICN_API_KEY not set" in result.stdout

    def test_multi_without_location_reports_config(self, tmp_path):
        result = _run(
            "aqi", "aqi.py", ["multi"],
            {"AQICN_API_KEY": "x", "WORKSPACE": str(tmp_path)},
        )
        assert "No location configured" in result.stdout

    def test_location_loaded_from_workspace_env(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        (config / "location.json").write_text(json.dumps(
            {"current": {"city": "Testville", "lat": 10.0, "lng": 20.0}}
        ))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "aqi" / "scripts"))
        try:
            import importlib
            import aqi
            importlib.reload(aqi)
            import os
            os.environ["WORKSPACE"] = str(tmp_path)
            try:
                assert aqi._load_location() == (10.0, 20.0, "Testville")
            finally:
                del os.environ["WORKSPACE"]
        finally:
            sys.path.pop(0)

    def test_pin_round_trip_and_current_uses_it(self, tmp_path):
        """Two stations 6 km apart read 4 and 101; `multi` averaged them.
        The morning briefing needs one answer, and the user's choice of
        station has to survive the session, so it is skill config."""
        data_dir = tmp_path / "skills-data" / "aqi"
        env = {"AQICN_API_KEY": "x", "WORKSPACE": str(tmp_path), "SKILL_DATA": str(data_dir)}

        assert "No station pinned" in _run("aqi", "aqi.py", ["pin"], env).stdout

        sys.path.insert(0, str(_CONTRIB_SKILLS / "aqi" / "scripts"))
        try:
            import importlib
            import os
            from unittest.mock import patch
            import aqi
            importlib.reload(aqi)
            os.environ["SKILL_DATA"] = str(data_dir)
            try:
                reading = {"city": "Riverside Park", "aqi": 101, "dominant": "pm25",
                           "time": "2026-08-23 14:00", "pollutants": {}}
                with patch.object(aqi, "get_aqi_by_id", return_value=dict(reading)) as by_id:
                    aqi.save_pin({"uid": 4321, "name": "Riverside Park"})
                    result = aqi.get_current()
                    by_id.assert_called_once_with(4321)
                assert result["pinned"] is True and result["aqi"] == 101
                assert json.loads((data_dir / "config.json").read_text())["pinned_station"]["uid"] == 4321

                aqi.save_pin(None)
                assert aqi.load_pin() is None
                with patch.object(aqi, "get_local_multi", return_value={"error": "No location configured"}):
                    assert "pin a station" in aqi.get_current()["error"]
            finally:
                del os.environ["SKILL_DATA"]
        finally:
            sys.path.pop(0)

    def test_pin_refuses_a_station_it_cannot_read(self, tmp_path):
        data_dir = tmp_path / "skills-data" / "aqi"
        env = {"AQICN_API_KEY": "", "WORKSPACE": str(tmp_path), "SKILL_DATA": str(data_dir)}
        result = _run("aqi", "aqi.py", ["pin", "4321"], env)
        assert result.returncode == 1
        assert "not pinned" in result.stdout
        assert not (data_dir / "config.json").exists()
        result = _run("aqi", "aqi.py", ["pin", "riverside"], env)
        assert result.returncode == 1 and "must be a number" in result.stdout

    def test_aqi_description_levels(self):
        sys.path.insert(0, str(_CONTRIB_SKILLS / "aqi" / "scripts"))
        try:
            import importlib
            import aqi
            importlib.reload(aqi)
            assert aqi.aqi_description(30) == "Good"
            assert aqi.aqi_description(75) == "Moderate"
            assert aqi.aqi_description(120) == "Unhealthy for sensitive groups"
            assert aqi.aqi_description(180) == "Unhealthy"
            assert aqi.aqi_description(250) == "Very unhealthy"
            assert aqi.aqi_description(400) == "Hazardous"
            assert aqi.aqi_description(None) == "unknown"
        finally:
            sys.path.pop(0)


class TestTimezone:
    def _import_tz(self):
        import importlib
        sys.path.insert(0, str(_CONTRIB_SKILLS / "timezone" / "scripts"))
        import tz
        importlib.reload(tz)
        sys.path.pop(0)
        return tz

    def test_parse_time_formats(self):
        tz = self._import_tz()
        assert tz.parse_time("15:00") == (15, 0)
        assert tz.parse_time("3pm") == (15, 0)
        assert tz.parse_time("3:30pm") == (15, 30)
        assert tz.parse_time("9:30am") == (9, 30)
        assert tz.parse_time("12am") == (0, 0)
        assert tz.parse_time("12pm") == (12, 0)
        assert tz.parse_time("15") == (15, 0)
        assert tz.parse_time("3p") == (15, 0)
        assert tz.parse_time("nonsense") is None

    def test_alias_resolution_dst_aware(self):
        tz = self._import_tz()
        aliases = tz.DEFAULT_ALIASES
        assert str(tz.resolve_tz("gmt", aliases)) == "Europe/London"
        assert str(tz.resolve_tz("hanoi", aliases)) == "Asia/Bangkok"
        assert str(tz.resolve_tz("Europe/Paris", aliases)) == "Europe/Paris"
        assert tz.resolve_tz("not-a-zone-xyz", aliases) is None

    def test_custom_aliases_from_workspace(self, tmp_path, monkeypatch):
        config = tmp_path / "config"
        config.mkdir()
        (config / "aliases.json").write_text(json.dumps(
            {"_comment": "test", "office": "Asia/Singapore"}
        ))
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        tz = self._import_tz()
        aliases = tz.load_aliases()
        assert aliases["office"] == "Asia/Singapore"
        assert "_comment" not in aliases
        assert aliases["london"] == "Europe/London"

    def test_local_tz_prefers_location_json(self, tmp_path, monkeypatch):
        config = tmp_path / "config"
        config.mkdir()
        (config / "location.json").write_text(json.dumps(
            {"current": {"city": "Porto", "timezone": "Europe/Lisbon"}}
        ))
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        monkeypatch.setenv("TZ", "Europe/London")
        tz = self._import_tz()
        assert tz.local_tz_name() == "Europe/Lisbon"

    def test_local_tz_falls_back_to_tz_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        monkeypatch.setenv("TZ", "Europe/London")
        tz = self._import_tz()
        assert tz.local_tz_name() == "Europe/London"

    def test_diff_requires_two_zones(self):
        result = _run("timezone", "tz.py", ["diff", "london"], {})
        assert result.returncode == 1
        assert "Usage" in result.stdout


_RSS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Feed</title>
<item><title>First post</title><link>https://ex.com/1</link>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>Mon, 03 Aug 2026 10:00:00 +0000</pubDate>
<guid>guid-1</guid></item>
<item><title>Second post</title><link>https://ex.com/2</link>
<guid>guid-2</guid></item>
</channel></rss>"""

_ATOM_SAMPLE = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>Atom entry</title><link href="https://ex.com/a"/>
<id>atom-1</id><updated>2026-08-03T10:00:00Z</updated>
<summary>Entry summary</summary></entry>
</feed>"""


class TestDigestEngine:
    def _import(self, monkeypatch, tmp_path):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "digest-engine" / "scripts"))
        import feed_fetch
        importlib.reload(feed_fetch)
        sys.path.pop(0)
        return feed_fetch

    def test_parses_rss(self, monkeypatch, tmp_path):
        ff = self._import(monkeypatch, tmp_path)
        items = ff.parse_feed(_RSS_SAMPLE, source_url="https://ex.com/feed")
        assert len(items) == 2
        assert items[0]["title"] == "First post"
        assert items[0]["guid"] == "guid-1"
        assert items[0]["description"] == "Hello world"
        assert items[0]["date"] is not None
        assert items[0]["feed"] == "Test Feed"

    def test_parses_atom(self, monkeypatch, tmp_path):
        ff = self._import(monkeypatch, tmp_path)
        items = ff.parse_feed(_ATOM_SAMPLE, source_url="https://ex.com/atom")
        assert len(items) == 1
        assert items[0]["title"] == "Atom entry"
        assert items[0]["link"] == "https://ex.com/a"
        assert items[0]["guid"] == "atom-1"

    def test_dedup_round_trip(self, monkeypatch, tmp_path):
        ff = self._import(monkeypatch, tmp_path)
        items = ff.parse_feed(_RSS_SAMPLE)
        seen = ff.mark_seen({}, items)
        new_items, seen_count = ff.filter_unseen(items, seen)
        assert new_items == []
        assert seen_count == 2

        extra = dict(items[0])
        extra["guid"] = "guid-3"
        new_items, seen_count = ff.filter_unseen(items + [extra], seen)
        assert len(new_items) == 1
        assert seen_count == 2

    def test_seen_persistence_under_skill_data(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKILL_DATA", str(tmp_path / "sd"))
        ff = self._import(monkeypatch, tmp_path)
        ff.save_seen("My Digest", {"h1": {
            "title": "x", "first_seen": "2026-08-01", "last_seen": "2026-08-05",
        }})
        assert (tmp_path / "sd" / "seen" / "my_digest.json").is_file()
        assert "h1" in ff.load_seen("My Digest")

    def test_config_read_from_workspace(self, monkeypatch, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        (config / "digests.json").write_text(json.dumps(
            {"digests": [{"name": "T", "sources": {"rss": []}}]}
        ))
        ff = self._import(monkeypatch, tmp_path)
        assert [d["name"] for d in ff.load_digests()] == ["T"]


class TestSkillDataConfigLocations:
    """2026-08-24: single-consumer skill configs lived in config/, which the
    agent treated as off-limits (the jobs.json rule bled over) and which
    mixed skill state into cross-skill configuration. They now live in
    skills-data/<name>/ with the legacy location still honoured."""

    def test_github_deps_reads_skills_data_first(self, tmp_path):
        sd = tmp_path / "skills-data" / "github-deps"
        sd.mkdir(parents=True)
        (sd / "repos.json").write_text(json.dumps({
            "repos": [{"owner": "x", "repo": "y", "label": "XY"}],
            "rss_pattern": "https://github.com/{owner}/{repo}/releases.atom",
        }))
        result = _run(
            "github-deps", "github_releases.py", ["--list"],
            {"WORKSPACE": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "XY" in result.stdout

    def test_digest_engine_reads_skills_data_first(self, tmp_path, monkeypatch):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        monkeypatch.setenv("SKILL_DATA", str(tmp_path / "skills-data" / "digest-engine"))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "digest-engine" / "scripts"))
        import feed_fetch
        importlib.reload(feed_fetch)
        sys.path.pop(0)
        sd = tmp_path / "skills-data" / "digest-engine"
        sd.mkdir(parents=True)
        (sd / "digests.json").write_text(json.dumps(
            {"digests": [{"name": "P", "sources": {"rss": []}}]}
        ))
        assert [d["name"] for d in feed_fetch.load_digests()] == ["P"]

    def test_timezone_aliases_read_from_skills_data(self, tmp_path, monkeypatch):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "timezone" / "scripts"))
        import tz
        importlib.reload(tz)
        sys.path.pop(0)
        sd = tmp_path / "skills-data" / "timezone"
        sd.mkdir(parents=True)
        (sd / "aliases.json").write_text(json.dumps({"gotham": "America/New_York"}))
        aliases = tz.load_aliases()
        assert aliases["gotham"] == "America/New_York"


class TestGithubDeps:
    def _import(self, monkeypatch, tmp_path):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "github-deps" / "scripts"))
        import github_releases
        importlib.reload(github_releases)
        sys.path.pop(0)
        return github_releases

    def test_semver_classification(self, monkeypatch, tmp_path):
        gh = self._import(monkeypatch, tmp_path)
        assert gh.classify_version("v2.0.0") == ("2.0.0", "major")
        assert gh.classify_version("v1.3.0") == ("1.3.0", "minor")
        assert gh.classify_version("v1.3.7") == ("1.3.7", "patch")

    def test_list_reads_workspace_config(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        (config / "repos.json").write_text(json.dumps({
            "repos": [{"owner": "a", "repo": "b", "label": "AB"}],
            "rss_pattern": "https://github.com/{owner}/{repo}/releases.atom",
        }))
        result = _run(
            "github-deps", "github_releases.py", ["--list"],
            {"WORKSPACE": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "AB" in result.stdout
        assert "a/b" in result.stdout

    def test_missing_config_handled(self, tmp_path):
        result = _run(
            "github-deps", "github_releases.py", ["--list"],
            {"WORKSPACE": str(tmp_path)},
        )
        assert "No repos configured" in result.stdout


class TestOpenRouterImage:
    def _import_generate(self, monkeypatch, tmp_path):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        monkeypatch.setenv("SKILL_DATA", str(tmp_path / "sd"))
        sys.path.insert(
            0, str(_CONTRIB_SKILLS / "openrouter-image-simple" / "scripts"),
        )
        import generate
        importlib.reload(generate)
        sys.path.pop(0)
        return generate

    def test_missing_key_exits(self):
        result = _run(
            "openrouter-image-simple", "generate.py",
            ["--prompt", "x", "--output", "/tmp/x.png"],
            {"OPENROUTER_API_KEY": ""},
        )
        assert result.returncode == 1
        assert "OPENROUTER_API_KEY" in result.stderr

    def test_generate_writes_image_and_media_line(
        self, monkeypatch, tmp_path, capsys,
    ):
        import base64
        from unittest.mock import MagicMock, patch as mock_patch

        gen = self._import_generate(monkeypatch, tmp_path)
        monkeypatch.setenv("OPENROUTER_API_KEY", "key")

        payload = {
            "choices": [{"message": {"images": [{
                "image_url": {"url": "data:image/png;base64,"
                              + base64.b64encode(b"PNGDATA").decode()},
            }]}}],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        out_path = tmp_path / "out" / "img.png"
        with mock_patch.object(
            gen.urllib.request, "urlopen", return_value=mock_resp,
        ):
            gen.generate_image("a cat", str(out_path))

        assert out_path.read_bytes() == b"PNGDATA"
        assert f"MEDIA: {out_path}" in capsys.readouterr().out

    def test_seed_supplies_generation_default(self, monkeypatch, tmp_path):
        gen = self._import_generate(monkeypatch, tmp_path)
        assert gen.DEFAULT_MODEL == "google/gemini-3.1-flash-image"

    def test_config_overrides_default_model(self, monkeypatch, tmp_path):
        # Real config in the skill's own data dir; a decoy where the
        # inherited SKILL_DATA env points. 2026-08-24: config resolved via
        # SKILL_DATA, so a script running as another skill's subprocess
        # read the CALLER's data dir, missed the operator's config, and
        # silently used the hardcoded fallback model.
        own = tmp_path / "skills-data" / "openrouter-image-simple"
        own.mkdir(parents=True)
        (own / "config.json").write_text(json.dumps({
            "generation": {"model": "custom/model-x", "aliases": {"c": "custom/model-x"}},
        }))
        decoy = tmp_path / "sd"
        decoy.mkdir()
        (decoy / "config.json").write_text(json.dumps({
            "generation": {"model": "wrong/decoy-model"},
        }))
        gen = self._import_generate(monkeypatch, tmp_path)  # sets SKILL_DATA=sd
        assert gen.DEFAULT_MODEL == "custom/model-x"
        assert gen.resolve_model("c") == "custom/model-x"
        assert gen.resolve_model("unaliased/id") == "unaliased/id"


class TestVeniceMedia:
    def test_all_scripts_have_help(self):
        for script in ("venice-image", "venice-edit", "venice-upscale", "venice-video"):
            result = _run("venice-ai-media", f"{script}.py", ["--help"], {})
            assert result.returncode == 0, f"{script} --help failed: {result.stderr}"

    def test_missing_key_exits(self):
        result = _run(
            "venice-ai-media", "venice-image.py", ["--prompt", "x"],
            {"VENICE_API_KEY": ""},
        )
        assert result.returncode == 2
        assert "VENICE_API_KEY" in result.stderr

    def _load_venice_image(self):
        import importlib.util
        path = _CONTRIB_SKILLS / "venice-ai-media" / "scripts" / "venice-image.py"
        sys.path.insert(0, str(path.parent))
        try:
            spec = importlib.util.spec_from_file_location("venice_image_under_test", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.pop(0)
        return mod

    def test_sizing_params_sent_only_when_requested(self, monkeypatch):
        """2026-08-24: unconditional width/height defaults broke every
        aspect-ratio model, including the skill's own default model."""
        mod = self._load_venice_image()
        captured = {}
        monkeypatch.setattr(
            mod, "api_json",
            lambda endpoint, method, payload, api_key, timeout=120: captured.update(payload) or {},
        )
        mod.generate_image(
            api_key="k", prompt="p", model="qwen-image-3",
            width=None, height=None, fmt="webp", cfg_scale=None, seed=None,
            negative_prompt=None, style_preset=None, resolution=None,
            aspect_ratio=None, safe_mode=False, hide_watermark=False,
        )
        assert "width" not in captured and "height" not in captured
        assert "aspect_ratio" not in captured

        captured.clear()
        mod.generate_image(
            api_key="k", prompt="p", model="qwen-image-3",
            width=None, height=None, fmt="webp", cfg_scale=None, seed=None,
            negative_prompt=None, style_preset=None, resolution=None,
            aspect_ratio="16:9", safe_mode=False, hide_watermark=False,
        )
        assert captured["aspect_ratio"] == "16:9"
        assert "width" not in captured

        captured.clear()
        mod.generate_image(
            api_key="k", prompt="p", model="venice-sd35",
            width=1024, height=768, fmt="webp", cfg_scale=None, seed=None,
            negative_prompt=None, style_preset=None, resolution=None,
            aspect_ratio=None, safe_mode=False, hide_watermark=False,
        )
        assert captured["width"] == 1024 and captured["height"] == 768

    def test_edit_defaults_to_shared_media(self):
        """2026-08-24: venice-edit defaulted to the input's own directory,
        dropping edited selfies next to the reference portrait in
        identity/ while HUMAN.md documented shared/media all along."""
        result = _run("venice-ai-media", "venice-edit.py", ["--help"], {})
        assert result.returncode == 0
        assert "shared/media" in result.stdout

    def test_failed_batch_run_leaves_no_empty_folder(self, monkeypatch, tmp_path):
        """2026-08-24: every failed generation left an empty timestamped
        folder under shared/media."""
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "venice-ai-media" / "scripts"))
        try:
            import venice_common
            importlib.reload(venice_common)
        finally:
            sys.path.pop(0)
        mod = self._load_venice_image()

        def boom(**kwargs):
            raise RuntimeError("model rejected the request")
        monkeypatch.setattr(mod, "generate_image", boom)
        monkeypatch.setattr(mod, "require_api_key", lambda: "k")
        monkeypatch.setattr(
            sys, "argv",
            ["venice-image.py", "--prompt", "x", "--count", "1", "--no-validate"],
        )
        rc = mod.main()
        assert rc == 1
        assert not (tmp_path / "shared" / "media" / "venice-image").exists()

    def test_seed_config_supplies_defaults_without_user_config(self, monkeypatch, tmp_path):
        """Defaults ship as the skill directory's config.json, not as model
        ids in code; a fresh install resolves models from the seed."""
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        monkeypatch.delenv("SKILL_DATA", raising=False)
        sys.path.insert(0, str(_CONTRIB_SKILLS / "venice-ai-media" / "scripts"))
        try:
            import venice_common
            importlib.reload(venice_common)
        finally:
            sys.path.pop(0)
        cfg = venice_common.load_config()
        assert cfg["edit"]["model"] == "firered-image-edit-1.1"
        assert cfg["image"]["model"] == "qwen-image-3"
        assert "gpt" not in json.dumps(cfg)

    def test_config_ignores_foreign_skill_data_env(self, monkeypatch, tmp_path):
        """2026-08-24: venice-edit as selfie's IMAGE_EDIT_CMD subprocess
        inherited selfie's SKILL_DATA, looked for config in
        skills-data/selfie/, and billed the hardcoded fallback model while
        the operator's real config sat unread."""
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        caller = tmp_path / "skills-data" / "selfie"
        caller.mkdir(parents=True)
        (caller / "config.json").write_text(json.dumps({
            "edit": {"model": "wrong/decoy"},
        }))
        monkeypatch.setenv("SKILL_DATA", str(caller))
        own = tmp_path / "skills-data" / "venice-ai-media"
        own.mkdir(parents=True)
        (own / "config.json").write_text(json.dumps({
            "edit": {"model": "firered-image-edit-1.1"},
        }))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "venice-ai-media" / "scripts"))
        try:
            import venice_common
            importlib.reload(venice_common)
        finally:
            sys.path.pop(0)
        cfg = venice_common.load_config()
        assert cfg["edit"]["model"] == "firered-image-edit-1.1"

    def test_prompt_log_appends_beside_the_image(self, monkeypatch, tmp_path):
        """2026-08-24: per-run folders with an index.html each made images
        unbrowsable; the flat folder keeps prompts in one appendable file."""
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "venice-ai-media" / "scripts"))
        try:
            import venice_common
            importlib.reload(venice_common)
        finally:
            sys.path.pop(0)
        img = tmp_path / "media" / "a.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"x")
        venice_common.append_prompt_log(img, "a firefly", "qwen-image-3")
        venice_common.append_prompt_log(img, "a beetle", "qwen-image-3")
        lines = (img.parent / "prompts.jsonl").read_text().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["file"] == "a.png" and first["prompt"] == "a firefly"

    def test_openrouter_prompt_log_matches(self, tmp_path):
        sys.path.insert(0, str(_CONTRIB_SKILLS / "openrouter-image-simple" / "scripts"))
        try:
            import openrouter_common
        finally:
            sys.path.pop(0)
        img = tmp_path / "b.png"
        img.write_bytes(b"x")
        openrouter_common.append_prompt_log(str(img), "sunset", "google/gemini-3.1-flash-image")
        entry = json.loads((tmp_path / "prompts.jsonl").read_text().splitlines()[0])
        assert entry["file"] == "b.png" and entry["model"] == "google/gemini-3.1-flash-image"

    def test_default_out_dir_under_workspace_shared(self, monkeypatch, tmp_path):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        sys.path.insert(0, str(_CONTRIB_SKILLS / "venice-ai-media" / "scripts"))
        import venice_common
        importlib.reload(venice_common)
        sys.path.pop(0)
        out = venice_common.default_out_dir("test")
        # Flat and stable: one folder per command, not one per run.
        assert out == tmp_path / "shared" / "media" / "test"


class TestWeather:
    def _import(self, monkeypatch, tmp_path):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        monkeypatch.setenv("SKILL_DATA", str(tmp_path / "sd"))
        monkeypatch.setenv("OPENWEATHERMAP_API_KEY", "test-key")
        sys.path.insert(0, str(_CONTRIB_SKILLS / "weather" / "scripts"))
        import weather
        importlib.reload(weather)
        sys.path.pop(0)
        return weather

    def test_missing_key_exits(self, tmp_path):
        result = _run(
            "weather", "weather.py", ["now", "paris"],
            {"OPENWEATHERMAP_API_KEY": "", "WORKSPACE": str(tmp_path)},
        )
        assert result.returncode == 2
        assert "OPENWEATHERMAP_API_KEY" in result.stderr

    def test_cache_round_trip_and_expiry(self, monkeypatch, tmp_path):
        w = self._import(monkeypatch, tmp_path)
        w.cache_put("k1", {"a": 1})
        assert w.cache_get("k1") == {"a": 1}
        assert w.cache_get("missing") is None

        stale = json.loads(w.CACHE_FILE.read_text())
        stale["k1"]["fetched_at"] -= w.CACHE_TTL_SECONDS + 1
        w.CACHE_FILE.write_text(json.dumps(stale))
        assert w.cache_get("k1") is None

    def test_fetch_uses_cache(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock, patch as mock_patch
        w = self._import(monkeypatch, tmp_path)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"temp": 20}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with mock_patch.object(
            w.urllib.request, "urlopen", return_value=mock_resp,
        ) as mock_open:
            first = w._fetch("/data/2.5/weather", {"lat": 1, "lon": 2})
            second = w._fetch("/data/2.5/weather", {"lat": 1, "lon": 2})
        assert first == second == {"temp": 20}
        assert mock_open.call_count == 1

    def test_geocode_parses_top_result(self, monkeypatch, tmp_path):
        w = self._import(monkeypatch, tmp_path)
        monkeypatch.setattr(
            w, "_fetch",
            lambda path, params: [{"name": "Paris", "country": "FR",
                                   "lat": 48.85, "lon": 2.35}],
        )
        assert w.geocode("paris") == (48.85, 2.35, "Paris, FR")

    def test_format_current(self, monkeypatch, tmp_path):
        w = self._import(monkeypatch, tmp_path)
        data = {
            "main": {"temp": 12.3, "feels_like": 8.1, "humidity": 70,
                     "pressure": 1013},
            "weather": [{"description": "light rain"}],
            "wind": {"speed": 5.2, "deg": 90},
            "visibility": 8000,
            "sys": {"sunrise": 1700000000, "sunset": 1700040000},
            "timezone": 0,
        }
        out = w.format_current(data, "Testville")
        assert "12.3C (feels like 8.1C)" in out
        assert "light rain" in out
        assert "5.2 m/s E" in out
        assert "8.0 km" in out
        assert "OpenWeatherMap" in out

    def test_forecast_day_grouping(self, monkeypatch, tmp_path):
        w = self._import(monkeypatch, tmp_path)
        base = 20305 * 86400  # midnight UTC, so hours stay within one day
        entries = []
        for day in range(2):
            for hour, temp in ((0, 10), (8, 20), (16, 15)):
                entries.append({
                    "dt": base + day * 86400 + hour * 3600,
                    "main": {"temp": temp + day},
                    "weather": [{"description": "clear sky"}],
                    "pop": 0.4 if day == 1 else 0,
                })
        grouped = w.group_forecast({"city": {"timezone": 0}, "list": entries})
        assert len(grouped) >= 2
        first_day = grouped[0][1]
        assert first_day["high"] == 20
        assert first_day["low"] == 10
        assert "clear sky" in first_day["conditions"]
        assert grouped[1][1]["rain_prob"] == 0.4

    def test_wind_direction(self, monkeypatch, tmp_path):
        w = self._import(monkeypatch, tmp_path)
        assert w.wind_direction(0) == "N"
        assert w.wind_direction(90) == "E"
        assert w.wind_direction(180) == "S"
        assert w.wind_direction(270) == "W"
        assert w.wind_direction(None) == "?"


class TestReminders:
    def _import(self, monkeypatch, tmp_path):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        (tmp_path / "config").mkdir(exist_ok=True)
        sys.path.insert(0, str(_CONTRIB_SKILLS / "reminders" / "scripts"))
        import remind
        importlib.reload(remind)
        sys.path.pop(0)
        return remind

    def _now(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        # Wednesday 5 August 2026, 14:00 Bangkok
        return datetime(2026, 8, 5, 14, 0, tzinfo=ZoneInfo("Asia/Bangkok"))

    def test_parse_relative(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        now = self._now()
        fire, rec = r.parse_when("in 2 hours", now)
        assert (fire - now).total_seconds() == 7200 and rec is None
        fire, rec = r.parse_when("in 30 minutes", now)
        assert (fire - now).total_seconds() == 1800

    def test_parse_day_words(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        now = self._now()
        fire, _ = r.parse_when("tomorrow 9am", now)
        assert (fire.day, fire.hour, fire.minute) == (6, 9, 0)
        fire, _ = r.parse_when("tonight 8pm", now)
        assert (fire.day, fire.hour) == (5, 20)
        fire, _ = r.parse_when("tomorrow", now)
        assert (fire.day, fire.hour) == (6, 9)

    def test_parse_weekday(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        now = self._now()  # Wednesday
        fire, _ = r.parse_when("next friday 3pm", now)
        assert fire.weekday() == 4
        assert (fire.day, fire.hour) == (7, 15)
        # Wednesday at earlier hour -> next week's Wednesday
        fire, _ = r.parse_when("wednesday 9am", now)
        assert (fire.day, fire.hour) == (12, 9)

    def test_parse_absolute_and_past_rejected(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        now = self._now()
        fire, _ = r.parse_when("2026-08-20 14:00", now)
        assert (fire.month, fire.day, fire.hour) == (8, 20, 14)
        with pytest.raises(ValueError):
            r.parse_when("2026-08-01 14:00", now)
        with pytest.raises(ValueError):
            r.parse_when("today 9am", now)  # 9am already passed at 14:00

    def test_parse_bare_time_rolls_to_tomorrow(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        now = self._now()
        fire, _ = r.parse_when("9am", now)
        assert (fire.day, fire.hour) == (6, 9)
        fire, _ = r.parse_when("15:30", now)
        assert (fire.day, fire.hour, fire.minute) == (5, 15, 30)

    def test_parse_recurring(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        now = self._now()
        fire, rec = r.parse_when("every monday 10am", now)
        assert rec == "0 10 * * 1"
        assert fire.weekday() == 0 and fire.hour == 10
        fire, rec = r.parse_when("every day 9am", now)
        assert rec == "0 9 * * *"
        assert (fire.day, fire.hour) == (6, 9)

    def test_next_occurrence(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        now = self._now()
        nxt = r.next_occurrence("0 9 * * *", now)
        assert (nxt.day, nxt.hour) == (6, 9)
        nxt = r.next_occurrence("0 10 * * 1", now)  # Monday
        assert nxt.weekday() == 0 and nxt.day == 10

    def test_add_list_remove_round_trip(self, monkeypatch, tmp_path):
        r = self._import(monkeypatch, tmp_path)
        monkeypatch.setenv("TZ", "UTC")
        assert r.cmd_add("call mum", "in 2 hours") == 0
        assert r.cmd_add("standup", "every monday 10am") == 0

        data = r.load_reminders()
        assert [x["id"] for x in data["reminders"]] == ["rem_001", "rem_002"]
        assert data["reminders"][1]["recurring"] == "0 10 * * 1"

        assert r.cmd_remove("rem_001") == 0
        assert r.cmd_remove("rem_999") == 1
        assert len(r.load_reminders()["reminders"]) == 1

    def test_check_fires_and_advances(self, monkeypatch, tmp_path, capsys):
        from datetime import datetime, timedelta, timezone as dt_tz
        r = self._import(monkeypatch, tmp_path)
        monkeypatch.setenv("TZ", "UTC")

        past = (datetime.now(dt_tz.utc) - timedelta(minutes=5)).isoformat(
            timespec="seconds",
        )
        r.save_reminders({"reminders": [
            {"id": "rem_001", "text": "one shot", "fire_at": past,
             "recurring": None, "created": past, "delivered": False},
            {"id": "rem_002", "text": "daily", "fire_at": past,
             "recurring": "0 9 * * *", "created": past, "delivered": False},
        ]})

        assert r.cmd_check() == 0
        out = capsys.readouterr().out
        assert "REMINDER: one shot" in out
        assert "REMINDER: daily" in out

        data = r.load_reminders()
        ids = [x["id"] for x in data["reminders"]]
        assert ids == ["rem_002"]
        assert datetime.fromisoformat(data["reminders"][0]["fire_at"]) > \
            datetime.now(dt_tz.utc)

    def test_check_quiet_when_nothing_due(self, monkeypatch, tmp_path, capsys):
        r = self._import(monkeypatch, tmp_path)
        monkeypatch.setenv("TZ", "UTC")
        assert r.cmd_check() == 0
        assert capsys.readouterr().out.strip() == "NO_REPLY"

    def test_timezone_from_location_json(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "location.json").write_text(json.dumps(
            {"current": {"timezone": "Asia/Bangkok"}}
        ))
        r = self._import(monkeypatch, tmp_path)
        monkeypatch.setenv("TZ", "Europe/London")
        assert str(r.local_tz()) == "Asia/Bangkok"


_ICS_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Team standup
DTSTART;TZID=Europe/London:20260805T100000
DTEND;TZID=Europe/London:20260805T103000
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
SUMMARY:Holiday
DTSTART;VALUE=DATE:20260805
DTEND;VALUE=DATE:20260806
END:VEVENT
BEGIN:VEVENT
SUMMARY:Daily check
DTSTART:20260801T090000Z
DTEND:20260801T091500Z
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
BEGIN:VEVENT
SUMMARY:Long summary that fold
 s across lines
DTSTART:20260805T120000Z
END:VEVENT
END:VCALENDAR
"""


class TestCalendar:
    def _import(self, monkeypatch, tmp_path):
        import importlib
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        monkeypatch.setenv("SKILL_DATA", str(tmp_path / "sd"))
        monkeypatch.setenv("TZ", "UTC")
        sys.path.insert(0, str(_CONTRIB_SKILLS / "calendar" / "scripts"))
        import cal as cal_mod
        importlib.reload(cal_mod)
        sys.path.pop(0)
        return cal_mod

    def _window(self, cal, day: str):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo("UTC"))
        return start, start + timedelta(days=1)

    def test_parses_vevents_and_folding(self, monkeypatch, tmp_path):
        from zoneinfo import ZoneInfo
        cal = self._import(monkeypatch, tmp_path)
        events = cal.parse_ics(_ICS_SAMPLE, ZoneInfo("UTC"))
        assert len(events) == 4
        summaries = [e.get("SUMMARY") for e in events]
        assert "Team standup" in summaries
        assert "Long summary that folds across lines" in summaries

    def test_tzid_and_utc_datetimes(self, monkeypatch, tmp_path):
        from zoneinfo import ZoneInfo
        cal = self._import(monkeypatch, tmp_path)
        events = cal.parse_ics(_ICS_SAMPLE, ZoneInfo("UTC"))
        standup = next(e for e in events if e["SUMMARY"] == "Team standup")
        start, all_day = standup["DTSTART"]
        assert not all_day
        assert start.utcoffset().total_seconds() == 3600  # BST in August

    def test_all_day_event(self, monkeypatch, tmp_path):
        from zoneinfo import ZoneInfo
        cal = self._import(monkeypatch, tmp_path)
        events = cal.parse_ics(_ICS_SAMPLE, ZoneInfo("UTC"))
        holiday = next(e for e in events if e["SUMMARY"] == "Holiday")
        start, all_day = holiday["DTSTART"]
        assert all_day
        occ = cal.expand_event(
            holiday, *self._window(cal, "2026-08-05"), ZoneInfo("UTC"),
        )
        assert len(occ) == 1
        assert occ[0]["all_day"]

    def test_daily_rrule_with_count(self, monkeypatch, tmp_path):
        from zoneinfo import ZoneInfo
        cal = self._import(monkeypatch, tmp_path)
        events = cal.parse_ics(_ICS_SAMPLE, ZoneInfo("UTC"))
        daily = next(e for e in events if e["SUMMARY"] == "Daily check")
        # within COUNT=10 from Aug 1: occurs Aug 5
        occ = cal.expand_event(
            daily, *self._window(cal, "2026-08-05"), ZoneInfo("UTC"),
        )
        assert len(occ) == 1
        # outside COUNT=10: Aug 15 has no occurrence
        occ = cal.expand_event(
            daily, *self._window(cal, "2026-08-15"), ZoneInfo("UTC"),
        )
        assert occ == []

    def test_weekly_byday_rrule(self, monkeypatch, tmp_path):
        from zoneinfo import ZoneInfo
        cal = self._import(monkeypatch, tmp_path)
        text = (
            "BEGIN:VEVENT\nSUMMARY:Gym\n"
            "DTSTART:20260803T070000Z\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR\n"
            "END:VEVENT\n"
        )
        event = cal.parse_ics(text, ZoneInfo("UTC"))[0]
        occ = cal.expand_event(
            event, *self._window(cal, "2026-08-05"), ZoneInfo("UTC"),
        )
        assert len(occ) == 1  # Wednesday
        occ = cal.expand_event(
            event, *self._window(cal, "2026-08-06"), ZoneInfo("UTC"),
        )
        assert occ == []  # Thursday

    def test_unsupported_rrule_skipped(self, monkeypatch, tmp_path, capsys):
        from zoneinfo import ZoneInfo
        cal = self._import(monkeypatch, tmp_path)
        text = (
            "BEGIN:VEVENT\nSUMMARY:Odd\n"
            "DTSTART:20260805T070000Z\n"
            "RRULE:FREQ=HOURLY\n"
            "END:VEVENT\n"
        )
        event = cal.parse_ics(text, ZoneInfo("UTC"))[0]
        occ = cal.expand_event(
            event, *self._window(cal, "2026-08-05"), ZoneInfo("UTC"),
        )
        assert occ == []
        assert "unsupported RRULE" in capsys.readouterr().err

    def test_file_calendar_and_day_filtering(self, monkeypatch, tmp_path):
        cal = self._import(monkeypatch, tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / "cal").mkdir()
        (tmp_path / "cal" / "p.ics").write_text(_ICS_SAMPLE)
        (tmp_path / "config" / "calendars.json").write_text(json.dumps({
            "calendars": [{"name": "P", "type": "file", "path": "cal/p.ics"}],
        }))
        events = cal.events_in_window(*self._window(cal, "2026-08-05"))
        names = [e["summary"] for e in events]
        assert "Team standup" in names
        assert "Holiday" in names
        assert all(e["calendar"] == "P" for e in events)

        events = cal.events_in_window(*self._window(cal, "2026-09-01"))
        assert events == []

    def test_url_calendar_cache_ttl(self, monkeypatch, tmp_path):
        cal = self._import(monkeypatch, tmp_path)
        cache = cal._cache_path("Work")
        cache.parent.mkdir(parents=True)
        cache.write_text(_ICS_SAMPLE)
        cal_cfg = {"name": "Work", "type": "url",
                   "url": "https://localhost:1/x.ics", "refresh_minutes": 30}
        # fresh cache: no fetch attempted
        text = cal.fetch_calendar(cal_cfg)
        assert "Team standup" in text
        # stale cache with unreachable URL: falls back to cache with warning
        import os
        os.utime(cache, (1, 1))
        text = cal.fetch_calendar(cal_cfg)
        assert text is not None and "Team standup" in text

    def test_json_output_shape(self, monkeypatch, tmp_path):
        from zoneinfo import ZoneInfo
        cal = self._import(monkeypatch, tmp_path)
        events = cal.parse_ics(_ICS_SAMPLE, ZoneInfo("UTC"))
        standup = next(e for e in events if e["SUMMARY"] == "Team standup")
        occ = cal.expand_event(
            standup, *self._window(cal, "2026-08-05"), ZoneInfo("UTC"),
        )
        occ[0]["calendar"] = "P"
        out = json.loads(cal.format_events(occ, as_json=True))
        assert out[0]["summary"] == "Team standup"
        assert out[0]["start"].startswith("2026-08-05T")
        assert out[0]["location"] == "Zoom"


class TestWordDaily:
    def _run_pick(self, tmp_path, args=None):
        return _run(
            "word-daily", "pick_word.py", args or [],
            {"WORKSPACE": str(tmp_path), "SKILL_DATA": str(tmp_path / "sd")},
        )

    def test_seeds_and_picks_word(self, tmp_path):
        result = self._run_pick(tmp_path)
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["word"]
        assert out["translation"]
        assert out["reason"] == "new"
        assert out["languages"]["learning"] == "Vietnamese"
        assert out["languages"]["bridge"] == "Chinese"
        assert out["total_sent"] == 1
        assert (tmp_path / "sd" / "words.json").is_file()

    def test_no_repeat_of_yesterdays_word(self, tmp_path):
        first = json.loads(self._run_pick(tmp_path).stdout)
        second = json.loads(self._run_pick(tmp_path).stdout)
        assert first["id"] != second["id"]

    def test_feedback_sets_interval(self, tmp_path):
        picked = json.loads(self._run_pick(tmp_path).stdout)
        result = self._run_pick(tmp_path, ["--feedback", picked["id"], "2"])
        out = json.loads(result.stdout)
        assert out["status"] == "learning"
        state = json.loads((tmp_path / "sd" / "word-state.json").read_text())
        assert state["words"][picked["id"]]["last_score"] == 2

    def test_invalid_feedback_rejected(self, tmp_path):
        self._run_pick(tmp_path)
        result = self._run_pick(tmp_path, ["--feedback", "g01", "9"])
        assert "error" in json.loads(result.stdout)

    def test_stats_shape(self, tmp_path):
        self._run_pick(tmp_path)
        out = json.loads(self._run_pick(tmp_path, ["--stats"]).stdout)
        assert out["total_words"] > 300
        assert out["total_sent"] == 1

    def test_custom_language_pair(self, tmp_path):
        sd = tmp_path / "sd"
        sd.mkdir()
        (sd / "words.json").write_text(json.dumps({
            "_meta": {"learning_language": "Spanish", "bridge_language": "English"},
            "words": [{"id": "w1", "word": "hola", "translation": "hello",
                       "level": "beginner"}],
        }))
        out = json.loads(self._run_pick(tmp_path).stdout)
        assert out["word"] == "hola"
        assert out["languages"] == {"learning": "Spanish", "bridge": "English"}


class TestWeeklyStateOfMe:
    def _run_generate(self, tmp_path):
        return _run(
            "weekly-state-of-me", "generate.py", [],
            {"WORKSPACE": str(tmp_path), "TZ": "UTC"},
        )

    def test_scaffold_created_with_stats(self, tmp_path):
        from datetime import datetime, timedelta, timezone as dt_tz
        memory = tmp_path / "memory"
        (memory / "dreams").mkdir(parents=True)
        # memory/daily/, the path bootstrap.py actually writes. The fixture
        # previously wrote to memory/ and so agreed with the bug.
        (memory / "daily").mkdir(parents=True)
        today = datetime.now(dt_tz.utc)
        for i in range(3):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            (memory / "daily" / f"{d}.md").write_text("day log")
        (memory / "dreams" / f"{today.strftime('%Y-%m-%d')}.md").write_text("dream")
        (tmp_path / "LEARNINGS.md").write_text("a\nb\nc\n")

        result = self._run_generate(tmp_path)
        assert result.returncode == 0, result.stderr
        out_path = tmp_path / "memory" / "state-of-me" / (
            f"state-of-me-{today.strftime('%Y-%m-%d')}.md"
        )
        assert out_path.is_file()
        content = out_path.read_text()
        assert "Days with conversations: 3/7" in content
        assert "Dreams generated: 1" in content
        assert "LEARNINGS.md length: 3 lines" in content
        assert "Soul proposals in last 30 days: 0" in content
        assert "Part 4: Soul Evolution Proposal" in content

    def test_idempotent(self, tmp_path):
        (tmp_path / "memory").mkdir()
        first = self._run_generate(tmp_path)
        assert "ALREADY_EXISTS" not in first.stdout
        second = self._run_generate(tmp_path)
        assert "ALREADY_EXISTS" in second.stdout

    def test_proposal_velocity_counted(self, tmp_path):
        from datetime import datetime, timedelta, timezone as dt_tz
        proposals = tmp_path / "memory" / "soul-proposals"
        proposals.mkdir(parents=True)
        # Relative to today, not hardcoded. The previous fixture used
        # 2026-08-01 and 2026-07-20 and would have started failing on
        # 2026-08-20, when the older file fell outside the 30 day window.
        now = datetime.now(dt_tz.utc)
        for days in (2, 25):
            stamp = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            (proposals / f"{stamp}.md").write_text("proposal")
        result = self._run_generate(tmp_path)
        assert result.returncode == 0
        out = next((tmp_path / "memory" / "state-of-me").glob("*.md")).read_text()
        assert "Soul proposals in last 30 days: 2" in out

    def test_previous_reflection_referenced(self, tmp_path):
        state = tmp_path / "memory" / "state-of-me"
        state.mkdir(parents=True)
        (state / "state-of-me-2026-07-29.md").write_text("old\nGenerated: x")
        self._run_generate(tmp_path)
        newest = sorted(state.glob("*.md"))[-1]
        assert "state-of-me-2026-07-29.md" in newest.read_text()


class TestUnitConverter:
    def test_standard_conversion(self):
        result = _run("unit-converter", "convert.py", ["100", "kg", "lb"], {})
        assert result.returncode == 0
        assert "220.5 lb" in result.stdout

    def test_temperature(self):
        result = _run("unit-converter", "convert.py", ["72", "f", "c"], {})
        assert "22.22 c" in result.stdout

    def test_ingredient_aware(self):
        result = _run("unit-converter", "convert.py", ["2", "cups", "g", "flour"], {})
        assert "250.0 g" in result.stdout
        assert "125g per cup" in result.stdout

    def test_ingredients_listing(self):
        result = _run("unit-converter", "convert.py", ["ingredients"], {})
        assert result.returncode == 0
        assert "flour" in result.stdout
        assert "per cup" in result.stdout


class TestCurrency:
    def test_usage_without_args(self):
        result = _run("currency", "currency.py", [], {})
        assert result.returncode == 1
        assert "Usage" in result.stdout

    def test_non_numeric_amount_rejected(self):
        result = _run("currency", "currency.py", ["abc", "USD", "VND"], {})
        assert result.returncode == 1
        assert "Not a number" in result.stdout

    def test_alias_resolution(self):
        sys.path.insert(0, str(_CONTRIB_SKILLS / "currency" / "scripts"))
        try:
            import importlib
            import currency
            importlib.reload(currency)
            assert currency.resolve_currency("dong") == "VND"
            assert currency.resolve_currency("Pounds") == "GBP"
            assert currency.resolve_currency("sterling") == "GBP"
            assert currency.resolve_currency("usd") == "USD"
            assert currency.resolve_currency("CHF") == "CHF"
            assert currency.resolve_currency("xyz") == "XYZ"
        finally:
            sys.path.pop(0)


