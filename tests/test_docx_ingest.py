"""Issue #25 — DOCX text extraction tests.

Covers ingest.text_extract in isolation — no Pathway, no Qdrant, no live S3.

Includes regression tests for the str-input crash introduced by PR #62:
Pathway delivers UDF arguments as str (not bytes) when replaying from state
or when format="binary" is used. The original code called str.decode() which
raises AttributeError for every file in the watched prefix.
"""

from __future__ import annotations

import io
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingest.text_extract import extract_text  # noqa: E402


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    """Build a minimal DOCX in memory using python-docx."""
    import docx

    doc = docx.Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Regression tests — the exact failure that hit live (PR #62 bug)
# ---------------------------------------------------------------------------

class TestStrInputRegression:
    """Pathway delivers str, not bytes. These are the live-failure scenarios."""

    def test_markdown_str_does_not_crash(self):
        # This exact case crashed the live pipeline
        data = "# Architecture Decision\n\nUse Pathway for streaming ingest."
        result = extract_text(data, "architecture/decision.md")
        assert isinstance(result, str)
        assert "Architecture Decision" in result

    def test_plaintext_str_does_not_crash(self):
        data = "plain text content from S3"
        result = extract_text(data, "docs/notes.txt")
        assert result == "plain text content from S3"

    def test_str_no_extension_does_not_crash(self):
        data = "content without extension"
        result = extract_text(data, "noextfile")
        assert result == "content without extension"

    def test_str_unknown_extension_does_not_crash(self):
        data = "reStructuredText content"
        result = extract_text(data, "README.rst")
        assert result == "reStructuredText content"

    def test_str_returned_verbatim_no_transformation(self):
        # Pathway already decoded the content — return it as-is, no double-decode
        data = "exact content with unicode: café, naïve, résumé"
        result = extract_text(data, "docs/unicode.md")
        assert result == data

    def test_docx_str_does_not_crash_pipeline(self, caplog):
        # If a DOCX arrives as str (unknown encoding), pipeline must not crash.
        # Expected: warning logged, empty string returned.
        with caplog.at_level(logging.WARNING, logger="text_extract"):
            result = extract_text("not real docx bytes as str", "file.docx")
        assert result == ""
        assert "docx extraction skipped" in caplog.text
        assert "str" in caplog.text


# ---------------------------------------------------------------------------
# Bytes input — existing correct behavior must not regress
# ---------------------------------------------------------------------------

class TestBytesInput:
    def test_markdown_bytes_decoded(self):
        data = "# Header\n\nSome markdown body.".encode("utf-8")
        result = extract_text(data, "docs/README.md")
        assert result == "# Header\n\nSome markdown body."

    def test_bytes_bad_encoding_replaced_not_raised(self):
        data = b"valid start \xff\xfe invalid bytes"
        result = extract_text(data, "file.md")
        assert isinstance(result, str)
        assert "valid start" in result

    def test_docx_bytes_extracts_paragraphs(self):
        data = _make_docx_bytes(["Hello world", "Second paragraph"])
        result = extract_text(data, "docs/sample.docx")
        assert "Hello world" in result
        assert "Second paragraph" in result

    def test_docx_bytes_skips_empty_paragraphs(self):
        data = _make_docx_bytes(["Real content", "", "More content"])
        result = extract_text(data, "file.docx")
        assert "Real content" in result
        assert "More content" in result
        assert "\n\n" not in result

    def test_docx_bytes_readable_text_not_binary(self):
        data = _make_docx_bytes(["Architecture decision: use Pathway"])
        result = extract_text(data, "arch.docx")
        assert isinstance(result, str)
        result.encode("utf-8")  # raises if not valid unicode

    def test_case_insensitive_extension(self):
        data = "content".encode("utf-8")
        result = extract_text(data, "FILE.MD")
        assert result == "content"
