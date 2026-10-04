"""Command-line entry point for the GoodNotes splitter.

Usage:
    python -m goodnotes_splitter [INPUT.goodnotes] -o OUTDIR

With no INPUT the lone ``*.goodnotes`` file in the current directory is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from .models import Document
from .partitioner import format_plan, partition_pages
from .reader import GoodNotesReader
from .validator import render, validate_parts
from .writer import rebuild_events, select_subset, write_part


def _find_source() -> str:
    candidates = [f for f in os.listdir(".") if f.lower().endswith(".goodnotes")]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit("no .goodnotes file in the current directory")
    raise SystemExit(
        "multiple .goodnotes files found; pass one explicitly:\n  "
        + "\n  ".join(candidates)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="goodnotes_splitter", description=__doc__)
    ap.add_argument("input", nargs="?", default=None, help="source .goodnotes file")
    ap.add_argument("-o", "--outdir", default="output", help="output directory")
    ap.add_argument("--target-mb", type=float, default=4.5, help="target per part (MB)")
    ap.add_argument("--hard-mb", type=float, default=5.0, help="hard limit per part (MB)")
    ap.add_argument("--prefix", default="part", help="output file name prefix")
    ap.add_argument(
        "--keep-doc-uuid",
        action="store_true",
        help="keep the original document UUID (default: regenerate per part)",
    )
    args = ap.parse_args(argv)

    source = args.input or _find_source()
    if source.lower().endswith(".goodnotes") and os.path.sep not in source:
        pass

    print(f"[goodnotes_splitter] reading {source}")
    doc = GoodNotesReader(source).read()

    parts = partition_pages(doc, target_mb=args.target_mb, hard_mb=args.hard_mb)
    print(f"[goodnotes_splitter] partition plan:\n{format_plan(doc, parts)}")

    parts = [p for p in parts if p]
    os.makedirs(args.outdir, exist_ok=True)

    written: list[str] = []
    reports = []
    total_pages = 0
    for idx, page_nums in enumerate(parts, start=1):
        subset = select_subset(doc, page_nums)
        new_doc = doc.document_uuid if args.keep_doc_uuid else str(uuid.uuid4()).upper()
        out_path = os.path.join(args.outdir, f"{args.prefix}-{idx:03d}.goodnotes")
        size, page_count, _sizes = write_part(doc, subset, new_doc, out_path)
        written.append(out_path)
        total_pages += page_count
        print(
            f"  wrote {os.path.basename(out_path)}  ({size} bytes, "
            f"{page_count} pages, doc {new_doc[:8]}...)"
        )

    print(f"[goodnotes_splitter] validating {len(written)} part(s)...")
    reports = validate_parts(written, hard_mb=args.hard_mb, source=doc)
    print(render(reports))

    all_ok = all(r.ok for r in reports) and total_pages == doc.page_count
    print(
        f"[goodnotes_splitter] pages: source={doc.page_count} "
        f"sum(parts)={total_pages} match={total_pages == doc.page_count}"
    )
    if not all_ok:
        for r in reports:
            for p in r.problems():
                print(f"  FAIL {os.path.basename(r.path)}: {p}")
        return 1
    print("[goodnotes_splitter] DONE - all parts validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())