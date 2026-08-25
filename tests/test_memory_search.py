"""Tests for the memory-search skill scripts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "templates" / "workspace" / "skills" / "memory-search" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from datetime import date

from index import chunk_markdown, collect_files, doc_date_for, file_sha256, index_file, init_db, load_skill_config, ensure_config
from providers import blob_to_vec, cosine_similarity, load_config, vec_to_blob
from search import _sanitise_fts_query, detect_mode, fts_search, load_half_life, recency_weight, rrf_merge, snippet


# --- Markdown chunking ---


class TestChunkMarkdown:
    def test_single_heading(self):
        text = "# Title\nSome content here."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0]["heading"] == "Title"
        assert "Some content here." in chunks[0]["content"]
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 2

    def test_multiple_headings(self):
        text = "# First\nContent one.\n## Second\nContent two."
        chunks = chunk_markdown(text)
        assert len(chunks) == 2
        assert chunks[0]["heading"] == "First"
        assert chunks[1]["heading"] == "Second"

    def test_no_heading_uses_top(self):
        text = "Just some text without headings.\nMore text."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0]["heading"] == "(top)"

    def test_empty_file(self):
        chunks = chunk_markdown("")
        assert chunks == []

    def test_whitespace_only(self):
        chunks = chunk_markdown("   \n\n  \n")
        assert chunks == []

    def test_heading_only(self):
        text = "# Just a heading"
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0]["heading"] == "Just a heading"

    def test_oversized_section_splits(self):
        long_content = "# Big\n" + ("word " * 500)
        chunks = chunk_markdown(long_content, max_chars=200)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk["content"]) <= 200
            assert chunk["heading"] == "Big"

    def test_paragraph_split(self):
        text = "# Test\nParagraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = chunk_markdown(text, max_chars=40)
        assert len(chunks) >= 2
        all_content = " ".join(c["content"] for c in chunks)
        assert "Paragraph one." in all_content
        assert "Paragraph two." in all_content
        assert "Paragraph three." in all_content

    def test_respects_max_chars(self):
        text = "# Heading\n" + "x" * 100 + "\n\n" + "y" * 100
        chunks = chunk_markdown(text, max_chars=150)
        for chunk in chunks:
            assert len(chunk["content"]) <= 150

    def test_line_numbers_sequential(self):
        text = "# A\nline\n# B\nline\n# C\nline"
        chunks = chunk_markdown(text)
        assert chunks[0]["start_line"] == 1
        assert chunks[1]["start_line"] == 3
        assert chunks[2]["start_line"] == 5

    def test_nested_headings(self):
        text = "# H1\ncontent\n## H2\ncontent\n### H3\ncontent"
        chunks = chunk_markdown(text)
        assert len(chunks) == 3
        assert chunks[0]["heading"] == "H1"
        assert chunks[1]["heading"] == "H2"
        assert chunks[2]["heading"] == "H3"


# --- FTS5 indexing and search ---


class TestFTS5:
    @pytest.fixture()
    def db(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        return init_db(db_path)

    @pytest.fixture()
    def workspace(self, ws):
        mem_dir = ws / "memory"
        mem_dir.mkdir()
        return ws

    def test_init_db_creates_tables(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "chunks" in tables
        assert "chunks_fts" in tables
        assert "file_hashes" in tables
        assert "embeddings" in tables

    def test_index_and_search(self, db, workspace):
        f = workspace / "memory" / "2024-01-01.md"
        f.write_text("# Morning\nDiscussed the new API design with Alice.")

        index_file(db, workspace, f, 1600, None, force=False)

        results = fts_search(db, "API design", 10)
        assert len(results) >= 1
        assert "API design" in results[0]["content"]

    def test_search_across_files(self, db, workspace):
        f1 = workspace / "memory" / "2024-01-01.md"
        f1.write_text("# Monday\nWorked on database migration.")
        f2 = workspace / "memory" / "2024-01-02.md"
        f2.write_text("# Tuesday\nFixed the authentication bug.")

        index_file(db, workspace, f1, 1600, None, force=False)
        index_file(db, workspace, f2, 1600, None, force=False)

        results = fts_search(db, "authentication", 10)
        assert len(results) >= 1
        assert "authentication" in results[0]["content"]

        results = fts_search(db, "migration", 10)
        assert len(results) >= 1
        assert "migration" in results[0]["content"]

    def test_search_no_results(self, db, workspace):
        f = workspace / "memory" / "2024-01-01.md"
        f.write_text("# Test\nNothing relevant here.")
        index_file(db, workspace, f, 1600, None, force=False)
        results = fts_search(db, "xyznonexistent", 10)
        assert results == []


# --- Recency weighting ---


class TestRecencyWeighting:
    @pytest.fixture()
    def db(self, tmp_path):
        return init_db(tmp_path / "test.sqlite")

    @pytest.fixture()
    def workspace(self, tmp_path):
        ws = tmp_path / "workspace"
        (ws / "memory").mkdir(parents=True)
        return ws

    def test_doc_date_from_filename(self, workspace):
        f = workspace / "memory" / "2026-05-01.md"
        f.write_text("# Note\ncontent")
        assert doc_date_for(f) == "2026-05-01"

    def test_doc_date_from_mtime_when_undated(self, workspace):
        import time as _time
        f = workspace / "MEMORY.md"
        f.write_text("# Index\ncontent")
        expected = _time.strftime("%Y-%m-%d", _time.localtime(f.stat().st_mtime))
        assert doc_date_for(f) == expected

    def test_indexed_chunks_carry_doc_date(self, db, workspace):
        f = workspace / "memory" / "2026-05-01.md"
        f.write_text("# Note\nvisa appointment details")
        index_file(db, workspace, f, 1600, None, force=False)
        row = db.execute("SELECT doc_date FROM chunks").fetchone()
        assert row[0] == "2026-05-01"

    def test_recency_weight_halves_per_half_life(self):
        today = date(2026, 8, 6)
        assert recency_weight("2026-08-06", today, 30) == 1.0
        assert recency_weight("2026-07-07", today, 30) == pytest.approx(0.5)
        assert recency_weight(None, today, 30) == 1.0
        assert recency_weight("not-a-date", today, 30) == 1.0
        assert recency_weight("2026-01-01", today, 0) == 1.0

    def test_future_dates_not_boosted(self):
        assert recency_weight("2026-12-31", date(2026, 8, 6), 30) == 1.0

    def test_fresh_note_outranks_stale_note(self, db, workspace):
        stale = workspace / "memory" / "2026-02-01.md"
        stale.write_text(
            "# Deploy\ndeploy pipeline deploy pipeline deploy pipeline broken badly"
        )
        fresh = workspace / "memory" / "2026-08-05.md"
        fresh.write_text("# Deploy\ndeploy pipeline fixed")
        index_file(db, workspace, stale, 1600, None, force=False)
        index_file(db, workspace, fresh, 1600, None, force=False)

        undecayed = fts_search(db, "deploy pipeline", 10)
        assert undecayed[0]["source_file"] == "memory/2026-02-01.md"

        decayed = fts_search(
            db, "deploy pipeline", 10,
            today=date(2026, 8, 6), half_life_days=30,
        )
        assert decayed[0]["source_file"] == "memory/2026-08-05.md"

    def test_old_index_without_doc_date_migrates(self, tmp_path):
        db_path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                heading TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                indexed_at REAL NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO chunks (source_file, start_line, end_line, heading, content, content_hash, indexed_at)
               VALUES ('memory/old.md', 1, 2, 'h', 'legacy row', 'x', 0)"""
        )
        conn.commit()
        conn.close()

        conn = init_db(db_path)
        row = conn.execute("SELECT doc_date FROM chunks").fetchone()
        assert row[0] is None

    def test_load_half_life_config(self, tmp_path):
        assert load_half_life(tmp_path) == 30.0
        (tmp_path / "config.json").write_text(json.dumps({"recency_half_life_days": 7}))
        assert load_half_life(tmp_path) == 7.0
        (tmp_path / "config.json").write_text(json.dumps({"recency_half_life_days": 0}))
        assert load_half_life(tmp_path) == 0.0


