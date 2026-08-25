"""Tests for the document-search built-in skill scripts."""

import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "templates" / "workspace" / "skills" / "document-search" / "scripts"
)


def _run_script(workspace: Path, action: str, args: list[str] | None = None):
    env = {
        "WORKSPACE": str(workspace),
        "SKILL_DATA": str(workspace / "skills-data" / "document-search"),
        "TZ": "UTC",
        "PATH": "/usr/bin:/bin",
    }
    cmd = [sys.executable, str(_SCRIPTS / f"{action}.py")]
    if args:
        cmd.extend(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, cwd=str(workspace), env=env,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "documents").mkdir(parents=True)
    return ws


def _write_xlsx(path: Path) -> None:
    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '</Types>'
    )
    workbook = (
        '<?xml version="1.0"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheets><sheet name="Budget" sheetId="1" r:id="rId1" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
        '</sheets></workbook>'
    )
    shared = (
        '<?xml version="1.0"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2" uniqueCount="2">'
        '<si><t>projector</t></si><si><t>total cost</t></si></sst>'
    )
    sheet = (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="A1" t="s"><v>1</v></c><c r="B1"><v>4200</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2"><v>899</v></c></row>'
        '</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


def _write_pdf(path: Path, text: str = "the kraken awakens in the harbour") -> None:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    compressed = zlib.compress(stream)
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Length " + str(len(compressed)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n"
        b"%%EOF\n"
    )
    path.write_bytes(body)


class TestIndexAndSearch:
    def test_indexes_and_finds_markdown(self, workspace):
        (workspace / "documents" / "notes.md").write_text(
            "# Q3 review\n\nChurn rate fell to 2.1% after the pricing change.\n"
        )
        result = _run_script(workspace, "index")
        assert result.returncode == 0, result.stderr
        assert "indexed 1 file(s)" in result.stdout

        result = _run_script(workspace, "search", ["churn", "pricing"])
        assert result.returncode == 0, result.stderr
        assert "notes.md" in result.stdout
        assert "Churn rate" in result.stdout

    def test_csv_rows_indexed_with_header(self, workspace):
        (workspace / "documents" / "spend.csv").write_text(
            "item,cost\nlaser cutter,1200\nespresso machine,650\n"
        )
        _run_script(workspace, "index")
        result = _run_script(workspace, "search", ["espresso"])
        assert "spend.csv" in result.stdout
        assert "rows" in result.stdout

    def test_xlsx_indexed_via_shared_strings(self, workspace):
        _write_xlsx(workspace / "documents" / "budget.xlsx")
        result = _run_script(workspace, "index")
        assert result.returncode == 0, result.stderr

        result = _run_script(workspace, "search", ["projector"])
        assert "budget.xlsx" in result.stdout
        assert "Budget" in result.stdout

    def test_pdf_flate_stream_indexed(self, workspace):
        _write_pdf(workspace / "documents" / "story.pdf")
        result = _run_script(workspace, "index")
        assert result.returncode == 0, result.stderr

        result = _run_script(workspace, "search", ["kraken", "harbour"])
        assert "story.pdf" in result.stdout

    def test_unchanged_files_not_reindexed(self, workspace):
        (workspace / "documents" / "a.txt").write_text("alpha beta gamma")
        _run_script(workspace, "index")
        result = _run_script(workspace, "index")
        assert "0 file(s)" in result.stdout
        assert "1 unchanged" in result.stdout

    def test_changed_file_reindexed(self, workspace):
        doc = workspace / "documents" / "a.txt"
        doc.write_text("old content here")
        _run_script(workspace, "index")
        doc.write_text("new content entirely different")
        result = _run_script(workspace, "index")
        assert "indexed 1 file(s)" in result.stdout

        result = _run_script(workspace, "search", ["entirely"])
        assert "a.txt" in result.stdout
        result = _run_script(workspace, "search", ["old"])
        assert "No matches" in result.stdout

    def test_deleted_file_pruned(self, workspace):
        doc = workspace / "documents" / "gone.txt"
        doc.write_text("ephemeral text")
        _run_script(workspace, "index")
        doc.unlink()
        result = _run_script(workspace, "index")
        assert "1 removed" in result.stdout

        result = _run_script(workspace, "search", ["ephemeral"])
        assert "No matches" in result.stdout

    def test_unsupported_extension_ignored(self, workspace):
        (workspace / "documents" / "binary.bin").write_bytes(b"\x00\x01")
        result = _run_script(workspace, "index")
        assert result.returncode == 0
        assert "indexed 0 file(s)" in result.stdout

    def test_search_before_index_errors(self, workspace):
        result = _run_script(workspace, "search", ["anything"])
        assert result.returncode == 1
        assert "No index yet" in result.stderr

    def test_fts_syntax_in_query_is_inert(self, workspace):
        (workspace / "documents" / "a.txt").write_text("plain words only")
        _run_script(workspace, "index")
        result = _run_script(workspace, "search", ['plain" OR source_file:"x'])
        assert result.returncode == 0

    def test_creates_documents_dir(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        result = _run_script(ws, "index")
        assert result.returncode == 0
        assert (ws / "documents").is_dir()


class TestChunking:
    def test_long_text_split_into_parts(self, workspace):
        paragraphs = "\n\n".join(
            f"Paragraph {i} about the migration of arctic terns." for i in range(80)
        )
        (workspace / "documents" / "long.md").write_text(paragraphs)
        result = _run_script(workspace, "index")
        assert result.returncode == 0
        import re
        match = re.search(r"\((\d+) chunks\)", result.stdout)
        assert match is not None, result.stdout
        assert int(match.group(1)) > 1
