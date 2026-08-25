"""Text extraction for document-search. Stdlib only.

extract_text(path) returns a list of (locator, text) segments, where locator
describes where in the document the text came from ("page 2", "Sheet1 rows
2-51", "" for whole-file formats).
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
_CSV_BATCH_ROWS = 50

SUPPORTED_EXTENSIONS = {".md", ".txt", ".csv", ".xlsx", ".pdf"}


def extract_text(path: Path) -> list[tuple[str, str]]:
    """Extract text segments from a supported document. Raises ValueError on
    unsupported or unreadable files."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")

    ext = path.suffix.lower()
    if ext in (".md", ".txt"):
        return [("", _cap(path.read_text(encoding="utf-8", errors="replace")))]
    if ext == ".csv":
        return _extract_csv(path)
    if ext == ".xlsx":
        return _extract_xlsx(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    raise ValueError(f"unsupported extension: {ext}")


def _cap(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return text
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


def _extract_csv(path: Path) -> list[tuple[str, str]]:
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header = ", ".join(rows[0])
    segments: list[tuple[str, str]] = []
    for start in range(1, len(rows), _CSV_BATCH_ROWS):
        batch = rows[start:start + _CSV_BATCH_ROWS]
        lines = [header] + [", ".join(r) for r in batch]
        locator = f"rows {start + 1}-{start + len(batch)}"
        segments.append((locator, _cap("\n".join(lines))))
    return segments or [("", header)]


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    strings: list[str] = []
    root = ElementTree.fromstring(data)
    for si in root.iter(f"{_XLSX_NS}si"):
        strings.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
    return strings


def _xlsx_sheet_names(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return (sheet display name, archive path) pairs in workbook order."""
    names: list[tuple[str, str]] = []
    try:
        workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    except (KeyError, ElementTree.ParseError):
        workbook = None
    sheet_files = sorted(
        n for n in zf.namelist()
        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
    )
    display: list[str] = []
    if workbook is not None:
        display = [
            s.get("name", "") for s in workbook.iter(f"{_XLSX_NS}sheet")
        ]
    for i, archive_path in enumerate(sheet_files):
        name = display[i] if i < len(display) else Path(archive_path).stem
        names.append((name, archive_path))
    return names


def _extract_xlsx(path: Path) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    with zipfile.ZipFile(path) as zf:
        shared = _xlsx_shared_strings(zf)
        for sheet_name, archive_path in _xlsx_sheet_names(zf):
            try:
                root = ElementTree.fromstring(zf.read(archive_path))
            except (KeyError, ElementTree.ParseError):
                continue
            rows: list[str] = []
            for row in root.iter(f"{_XLSX_NS}row"):
                cells: list[str] = []
                for cell in row.iter(f"{_XLSX_NS}c"):
                    v = cell.find(f"{_XLSX_NS}v")
                    if v is None or v.text is None:
                        is_node = cell.find(f"{_XLSX_NS}is")
                        if is_node is not None:
                            cells.append("".join(
                                t.text or "" for t in is_node.iter(f"{_XLSX_NS}t")
                            ))
                        continue
                    if cell.get("t") == "s":
                        try:
                            cells.append(shared[int(v.text)])
                        except (ValueError, IndexError):
                            cells.append(v.text)
                    else:
                        cells.append(v.text)
                if cells:
                    rows.append(", ".join(cells))
            for start in range(0, len(rows), _CSV_BATCH_ROWS):
                batch = rows[start:start + _CSV_BATCH_ROWS]
                locator = f"{sheet_name} rows {start + 1}-{start + len(batch)}"
                segments.append((locator, _cap("\n".join(batch))))
    return segments


_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_PDF_TEXT_OP_RE = re.compile(
    rb"\((?P<lit>(?:[^()\\]|\\.)*)\)\s*Tj"
    rb"|\[(?P<arr>(?:[^\[\]\\]|\\.)*)\]\s*TJ",
    re.DOTALL,
)
_PDF_ARRAY_STRING_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)", re.DOTALL)
_PDF_ESCAPES = {
    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
    b"(": b"(", b")": b")", b"\\": b"\\",
}


def _decode_pdf_string(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i:i + 1]
        if ch == b"\\" and i + 1 < len(raw):
            nxt = raw[i + 1:i + 2]
            if nxt in _PDF_ESCAPES:
                out += _PDF_ESCAPES[nxt]
                i += 2
                continue
            if nxt.isdigit():
                octal = raw[i + 1:i + 4]
                digits = bytes(c for c in octal if chr(c).isdigit())
                if digits:
                    try:
                        out.append(int(digits, 8) % 256)
                    except ValueError:
                        pass
                    i += 1 + len(digits)
                    continue
            i += 2
            continue
        out += ch
        i += 1
    return out.decode("latin-1", errors="replace")


def _extract_pdf(path: Path) -> list[tuple[str, str]]:
    """Best-effort text extraction: decompress streams, pull Tj/TJ operator
    strings. Handles plain and FlateDecode content streams; CID-encoded fonts
    and encrypted PDFs produce little or nothing."""
    data = path.read_bytes()
    pieces: list[str] = []
    for match in _PDF_STREAM_RE.finditer(data):
        stream = match.group(1)
        try:
            stream = zlib.decompress(stream)
        except zlib.error:
            pass
        for op in _PDF_TEXT_OP_RE.finditer(stream):
            lit = op.group("lit")
            if lit is not None:
                pieces.append(_decode_pdf_string(lit))
                continue
            arr = op.group("arr")
            if arr is not None:
                for s in _PDF_ARRAY_STRING_RE.finditer(arr):
                    pieces.append(_decode_pdf_string(s.group(0)[1:-1]))
    text = "".join(pieces)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    return [("", _cap(text))]
