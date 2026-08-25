import hashlib
import json
import os
from unittest.mock import patch

from zoneinfo import ZoneInfo

from faffmonkey.config import CompactionConfig, Config, HeartbeatConfig, ModelConfig
from faffmonkey.runtime.trust import (
    ALWAYS_TRUSTED,
    TrustEntry,
    is_trusted,
    load_trust_store,
    read_and_check_trust,
    save_trust_store,
    trust_file,
    untrust_file,
)
from faffmonkey.runtime.bootstrap import load_bootstrap
from faffmonkey.cli.trust import run_trust, run_trust_status, run_untrust


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_config(**overrides) -> Config:
    defaults = {
        "models": {
            "main": ModelConfig(
                provider="ollama-local",
                model="llama3",
                base_url="http://localhost:11434/v1",
                api_key="",
            ),
        },
        "routing": {"conversation": "main"},
        "fallback_models": [],
        "timezone": ZoneInfo("UTC"),
        "heartbeat": HeartbeatConfig(),
        "compaction": CompactionConfig(),
        "channels": {},
        "tool_permissions": {
            "file_read": "always",
            "shell_exec": "ask",
        },
    }
    defaults.update(overrides)
    return Config(**defaults)


def _make_workspace(tmp_path, files: dict[str, str] | None = None) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "skills").mkdir(exist_ok=True)
    (workspace / "config").mkdir(exist_ok=True)
    if files:
        for name, content in files.items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


