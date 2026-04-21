"""Official Pathway-native parser spike — validates the documented flow:

  connector(format="binary") → UnstructuredParser → flatten → sink

This spike uses `pw.io.fs.read(format="binary")` rather than `pw.io.s3.read`
because the official docstring explicitly states the two share the same
`data: bytes` column contract for `format="binary"` — the parser boundary
is what this spike validates, not the S3 network path.

NO custom extract_text. NO plaintext_by_object. NO per-extension dispatch.
One parser, one flow, applied uniformly across DOCX/PDF/HTML/TXT/MD.

Reference: python/pathway/xpacks/llm/parsers.py (v0.30.0) + LLM xpack overview.
"""
import json
import sys
from pathlib import Path

import pathway as pw
from pathway.xpacks.llm.parsers import UnstructuredParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Step 1 — connector boundary: read every file as raw bytes
# Output schema (per v0.30.0 s3/__init__.py and fs/__init__.py docstring):
#   data: bytes, _metadata: json(path, modified_at, ...)
files = pw.io.fs.read(
    str(FIXTURE_DIR),
    mode="static",
    format="binary",
    with_metadata=True,
)

# Step 2 — official parser boundary
# UnstructuredParser(chunking_mode="elements") takes a bytes column,
# returns list[tuple[str, dict]] per row. Delegates to unstructured.partition.auto.
parser = UnstructuredParser(chunking_mode="elements")

parsed = files.select(
    path=pw.this._metadata["path"],
    elements=parser(pw.this.data),
)

# Step 3 — flatten list → one row per chunk
# This is the pattern shown in the LLM xpack overview.
flat = parsed.flatten(pw.this.elements).select(
    path=pw.this.path,
    chunk=pw.this.elements,
)


# Step 4 — sink. Use a python callback sink so we can capture the parsed
# output deterministically. (csv/json connectors don't handle tuples well.)
_rows: list[dict] = []


class CollectorSink(pw.io.python.ConnectorSubject):
    def run(self):  # pragma: no cover
        pass


def _on_change(key, row, time, is_addition):  # noqa: ARG001
    if is_addition:
        _rows.append({
            "path": str(row["path"]),
            "chunk": row["chunk"],
        })


pw.io.subscribe(flat, on_change=_on_change)

if __name__ == "__main__":
    # Static mode: pw.run() processes all files then stops.
    pw.run(monitoring_level=pw.MonitoringLevel.NONE)

    # Write results for inspection
    out_path = OUTPUT_DIR / "parsed_chunks.json"
    # _rows[i]["chunk"] is a tuple(text, metadata_dict) — normalize for JSON
    normalized = []
    for r in _rows:
        chunk = r["chunk"]
        if isinstance(chunk, (list, tuple)) and len(chunk) == 2:
            text, meta = chunk
        else:
            text, meta = str(chunk), {}
        normalized.append({
            "path": r["path"],
            "text": text,
            "metadata": meta,
        })
    out_path.write_text(json.dumps(normalized, indent=2, default=str))
    print(f"Wrote {len(normalized)} chunk(s) to {out_path}")

    # Per-file summary
    by_path: dict[str, list[str]] = {}
    for r in normalized:
        by_path.setdefault(r["path"], []).append(r["text"])

    print("\nPer-file chunk summary:")
    for path, chunks in sorted(by_path.items()):
        name = Path(path).name
        total_chars = sum(len(c) for c in chunks)
        preview = (chunks[0][:80] + "...") if chunks and chunks[0] else "(empty)"
        print(f"  {name:12s}  chunks={len(chunks):3d}  total_chars={total_chars:5d}  first={preview!r}")