# --- Incremental indexing ---


class TestIncrementalIndexing:
    @pytest.fixture()
    def setup(self, tmp_path, ws):
        (ws / "memory").mkdir()
        skill_data = tmp_path / "skill-data"
        skill_data.mkdir()
        db_path = skill_data / "index.sqlite"
        conn = init_db(db_path)
        return ws, conn

    def test_unchanged_file_skipped(self, setup):
        ws, conn = setup
        f = ws / "memory" / "test.md"
        f.write_text("# Test\nSome content.")

        count1 = index_file(conn, ws, f, 1600, None, force=False)
        assert count1 == 1

        count2 = index_file(conn, ws, f, 1600, None, force=False)
        assert count2 == 0

    def test_changed_file_reindexed(self, setup):
        ws, conn = setup
        f = ws / "memory" / "test.md"
        f.write_text("# Test\nOriginal content.")

        count1 = index_file(conn, ws, f, 1600, None, force=False)
        assert count1 == 1

        f.write_text("# Test\nUpdated content with more words.")

        count2 = index_file(conn, ws, f, 1600, None, force=False)
        assert count2 == 1

        chunks = conn.execute("SELECT content FROM chunks WHERE source_file = ?",
                              (str(f.relative_to(ws)),)).fetchall()
        assert len(chunks) == 1
        assert "Updated" in chunks[0][0]

    def test_force_reindexes_unchanged(self, setup):
        ws, conn = setup
        f = ws / "memory" / "test.md"
        f.write_text("# Test\nContent.")

        index_file(conn, ws, f, 1600, None, force=False)
        count = index_file(conn, ws, f, 1600, None, force=True)
        assert count == 1


