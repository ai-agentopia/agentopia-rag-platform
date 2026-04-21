"""Format-aware text extraction for S3-ingested objects.

Dispatches by file extension so Pathway can read all objects as binary
and this module handles format-specific parsing. Keeping extraction
separate from the Pathway graph means it can be tested without pathway.

Supported:
  .docx — python-docx paragraph extraction
  all other extensions — UTF-8 decode (replacement chars on bad bytes)
"""

from __future__ import annotations

import io


def extract_text(data: bytes, path: str) -> str:
    """Return plain text for a binary S3 object, dispatched by extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == "docx":
        import docx  # python-docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return data.decode("utf-8", errors="replace")
