"""Format-aware text extraction for S3-ingested objects.

Dispatches by file extension. Designed to handle both str and bytes input
because Pathway's UDF layer delivers data as str (not bytes) when replaying
from state or when format="binary" is used with the current Pathway version.

Supported:
  .docx — python-docx paragraph extraction (bytes input only; str is warned)
  all other extensions — str returned directly, or bytes decoded as UTF-8
"""

from __future__ import annotations

import io
import logging

_log = logging.getLogger("text_extract")


def extract_text(data, path: str) -> str:
    """Return plain text for an S3 object, dispatched by file extension.

    Handles both str and bytes safely:
    - Non-DOCX str: return as-is (Pathway delivered already-decoded content)
    - Non-DOCX bytes: decode as UTF-8 with replacement chars
    - DOCX bytes: extract paragraph text via python-docx
    - DOCX str: log warning, return "" (bytes required; str recovery unproven)
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""

    if ext == "docx":
        if not isinstance(data, bytes):
            _log.warning(
                "docx extraction skipped for %s: expected bytes, got %s. "
                "Pipeline continues with empty content for this object.",
                path,
                type(data).__name__,
            )
            return ""
        import docx  # python-docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")
