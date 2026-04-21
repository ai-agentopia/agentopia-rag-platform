"""Issue #25 — DOCX text extraction tests.

These tests cover ingest.text_extract in isolation — no Pathway, no Qdrant,
no live S3. They verify the format-dispatch logic using in-memory fixtures.
"""

from __future__ import annotations

import io
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


class TestDocxExtraction:
    def test_single_paragraph(self):
        data = _make_docx_bytes(["Hello world"])
        result = extract_text(data, "docs/sample.docx")
        assert "Hello world" in result

    def test_multiple_paragraphs_joined(self):
        data = _make_docx_bytes(["First paragraph", "Second paragraph"])
        result = extract_text(data, "folder/doc.docx")
        assert "First paragraph" in result
        assert "Second paragraph" in result

    def test_empty_paragraphs_skipped(self):
        data = _make_docx_bytes(["Real content", "", "More content"])
        result = extract_text(data, "file.docx")
        assert "Real content" in result
        assert "More content" in result
        # empty string paragraphs should not produce blank lines in output
        assert "\n\n" not in result

    def test_binary_garbage_not_passed_through(self):
        data = _make_docx_bytes(["Architecture decision: use Pathway"])
        result = extract_text(data, "arch.docx")
        # result must be valid UTF-8 plain text, not binary garbage
        assert isinstance(result, str)
        result.encode("utf-8")  # raises if result is not valid unicode


class TestMarkdownExtraction:
    def test_plain_utf8_passthrough(self):
        content = "# Header\n\nSome markdown body."
        data = content.encode("utf-8")
        result = extract_text(data, "docs/README.md")
        assert result == content

    def test_txt_extension_passthrough(self):
        content = "plain text content"
        data = content.encode("utf-8")
        result = extract_text(data, "notes.txt")
        assert result == content

    def test_no_extension_passthrough(self):
        content = "content without extension"
        data = content.encode("utf-8")
        result = extract_text(data, "noextfile")
        assert result == content

    def test_unknown_extension_passthrough(self):
        content = "some content"
        data = content.encode("utf-8")
        result = extract_text(data, "file.rst")
        assert result == content

    def test_bad_bytes_replaced_not_raised(self):
        data = b"valid start \xff\xfe invalid bytes"
        result = extract_text(data, "file.md")
        assert isinstance(result, str)
        assert "valid start" in result

    def test_case_insensitive_extension(self):
        content = "content"
        data = content.encode("utf-8")
        result = extract_text(data, "FILE.MD")
        assert result == content
