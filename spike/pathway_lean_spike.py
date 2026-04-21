"""Lean Pathway parser spike — same binary→parser→flatten flow, but using
the minimum Pathway+parser deps (no docling, no torch, no onnx).

Dispatch:
  .pdf         → PypdfParser (pure pypdf, no ML models)
  everything   → UnstructuredParser (pure unstructured core, no inference)
"""
import json
from pathlib import Path

import pathway as pw
from pathway.xpacks.llm.parsers import UnstructuredParser, PypdfParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

files = pw.io.fs.read(
    str(FIXTURE_DIR),
    mode="static",
    format="binary",
    with_metadata=True,
)

unstructured_parser = UnstructuredParser(chunking_mode="elements")
pdf_parser = PypdfParser()

# Dispatch: PDF uses PypdfParser, everything else uses UnstructuredParser.
# Both parsers accept a bytes column and return list[tuple[str, dict]].
pdf_rows = files.filter(pw.this._metadata["path"].str.endswith(".pdf"))
other_rows = files.filter(~pw.this._metadata["path"].str.endswith(".pdf"))

parsed_pdf = pdf_rows.select(
    path=pw.this._metadata["path"],
    elements=pdf_parser(pw.this.data),
)
parsed_other = other_rows.select(
    path=pw.this._metadata["path"],
    elements=unstructured_parser(pw.this.data),
)

# Concatenate both streams and flatten to one chunk per row
parsed = parsed_pdf.concat_reindex(parsed_other)
flat = parsed.flatten(pw.this.elements).select(
    path=pw.this.path,
    chunk=pw.this.elements,
)

_rows: list[dict] = []

def _on_change(key, row, time, is_addition):  # noqa: ARG001
    if is_addition:
        _rows.append({"path": str(row["path"]), "chunk": row["chunk"]})

pw.io.subscribe(flat, on_change=_on_change)

if __name__ == "__main__":
    pw.run(monitoring_level=pw.MonitoringLevel.NONE)

    normalized = []
    for r in _rows:
        chunk = r["chunk"]
        if isinstance(chunk, (list, tuple)) and len(chunk) == 2:
            text, meta = chunk
        else:
            text, meta = str(chunk), {}
        normalized.append({"path": r["path"], "text": text, "metadata": meta})

    out = OUTPUT_DIR / "parsed_chunks_lean.json"
    out.write_text(json.dumps(normalized, indent=2, default=str))
    print(f"Wrote {len(normalized)} chunks → {out.name}")

    by_path: dict[str, list[str]] = {}
    for r in normalized:
        by_path.setdefault(r["path"], []).append(r["text"])
    print("\nPer-file:")
    for path, chunks in sorted(by_path.items()):
        name = Path(path.strip('"')).name
        print(f"  {name:12s} chunks={len(chunks):3d} first={(chunks[0][:60] if chunks else '')!r}")