# --- File hash tracking ---


class TestFileHashes:
    def test_sha256(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert file_sha256(f) == expected

    def test_hash_stored_in_db(self, tmp_path, ws):
        (ws / "memory").mkdir()
        db_path = tmp_path / "test.sqlite"
        conn = init_db(db_path)

        f = ws / "memory" / "test.md"
        f.write_text("# Test\nContent.")
        index_file(conn, ws, f, 1600, None, force=False)

        row = conn.execute("SELECT content_hash FROM file_hashes WHERE file_path = ?",
                           ("memory/test.md",)).fetchone()
        assert row is not None
        assert row[0] == file_sha256(f)


# --- RRF merge ---


class TestRRFMerge:
    def test_merge_disjoint(self):
        fts = [
            {"chunk_id": 1, "content": "a", "score": 1.0},
            {"chunk_id": 2, "content": "b", "score": 0.5},
        ]
        vec = [
            {"chunk_id": 3, "content": "c", "score": 0.9},
            {"chunk_id": 4, "content": "d", "score": 0.4},
        ]
        merged = rrf_merge(fts, vec, top_k=4, k=60)
        assert len(merged) == 4
        ids = [r["chunk_id"] for r in merged]
        assert 1 in ids
        assert 3 in ids

    def test_merge_overlapping(self):
        fts = [
            {"chunk_id": 1, "content": "a", "score": 1.0},
            {"chunk_id": 2, "content": "b", "score": 0.5},
        ]
        vec = [
            {"chunk_id": 1, "content": "a", "score": 0.9},
            {"chunk_id": 3, "content": "c", "score": 0.4},
        ]
        merged = rrf_merge(fts, vec, top_k=3, k=60)
        # chunk_id=1 appears in both, should have highest RRF score
        assert merged[0]["chunk_id"] == 1

    def test_merge_respects_top_k(self):
        fts = [{"chunk_id": i, "content": str(i), "score": 1.0} for i in range(10)]
        vec = [{"chunk_id": i + 10, "content": str(i), "score": 1.0} for i in range(10)]
        merged = rrf_merge(fts, vec, top_k=5, k=60)
        assert len(merged) == 5

    def test_merge_empty_inputs(self):
        assert rrf_merge([], [], top_k=10) == []
        fts = [{"chunk_id": 1, "content": "a", "score": 1.0}]
        merged = rrf_merge(fts, [], top_k=10)
        assert len(merged) == 1
        assert merged[0]["chunk_id"] == 1

    def test_rrf_scores_decrease(self):
        fts = [
            {"chunk_id": 1, "content": "a", "score": 1.0},
            {"chunk_id": 2, "content": "b", "score": 0.5},
            {"chunk_id": 3, "content": "c", "score": 0.3},
        ]
        merged = rrf_merge(fts, [], top_k=3, k=60)
        for i in range(len(merged) - 1):
            assert merged[i]["score"] >= merged[i + 1]["score"]


# --- Vector serialisation ---


class TestVectorSerialisation:
    def test_round_trip(self):
        vec = [0.1, 0.2, 0.3, -0.5, 1.0]
        blob = vec_to_blob(vec)
        restored = blob_to_vec(blob)
        assert len(restored) == len(vec)
        for a, b in zip(vec, restored):
            assert abs(a - b) < 1e-6

    def test_empty_vector(self):
        vec: list[float] = []
        blob = vec_to_blob(vec)
        assert blob == b""
        restored = blob_to_vec(blob)
        assert restored == []

    def test_blob_format(self):
        vec = [1.0, 2.0]
        blob = vec_to_blob(vec)
        assert len(blob) == 8  # 2 floats * 4 bytes
        assert struct.unpack("<2f", blob) == (1.0, 2.0)


# --- Cosine similarity ---


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        assert cosine_similarity(a, b) == 0.0

    def test_different_lengths(self):
        a = [1.0, 2.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0


# --- Config loading ---


class TestConfigLoading:
    def test_load_config_no_file(self, tmp_path):
        assert load_config(tmp_path) is None

    def test_load_config_with_embedding(self, tmp_path):
        cfg = {"embedding": {"provider": "auto", "providers": {"ollama": {"endpoint": "http://localhost:11434/api/embed", "model": "test", "format": "ollama"}}}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        result = load_config(tmp_path)
        assert result is not None
        assert "ollama" in result["providers"]
        assert result["providers"]["ollama"]["format"] == "ollama"

    def test_load_config_no_embedding_section(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"index_paths": ["memory/"]}))
        result = load_config(tmp_path)
        assert result is None

    def test_load_config_invalid_json(self, tmp_path):
        (tmp_path / "config.json").write_text("not json{{{")
        assert load_config(tmp_path) is None

    def test_load_skill_config_defaults(self, tmp_path):
        cfg = load_skill_config(tmp_path)
        assert cfg["index_paths"] == ["memory/", "LEARNINGS.md"]
        assert cfg["max_chunk_chars"] == 1600

    def test_ensure_config_creates_default(self, tmp_path):
        skill_data = tmp_path / "skill-data"
        skill_data.mkdir()
        ensure_config(skill_data)
        config_path = skill_data / "config.json"
        assert config_path.exists()
        cfg = json.loads(config_path.read_text())
        assert cfg["max_chunk_chars"] == 1600
        assert cfg["embedding"]["provider"] == "none"
        assert len(cfg["embedding"]["providers"]) == 1

    def test_ensure_config_does_not_overwrite(self, tmp_path):
        skill_data = tmp_path / "skill-data"
        skill_data.mkdir()
        existing = {"index_paths": ["custom/"], "max_chunk_chars": 800}
        (skill_data / "config.json").write_text(json.dumps(existing))
        ensure_config(skill_data)
        cfg = json.loads((skill_data / "config.json").read_text())
        assert cfg["max_chunk_chars"] == 800


# --- Collect files ---


class TestCollectFiles:
    def test_collects_memory_md_and_dir(self, ws):
        (ws / "MEMORY.md").write_text("# Index")
        mem = ws / "memory"
        mem.mkdir()
        (mem / "2024-01-01.md").write_text("# Day")
        (mem / "2024-01-02.md").write_text("# Day")

        files = collect_files(ws, ["memory/"])
        paths = [str(f.relative_to(ws)) for f in files]
        assert "MEMORY.md" in paths
        assert "memory/2024-01-01.md" in paths
        assert "memory/2024-01-02.md" in paths

    def test_no_memory_md(self, ws):
        mem = ws / "memory"
        mem.mkdir()
        (mem / "test.md").write_text("# Test")

        files = collect_files(ws, ["memory/"])
        paths = [str(f.relative_to(ws)) for f in files]
        assert "MEMORY.md" not in paths
        assert "memory/test.md" in paths

    def test_nested_directories(self, ws):
        person = ws / "memory" / "person"
        person.mkdir(parents=True)
        (person / "alice.md").write_text("# Alice")

        files = collect_files(ws, ["memory/"])
        paths = [str(f.relative_to(ws)) for f in files]
        assert "memory/person/alice.md" in paths

    def test_empty_workspace(self, ws):
        files = collect_files(ws, ["memory/"])
        assert files == []


# --- Snippet ---


class TestSnippet:
    def test_short_content_unchanged(self):
        assert snippet("hello") == "hello"

    def test_long_content_truncated(self):
        long = "a" * 800
        result = snippet(long, max_len=700)
        assert len(result) == 700
        assert result.endswith("...")

    def test_exact_boundary(self):
        text = "a" * 700
        assert snippet(text, max_len=700) == text


# --- FTS query sanitisation ---


class TestSanitiseFTSQuery:
    def test_normal_query(self):
        assert _sanitise_fts_query("hello world") == "hello OR world"

    def test_special_characters_stripped(self):
        result = _sanitise_fts_query('hello "world" (test)')
        assert '"' not in result
        assert "(" not in result

    def test_empty_query(self):
        assert _sanitise_fts_query("") == ""

    def test_only_special_chars(self):
        assert _sanitise_fts_query("!@#$%") == ""


# --- detect_mode ---


class TestDetectMode:
    @pytest.fixture()
    def db_no_embeddings(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn = init_db(db_path)
        return conn

    @pytest.fixture()
    def db_with_embeddings(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn = init_db(db_path)
        conn.execute(
            "INSERT INTO chunks (source_file, start_line, end_line, heading, content, content_hash, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test.md", 1, 1, "test", "test", "abc", 0.0),
        )
        chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO embeddings (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, vec_to_blob([0.1, 0.2])),
        )
        conn.commit()
        return conn

    def test_fts_when_no_embeddings(self, db_no_embeddings):
        assert detect_mode(db_no_embeddings, "") == "fts"

    def test_hybrid_when_embeddings_exist(self, db_with_embeddings):
        assert detect_mode(db_with_embeddings, "") == "hybrid"

    def test_explicit_fts(self, db_with_embeddings):
        assert detect_mode(db_with_embeddings, "fts") == "fts"

    def test_explicit_vector(self, db_no_embeddings):
        assert detect_mode(db_no_embeddings, "vector") == "vector"


class TestAutoIndexOnSearch:
    """2026-08-24: search dead-ended with "Index does not exist" and nothing
    in the install ever built the index; search now builds or refreshes it
    before every query."""

    def _env(self, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        (workspace / "memory").mkdir(parents=True)
        (workspace / "memory" / "note.md").write_text(
            "# Note\nThe visa appointment is May 16."
        )
        skill_data = tmp_path / "sd"
        skill_data.mkdir()
        # No embedding block: the test must not attempt network calls.
        (skill_data / "config.json").write_text(json.dumps({
            "index_paths": ["memory/"],
            "max_chunk_chars": 1600,
            "search_top_k": 10,
            "recency_half_life_days": 0,
        }))
        monkeypatch.setenv("WORKSPACE", str(workspace))
        monkeypatch.setenv("SKILL_DATA", str(skill_data))
        return workspace, skill_data

    def test_search_builds_missing_index(self, tmp_path, monkeypatch, capsys):
        import search as search_mod
        workspace, skill_data = self._env(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["search.py", "visa"])
        search_mod.main()
        out = capsys.readouterr().out
        assert (skill_data / "index.sqlite").exists()
        assert "visa" in out.lower()

    def test_search_picks_up_new_files(self, tmp_path, monkeypatch, capsys):
        import search as search_mod
        workspace, skill_data = self._env(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["search.py", "visa"])
        search_mod.main()
        capsys.readouterr()
        (workspace / "memory" / "later.md").write_text(
            "# Later\nThe dentist appointment moved to June 2."
        )
        monkeypatch.setattr(sys, "argv", ["search.py", "dentist"])
        search_mod.main()
        out = capsys.readouterr().out
        assert "dentist" in out.lower()


class TestEmbeddingPrivacy:
    """2026-08-24: the default config shipped provider "auto" with a
    prefilled OpenRouter entry, so installing an unrelated skill that set
    OPENROUTER_API_KEY silently sent memory chunks to a remote embedding
    endpoint. Embeddings are now off by default and never widen beyond the
    named provider."""

    _PROVIDERS = {
        "remote": {
            "endpoint": "https://example.invalid/embeddings",
            "model": "m",
            "apiKeyEnvVar": "SOME_KEY",
            "format": "openai",
        },
    }

    def test_provider_none_never_embeds(self, monkeypatch):
        from providers import embed
        monkeypatch.setenv("SOME_KEY", "set")
        calls = []
        import providers as providers_mod
        monkeypatch.setattr(providers_mod, "_embed_openai_compat",
                            lambda *a: calls.append(a) or [0.1])
        result = embed("private note", {"provider": "none", "providers": self._PROVIDERS})
        assert result is None
        assert calls == []

    def test_unknown_provider_does_not_widen_to_auto(self, monkeypatch):
        from providers import embed
        monkeypatch.setenv("SOME_KEY", "set")
        calls = []
        import providers as providers_mod
        monkeypatch.setattr(providers_mod, "_embed_openai_compat",
                            lambda *a: calls.append(a) or [0.1])
        result = embed("private note", {"provider": "typo", "providers": self._PROVIDERS})
        assert result is None
        assert calls == []

    def test_default_config_ships_disabled_and_local_only(self, tmp_path):
        skill_data = tmp_path / "sd"
        skill_data.mkdir()
        ensure_config(skill_data)
        cfg = json.loads((skill_data / "config.json").read_text())
        embedding = cfg["embedding"]
        assert embedding["provider"] == "none"
        assert "openrouter" not in embedding["providers"]
        for pcfg in embedding["providers"].values():
            assert pcfg["endpoint"].startswith("http://localhost")

    def test_index_with_default_config_writes_no_embeddings(self, tmp_path, monkeypatch):
        from index import run_index
        workspace = tmp_path / "ws"
        (workspace / "memory").mkdir(parents=True)
        (workspace / "memory" / "note.md").write_text("# N\nSecret content.")
        skill_data = tmp_path / "sd"
        skill_data.mkdir()
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-should-never-be-used")
        summary = run_index(workspace, skill_data)
        assert "Indexed" in summary
        conn = sqlite3.connect(str(skill_data / "index.sqlite"))
        try:
            assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
        finally:
            conn.close()