class TestTrustStore:
    def test_trust_stores_hash(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text("some content")

        store: dict[str, TrustEntry] = {}
        trust_file("MEMORY.md", workspace, store)

        assert "MEMORY.md" in store
        expected_hash = _sha256(b"some content")
        assert store["MEMORY.md"].hash == expected_hash
        assert store["MEMORY.md"].trusted_at != ""

    def test_trust_nonexistent_file_returns_false(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store: dict[str, TrustEntry] = {}
        assert trust_file("nope.md", workspace, store) is False
        assert "nope.md" not in store

    def test_untrust_removes_entry(self, tmp_path):
        store = {
            "MEMORY.md": TrustEntry(hash="abc", trusted_at="2026-01-01T00:00:00+00:00"),
        }
        assert untrust_file("MEMORY.md", store) is True
        assert "MEMORY.md" not in store

    def test_untrust_nonexistent_returns_false(self):
        store: dict[str, TrustEntry] = {}
        assert untrust_file("nope.md", store) is False

    def test_save_and_load_roundtrip(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = {
            "MEMORY.md": TrustEntry(hash="abc123", trusted_at="2026-01-01T00:00:00+00:00"),
            "LEARNINGS.md": TrustEntry(hash="def456", trusted_at="2026-01-02T00:00:00+00:00"),
        }
        save_trust_store(state_dir, store)
        loaded = load_trust_store(state_dir)
        assert loaded["MEMORY.md"].hash == "abc123"
        assert loaded["LEARNINGS.md"].hash == "def456"

    def test_load_empty_state_dir(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        assert load_trust_store(state_dir) == {}

    def test_load_corrupt_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "trusted.json").write_text("not json")
        assert load_trust_store(state_dir) == {}


class TestIsTrusted:
    def test_trusted_file_with_matching_hash(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text("content")

        h = _sha256(b"content")
        store = {"MEMORY.md": TrustEntry(hash=h, trusted_at="2026-01-01T00:00:00+00:00")}
        assert is_trusted("MEMORY.md", workspace, store) is True

    def test_modified_file_not_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text("modified content")

        store = {"MEMORY.md": TrustEntry(hash="oldhash", trusted_at="2026-01-01T00:00:00+00:00")}
        assert is_trusted("MEMORY.md", workspace, store) is False

    def test_no_entry_not_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text("content")

        store: dict[str, TrustEntry] = {}
        assert is_trusted("MEMORY.md", workspace, store) is False

    def test_always_trusted_files(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for name in ALWAYS_TRUSTED:
            (workspace / name).write_text("content")
            assert is_trusted(name, workspace, {}) is True

    def test_deleted_file_not_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = {"gone.md": TrustEntry(hash="abc", trusted_at="2026-01-01T00:00:00+00:00")}
        assert is_trusted("gone.md", workspace, store) is False


class TestBootstrapWithTrust:
    def test_trusted_file_loads_unwrapped(self, tmp_path):
        import re
        workspace = tmp_path / "workspace"
        _make_workspace(tmp_path, {"MEMORY.md": "MEMORY_DATA"})

        h = _sha256(b"MEMORY_DATA")
        trust_store = {"MEMORY.md": TrustEntry(hash=h, trusted_at="2026-01-01T00:00:00+00:00")}

        config = _make_config()
        with patch("faffmonkey.runtime.bootstrap._find_template_dir", return_value=None):
            result = load_bootstrap(workspace, config, mode="full", wrap=True, trust_store=trust_store)
        assert "MEMORY_DATA" in result.text
        assert not re.search(r'<untrusted nonce="[^"]+">.*MEMORY_DATA', result.text, re.DOTALL)

    def test_trusted_file_no_wrapping(self, tmp_path):
        import re
        workspace = tmp_path / "workspace"
        _make_workspace(tmp_path, {"MEMORY.md": "MEMORY_DATA"})

        h = _sha256(b"MEMORY_DATA")
        trust_store = {"MEMORY.md": TrustEntry(hash=h, trusted_at="2026-01-01T00:00:00+00:00")}

        config = _make_config()
        with patch("faffmonkey.runtime.bootstrap._find_template_dir", return_value=None):
            result = load_bootstrap(workspace, config, mode="full", wrap=True, trust_store=trust_store)
        assert "MEMORY_DATA" in result.text
        assert len(re.findall(r'</untrusted-[0-9a-f]{16}>', result.text)) == 0

    def test_modified_file_loads_wrapped(self, tmp_path):
        workspace = tmp_path / "workspace"
        _make_workspace(tmp_path, {"MEMORY.md": "MODIFIED_DATA"})

        trust_store = {"MEMORY.md": TrustEntry(hash="stale_hash", trusted_at="2026-01-01T00:00:00+00:00")}

        config = _make_config()
        with patch("faffmonkey.runtime.bootstrap._find_template_dir", return_value=None):
            result = load_bootstrap(workspace, config, mode="full", wrap=True, trust_store=trust_store)
        assert "MODIFIED_DATA" in result.text
        assert '<untrusted nonce="' in result.text

    def test_untrusted_file_loads_wrapped(self, tmp_path):
        workspace = tmp_path / "workspace"
        _make_workspace(tmp_path, {"MEMORY.md": "UNTRUSTED_DATA"})

        config = _make_config()
        with patch("faffmonkey.runtime.bootstrap._find_template_dir", return_value=None):
            result = load_bootstrap(workspace, config, mode="full", wrap=True, trust_store={})
        assert "UNTRUSTED_DATA" in result.text
        assert '<untrusted nonce="' in result.text

    def test_mixed_trusted_and_untrusted(self, tmp_path):
        import re
        workspace = tmp_path / "workspace"
        _make_workspace(tmp_path, {
            "MEMORY.md": "TRUSTED_MEM",
            "LEARNINGS.md": "UNTRUSTED_LEARN",
        })

        h = _sha256(b"TRUSTED_MEM")
        trust_store = {"MEMORY.md": TrustEntry(hash=h, trusted_at="2026-01-01T00:00:00+00:00")}

        config = _make_config()
        with patch("faffmonkey.runtime.bootstrap._find_template_dir", return_value=None):
            result = load_bootstrap(workspace, config, mode="full", wrap=True, trust_store=trust_store)
        assert "TRUSTED_MEM" in result.text
        assert "UNTRUSTED_LEARN" in result.text
        assert len(re.findall(r'</untrusted-[0-9a-f]{16}>', result.text)) == 1
        m = re.search(r'<untrusted nonce="([^"]+)">', result.text)
        assert m
        nonce = m.group(1)
        wrap_start = result.text.index(f'<untrusted nonce="{nonce}">')
        wrap_end = result.text.index(f'</untrusted-{nonce}>')
        assert "UNTRUSTED_LEARN" in result.text[wrap_start:wrap_end]

    def test_no_trust_store_wraps_all(self, tmp_path):
        import re
        workspace = tmp_path / "workspace"
        _make_workspace(tmp_path, {
            "MEMORY.md": "MEM",
            "LEARNINGS.md": "LEARN",
        })
        config = _make_config()
        with patch("faffmonkey.runtime.bootstrap._find_template_dir", return_value=None):
            result = load_bootstrap(workspace, config, mode="full", wrap=True)
        assert len(re.findall(r'</untrusted-[0-9a-f]{16}>', result.text)) == 2


class TestTrustCLI:
    def test_run_trust_stores_hash(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text("trusted content")

        rc = run_trust(state_dir, workspace, "MEMORY.md")
        assert rc == 0
        store = load_trust_store(state_dir)
        assert "MEMORY.md" in store
        assert store["MEMORY.md"].hash == _sha256(b"trusted content")

    def test_run_trust_directory(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mem_dir = workspace / "memory"
        mem_dir.mkdir()
        (mem_dir / "log1.md").write_text("log one")
        (mem_dir / "log2.md").write_text("log two")

        rc = run_trust(state_dir, workspace, "memory/")
        assert rc == 0
        store = load_trust_store(state_dir)
        assert "memory/log1.md" in store
        assert "memory/log2.md" in store

    def test_run_trust_nonexistent_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        rc = run_trust(state_dir, workspace, "nope.md")
        assert rc == 1

    def test_run_trust_nonexistent_dir(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        rc = run_trust(state_dir, workspace, "nope/")
        assert rc == 1

    def test_run_untrust_removes_entry(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = {"MEMORY.md": TrustEntry(hash="abc", trusted_at="2026-01-01T00:00:00+00:00")}
        save_trust_store(state_dir, store)

        rc = run_untrust(state_dir, workspace, "MEMORY.md")
        assert rc == 0
        reloaded = load_trust_store(state_dir)
        assert "MEMORY.md" not in reloaded

    def test_run_untrust_nonexistent_returns_1(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        rc = run_untrust(state_dir, workspace, "nope.md")
        assert rc == 1

    def test_trust_status_output(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("soul")
        (workspace / "MEMORY.md").write_text("memory content")
        (workspace / "notes.md").write_text("notes")

        h = _sha256(b"notes")
        store = {"notes.md": TrustEntry(hash=h, trusted_at="2026-01-01T00:00:00+00:00")}
        save_trust_store(state_dir, store)

        run_trust_status(state_dir, workspace)
        out = capsys.readouterr().out

        assert "Not tracked (always trusted):" in out
        assert "SOUL.md" in out
        assert "Trusted (hash current):" in out
        assert "notes.md" in out
        assert "Untrusted:" in out
        assert "MEMORY.md" in out

    def test_trust_path_traversal_rejected(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        rc = run_trust(state_dir, workspace, "../../etc/passwd")
        assert rc == 1

    def test_untrust_path_traversal_rejected(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        rc = run_untrust(state_dir, workspace, "../../etc/passwd")
        assert rc == 1


class TestIsTrustedSubdir:
    def test_subdir_soul_md_not_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "SOUL.md").write_text("fake soul")

        assert is_trusted("subdir/SOUL.md", workspace, {}) is False

    def test_root_soul_md_always_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("real soul")

        assert is_trusted("SOUL.md", workspace, {}) is True


class TestWorkspaceContainment:
    def test_traversal_rejected_by_read_and_check_trust(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        (state / ".env").write_text("SECRET=hunter2")

        result = read_and_check_trust("../state/.env", workspace, {})
        assert result is None

    def test_double_dot_deep_traversal_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "secret.txt").write_text("secret")

        result = read_and_check_trust("subdir/../../secret.txt", workspace, {})
        assert result is None


class TestPathNormalisation:
    def test_dotslash_and_bare_same_trust_key(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("soul content")

        assert is_trusted("./SOUL.md", workspace, {}) is True
        assert is_trusted("SOUL.md", workspace, {}) is True

    def test_trust_file_normalises_key(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "notes.md").write_text("notes")

        store: dict[str, TrustEntry] = {}
        trust_file("./notes.md", workspace, store)
        assert "notes.md" in store
        assert "./notes.md" not in store

    def test_untrust_normalises_key(self, tmp_path):
        store = {
            "notes.md": TrustEntry(hash="abc", trusted_at="2026-01-01T00:00:00+00:00"),
        }
        assert untrust_file("./notes.md", store) is True
        assert "notes.md" not in store

    def test_double_slash_normalised(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mem_dir = workspace / "memory"
        mem_dir.mkdir()
        (mem_dir / "log.md").write_text("log")

        store: dict[str, TrustEntry] = {}
        trust_file("memory//log.md", workspace, store)
        assert "memory/log.md" in store

    def test_save_normalises_keys(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = {
            "./MEMORY.md": TrustEntry(hash="abc", trusted_at="2026-01-01T00:00:00+00:00"),
        }
        save_trust_store(state_dir, store)
        loaded = load_trust_store(state_dir)
        assert "MEMORY.md" in loaded
        assert "./MEMORY.md" not in loaded


class TestAlwaysTrustedExistence:
    def test_nonexistent_always_trusted_returns_false(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        for name in ALWAYS_TRUSTED:
            assert is_trusted(name, workspace, {}) is False

    def test_existing_always_trusted_returns_true(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for name in ALWAYS_TRUSTED:
            (workspace / name).write_text("content")
            assert is_trusted(name, workspace, {}) is True


class TestWorkspaceFilesSkipsSymlinks:
    def test_symlinked_file_excluded(self, tmp_path):
        from faffmonkey.cli.trust import _workspace_files
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        real = workspace / "real.md"
        real.write_text("real content")
        link = workspace / "link.md"
        link.symlink_to(real)

        files = _workspace_files(workspace)
        assert "real.md" in files
        assert "link.md" not in files

    def test_symlinked_dir_excluded(self, tmp_path):
        from faffmonkey.cli.trust import _workspace_files
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        real_dir = tmp_path / "outside"
        real_dir.mkdir()
        (real_dir / "secret.md").write_text("secret")
        link_dir = workspace / "linked"
        link_dir.symlink_to(real_dir)

        files = _workspace_files(workspace)
        assert not any("secret" in f for f in files)

    def test_trust_dir_skips_symlinks(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mem_dir = workspace / "memory"
        mem_dir.mkdir()
        (mem_dir / "real.md").write_text("real")
        outside = tmp_path / "outside.md"
        outside.write_text("outside")
        (mem_dir / "link.md").symlink_to(outside)

        rc = run_trust(state_dir, workspace, "memory/")
        assert rc == 0
        store = load_trust_store(state_dir)
        assert "memory/real.md" in store
        assert "memory/link.md" not in store


class TestSymlinkBypassPrevented:
    def test_symlink_soul_md_not_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "evil.md").write_text("evil content")
        (workspace / "SOUL.md").symlink_to(subdir / "evil.md")

        assert is_trusted("SOUL.md", workspace, {}) is False

    def test_symlink_user_md_not_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "file.md").write_text("some content")
        (workspace / "USER.md").symlink_to(subdir / "file.md")

        assert is_trusted("USER.md", workspace, {}) is False

    def test_non_symlink_soul_md_still_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("real soul")

        assert is_trusted("SOUL.md", workspace, {}) is True

    def test_read_and_check_trust_symlink_soul_md_untrusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "evil.md").write_text("evil content")
        (workspace / "SOUL.md").symlink_to(subdir / "evil.md")

        result = read_and_check_trust("SOUL.md", workspace, {})
        assert result is not None
        assert result.trusted is False

    def test_read_and_check_trust_symlink_user_md_untrusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "file.md").write_text("content")
        (workspace / "USER.md").symlink_to(subdir / "file.md")

        result = read_and_check_trust("USER.md", workspace, {})
        assert result is not None
        assert result.trusted is False

    def test_read_and_check_trust_non_symlink_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("real soul")

        result = read_and_check_trust("SOUL.md", workspace, {})
        assert result is not None
        assert result.trusted is True


class TestCaseSensitiveAlwaysTrusted:
    def test_case_mismatch_is_trusted_returns_false(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "soul.md").write_text("fake soul via case trick")

        assert is_trusted("SOUL.md", workspace, {}) is False

    def test_case_mismatch_read_and_check_trust_untrusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "soul.md").write_text("fake soul via case trick")

        # The old guard was `if result is not None:`, which skipped the only
        # assertion on Linux, the platform this deploys to. Both outcomes are
        # now pinned, so a regression returning trusted=True fails somewhere.
        case_insensitive = (workspace / "SOUL.md").exists()
        result = read_and_check_trust("SOUL.md", workspace, {})
        if case_insensitive:
            # macOS resolves the path to the lowercase file, so the read
            # succeeds and must not carry trust.
            assert result is not None
            assert result.trusted is False
        else:
            # Case-sensitive filesystem: SOUL.md does not exist, so there is
            # nothing to read and nothing to trust.
            assert result is None

    def test_exact_case_still_trusted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("real soul")

        assert is_trusted("SOUL.md", workspace, {}) is True
        result = read_and_check_trust("SOUL.md", workspace, {})
        assert result is not None
        assert result.trusted is True


class TestLoadTrustStoreRejectsTraversal:
    def test_dotdot_key_rejected_on_load(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        data = {
            "../etc/passwd": {"hash": "abc", "trusted_at": "2026-01-01T00:00:00+00:00"},
            "legit.md": {"hash": "def", "trusted_at": "2026-01-01T00:00:00+00:00"},
        }
        (state_dir / "trusted.json").write_text(json.dumps(data))
        store = load_trust_store(state_dir)
        assert "../etc/passwd" not in store
        assert "legit.md" in store

    def test_nested_dotdot_key_rejected_on_load(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        data = {
            "sub/../../secret": {"hash": "abc", "trusted_at": "2026-01-01T00:00:00+00:00"},
        }
        (state_dir / "trusted.json").write_text(json.dumps(data))
        store = load_trust_store(state_dir)
        assert len(store) == 0


class TestIsTrustedRejectsTraversal:
    def test_dotdot_path_returns_false(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = {"../evil": TrustEntry(hash="abc", trusted_at="2026-01-01T00:00:00+00:00")}
        assert is_trusted("../evil", workspace, store) is False

    def test_nested_dotdot_path_returns_false(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = {}
        assert is_trusted("sub/../../evil", workspace, store) is False


class TestTrustFilePathTraversal:
    def test_dotdot_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "evil.md").write_text("evil")

        store: dict[str, TrustEntry] = {}
        assert trust_file("../evil.md", workspace, store) is False
        assert len(store) == 0

    def test_deep_dotdot_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "sub"
        subdir.mkdir()
        (tmp_path / "evil.md").write_text("evil")

        store: dict[str, TrustEntry] = {}
        assert trust_file("sub/../../evil.md", workspace, store) is False
        assert len(store) == 0

    def test_valid_path_still_works(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "notes.md").write_text("notes")

        store: dict[str, TrustEntry] = {}
        assert trust_file("notes.md", workspace, store) is True
        assert "notes.md" in store


class TestCorruptTrustStoreIsQuarantined:
    """D17: a truncated file was silently replaced on the next write."""

    def test_unreadable_file_moves_aside(self, tmp_path):
        path = tmp_path / "trusted.json"
        path.write_text('{"a": {"hash": "x"')

        assert load_trust_store(tmp_path) == {}
        assert not path.exists()
        assert (tmp_path / "trusted.json.corrupt").read_text() == '{"a": {"hash": "x"'

    def test_non_object_file_moves_aside(self, tmp_path):
        path = tmp_path / "trusted.json"
        path.write_text("[]")

        assert load_trust_store(tmp_path) == {}
        assert (tmp_path / "trusted.json.corrupt").exists()
