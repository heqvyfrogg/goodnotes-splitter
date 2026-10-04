"""Partitioner: group the pages into subsets that fit under a size target.

The tool must never cut inside a page, a note, or an attachment -- every
output is a page-exact subset repackaged as its own document (see writer).
So partitioning is a greedy packing problem over whole pages.

The dominant cost is the compressed attachments (22.4 MB out of ~26 MB total
in the target file); notes and search blobs are much smaller.  Attachments
shared between pages are only counted once *within* a part (they ARE copied
into every part that references them, so overlap across parts re-counts the
same attachment -- that is intentional and correct for independent docs).
"""

from __future__ import annotations

import math
from typing import Optional

from .models import Document
from .writer import Subset, estimate_part_size, select_subset

_BYTE_IN_MB = 1024 * 1024
_SAFETY_MARGIN_RATIO = 1.05  # estimator headroom so parts stay under the hard cap


def _estimate(doc: Document, page_numbers: list[int]) -> tuple[Subset, int]:
    subset = select_subset(doc, page_numbers)
    if not subset.pages:
        raise ValueError("empty subset")
    return subset, estimate_part_size(doc, subset)


def partition_pages(
    doc: Document,
    target_mb: float = 4.5,
    hard_mb: Optional[float] = None,
    start: int = 1,
) -> list[list[int]]:
    """Greedy partition of ``doc.pages`` into page-number groups.

    Pages are packed in document order (so each output keeps a contiguous
    slice of the original pages).  A page group is sealed as soon as the
    estimated size would exceed ``target_mb``, with ``hard_mb`` as a sanity
    ceiling for the estimator (not the written file).
    """
    target = int(target_mb * _BYTE_IN_MB / _SAFETY_MARGIN_RATIO) if target_mb else math.inf
    if hard_mb:
        hard = int(hard_mb * _BYTE_IN_MB / _SAFETY_MARGIN_RATIO)
        target = min(target, hard)

    order = sorted({p.page_number for p in doc.pages})
    if order and order[0] != start:
        raise ValueError(f"page numbering does not start at {start}")

    parts: list[list[int]] = []
    current: list[int] = []

    for page_num in order:
        probe = current + [page_num]
        _subset, est = _estimate(doc, probe)
        if current and est > target:
            parts.append(current)
            current = [page_num]
        else:
            current = probe

    if current:
        parts.append(current)
    return parts


def format_plan(doc: Document, parts: list[list[int]]) -> str:
    """Human readable partition summary (page ranges, estimated sizes)."""
    lines: list[str] = []
    total_pages = sum(len(p) for p in parts)
    lines.append(f"parts: {len(parts)}, total pages: {total_pages}")
    for idx, nums in enumerate(parts, start=1):
        _, est = _estimate(doc, nums)
        rng = str(nums[0]) if nums[0] == nums[-1] else f"{nums[0]}-{nums[-1]}"
        lines.append(
            f"  part {idx:2d}: pages {rng:<12s} ({len(nums):2d} pages) "
            f"est {est / _BYTE_IN_MB:.2f} MB"
        )
    return "\n".join(lines)