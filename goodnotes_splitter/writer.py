"""Writer: rebuild index protobufs and repackage a page subset.

Strategy (per the approved plan "identify -> select -> copy -> repackage"):
notes/*, attachments/* and search/* payloads are copied byte-for-byte.  Only
the index protobufs are rebuilt:

* index.notes.pb       -- subset of original records, ordered by page number
* index.attachments.pb -- subset for the attachments used by the subset
* index.search.pb      -- subset for search blobs that belong to the subset
* index.events.pb      -- the original event journal filtered down to records
                          whose subject belongs to the subset, plus the
                          document-scoped records (doc-created etc.).  Page
                          order in GoodNotes is the ORDER OF THE F54
                          page-created records, so keeping the original
                          journal order preserves relative page order with no
                          key patching.

Every new part gets a fresh random document UUID.  The old document UUID only
appears inside index.events.pb (verified: 0 occurrences in notes/search/
attachment payloads), so a byte-for-byte 36-char ASCII swap is safe for all
kept event records.

Note: the doc-created (F30) record embeds a device UUID (in this file
``5A53E89E-...``) for its "first page" reference, not a page UUID -- it never
references an actual page, so no first-page patching is needed.
"""

from __future__ import annotations

import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from typing import Iterable, Optional

from .models import Document, Page
from .protobuf import (
    decode_message,
    encode_message,
    join_delimited,
    write_len_delimited,
)

# Event action fields that describe reorder/delete history.  For a freshly
# repackaged page subset these are neither meaningful nor safe (the 16 F56
# "page deleted" records in the source even reference pages that are alive),
# so they are always dropped.
DROP_ACTIONS = frozenset({55, 56, 57})

_UUID_RE = re.compile(
    rb"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
    re.IGNORECASE,
)


def subject_of(rec: bytes) -> Optional[str]:
    """Return the first UUID text found in a raw event record.

    Every event record starts with field 1 holding the subject UUID as the
    first length-delimited string, so the first UUID in the raw bytes is the
    subject.  (For doc-scoped records such as F30 the subject is nested
    inside an "id" submessage, which this byte-level scan still locates
    first.)
    """
    for m in _UUID_RE.finditer(rec):
        try:
            s = m.group(0).decode("ascii")
        except Exception:
            continue
        return s.upper()
    return None


@dataclass
class Subset:
    """The entity sets needed to repackage a group of pages."""

    pages: list[Page]
    content: set[str]
    entities: set[str]
    papers: set[str]
    attachments: set[str]
    searches: set[str]


def select_subset(doc: Document, page_numbers: Iterable[int]) -> Subset:
    """Collect every entity a page subset depends on (paper, paper
    attachment, attachments implied by note bytes, search blobs)."""
    wanted = {int(p) for p in page_numbers}
    pages = [p for p in doc.pages if p.page_number in wanted and p.note_entry]
    pages.sort(key=lambda p: p.page_number)
    if not pages:
        raise ValueError("subset is empty")

    content = {p.content_uuid.upper() for p in pages if p.content_uuid}
    entities = {p.entity_uuid.upper() for p in pages if p.entity_uuid}
    papers = {p.paper_uuid.upper() for p in pages if p.paper_uuid}

    attachments: set[str] = set()
    for p in pages:
        for a in list(p.paper_attachments) + list(p.note_implied_attachments):
            if a:
                attachments.add(a.upper())

    search_lookup = {k.upper(): v for k, v in doc.search_files.items()}
    searches: set[str] = set()
    for p in pages:
        if p.search_uuid and p.search_uuid.upper() in search_lookup:
            searches.add(p.search_uuid.upper())
    for a in attachments:
        if a in search_lookup:
            searches.add(a)

    return Subset(
        pages=pages,
        content=content,
        entities=entities,
        papers=papers,
        attachments=attachments,
        searches=searches,
    )


def kept_record_count(doc: Document, subset: Subset) -> int:
    """Number of event records that survive filtering (subject-based)."""
    old_doc = (doc.document_uuid or "").upper()
    universe = subset.content | subset.entities | subset.papers | subset.attachments
    count = 0
    for ev in doc.events:
        if DROP_ACTIONS & ev.top_fields:
            continue
        subj = subject_of(ev.raw)
        if subj == old_doc or (subj and subj in universe):
            count += 1
    return count


ORDER_KEY_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def make_order_key(local_index: int) -> str:
    """Fresh lexicographically-increasing order key for a page.

    GoodNotes derives a document's page order at import time by sorting the
    F54 "page-created" records' order keys (field 4.1) lexicographically.
    We generate keys that are strictly increasing across the subset with a
    clean ASCII prefix (e.g. a0001, a0002...) matching real-world keys and
    verified on physical devices.
    """
    return f"a{int(local_index):04d}"


def _rewrite_f54_order_key(rec: bytes, new_key: str) -> bytes:
    """Replace the F54 order key (field 4.1 string) keeping all else verbatim."""
    key = new_key.encode("ascii")
    top = decode_message(rec)
    rebuilt_top = []
    for f in top:
        if f.number != 54:
            rebuilt_top.append(f.raw)
            continue
        body = decode_message(f.value)
        rebuilt_body = []
        for bf in body:
            if bf.number != 4:
                rebuilt_body.append(bf.raw)
                continue
            inner = decode_message(bf.value)
            rebuilt_inner = b"".join(
                encode_message([write_len_delimited(1, key)])
                if x.number == 1
                else x.raw
                for x in inner
            )
            rebuilt_body.append(write_len_delimited(4, rebuilt_inner))
        rebuilt_top.append(write_len_delimited(54, b"".join(rebuilt_body)))
    return encode_message(rebuilt_top)


def _expected_order_key(subset: Subset, entity_uuid: str) -> str | None:
    for i, p in enumerate(subset.pages):
        if p.entity_uuid and p.entity_uuid.upper() == entity_uuid.upper():
            return make_order_key(i + 1)
    return None


def rebuild_events(
    doc: Document,
    subset: Subset,
    new_doc_uuid: Optional[str] = None,
) -> tuple[bytes, int]:
    """Rebuild index.events.pb for a page subset.

    Returns ``(encoded_record_stream, record_count)``.  Records are kept in
    original journal order (which is also page order for the subset).  Three
    content rewrites are applied:

    * byte-for-byte document UUID swap;
    * doc-scoped records (F10) whose first-page entity reference points at
      an excluded page get that reference rewritten to the subset's first
      page;
    * every kept F54 "page-created" order key is regenerated to be strictly
      increasing across the subset, because GoodNotes sorts pages by these
      keys (not by ``index.notes.pb`` order) when importing a file.

    The F30 record's "first page" reference is a device UUID (never a real
    page), so it is left untouched.
    """
    old_doc = (doc.document_uuid or "").upper()
    if not new_doc_uuid:
        new_doc_uuid = str(uuid.uuid4()).upper()
    new_doc = new_doc_uuid.upper()

    universe = subset.content | subset.entities | subset.papers | subset.attachments

    # Doc-scoped records (session/created/open) embed a reference to the
    # document's first page entity.  When that page is not part of this
    # subset, rewrite the reference to the subset's first page so nothing
    # dangles.  (F30's own "first page" slot holds a device UUID, so the
    # replacement is simply a no-op there.)
    orig_first_entity = (doc.pages[0].entity_uuid or "").upper()
    first_entity = (
        (subset.pages[0].entity_uuid or "").upper() if subset.pages else orig_first_entity
    )
    patch_first = bool(orig_first_entity and first_entity and orig_first_entity != first_entity)

    kept: list[bytes] = []
    for ev in doc.events:
        if DROP_ACTIONS & ev.top_fields:
            continue
        subj = subject_of(ev.raw)
        if subj != old_doc and (not subj or subj not in universe):
            continue

        rec = ev.raw
        if old_doc and new_doc != old_doc:
            rec = rec.replace(old_doc.encode("ascii"), new_doc.encode("ascii"))
        if patch_first and ev.top_fields - {1} <= {10, 30, 31, 60}:
            rec = rec.replace(orig_first_entity.encode("ascii"), first_entity.encode("ascii"))
        if 54 in ev.top_fields and subj:
            key = _expected_order_key(subset, subj)
            if key:
                rec = _rewrite_f54_order_key(rec, key)
        kept.append(rec)

    return join_delimited(kept), len(kept)


def _payload_entries(doc: Document, subset: Subset, zsrc: zipfile.ZipFile):
    """Yield (name, data) for every copied payload entry of a subset."""
    for p in subset.pages:
        name = f"notes/{p.content_uuid}"
        if name not in doc.all_entries:
            raise KeyError(f"source entry missing: {name}")
        yield name, zsrc.read(name)

    for a_uuid in subset.attachments:
        att = doc.attachments.get(a_uuid)
        if att and att.entry:
            yield att.entry.filename, zsrc.read(att.entry.filename)

    search_lookup = {v.filename.upper(): v for v in doc.search_files.values()}
    for s_uuid in subset.searches:
        ref = doc.search_files.get(s_uuid)
        if ref is None:
            for k, v in doc.search_files.items():
                if k.upper() == s_uuid:
                    ref = v
                    break
        if ref:
            yield ref.filename, zsrc.read(ref.filename)


def write_part(
    doc: Document,
    subset: Subset,
    new_doc_uuid: str,
    out_path: str,
) -> tuple[int, int, dict[str, int]]:
    """Write one .goodnotes part.

    Returns ``(part_size_bytes, page_count, entry_name->len map)``.
    """
    events_bytes, event_count = rebuild_events(doc, subset, new_doc_uuid)

    note_idx_recs = [
        doc.notes_index_raw[p.content_uuid]
        for p in subset.pages
        if p.content_uuid in doc.notes_index_raw
    ]
    att_idx_recs = [
        doc.attachments_index_raw[a]
        for a in subset.attachments
        if a in doc.attachments_index_raw
    ]
    search_idx_recs = [
        doc.search_index_raw[u] for u in subset.searches if u in doc.search_index_raw
    ]

    # index.events.pb is a pure record stream: field1 then one action field.
    entries: dict[str, bytes] = {
        "schema.pb": doc.schema_data,
        "thumbnail.jpg": doc.thumbnail_data,
        "document.info.pb": doc.document_info,
        "index.notes.pb": join_delimited(note_idx_recs),
        "index.attachments.pb": join_delimited(att_idx_recs),
        "index.search.pb": join_delimited(search_idx_recs),
        "index.events.pb": events_bytes,
    }

    payloads: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(doc.source_path, "r") as zsrc:
        payloads = list(_payload_entries(doc, subset, zsrc))

    with zipfile.ZipFile(
        out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True
    ) as z:
        for name, data in payloads:
            z.writestr(name, data)
        for name, data in entries.items():
            z.writestr(name, data)

    size_map: dict[str, int] = {name: len(data) for name, data in payloads}
    size_map.update({name: len(data) for name, data in entries.items()})
    return os.path.getsize(out_path), len(subset.pages), size_map


def estimate_part_size(doc: Document, subset: Subset) -> int:
    """Rough compressed-size estimate for a candidate subset.

    Uses the compressed sizes of the payload entries (which dominate) plus a
    per-record average for the rebuilt events and a per-entry ZIP overhead.
    The estimator prefers over-estimation so real parts stay under the hard
    limit.
    """
    per_entry = 160  # local header + central-directory bytes, approximate
    total = 0

    events_compress = doc.all_entries.get("index.events.pb")
    events_compressed = events_compress.compress_size if events_compress else 0

    for p in subset.pages:
        if p.note_entry:
            total += p.note_entry.compress_size + per_entry
    for a_uuid in subset.attachments:
        att = doc.attachments.get(a_uuid)
        if att and att.entry:
            total += att.entry.compress_size + per_entry
    for s_uuid in subset.searches:
        ref = doc.search_files.get(s_uuid)
        if ref is None:
            for k, v in doc.search_files.items():
                if k.upper() == s_uuid:
                    ref = v
                    break
        if ref:
            total += ref.compress_size + per_entry

    total += len(doc.schema_data) + len(doc.thumbnail_data) + len(doc.document_info)
    total += per_entry * 3  # schema, thumbnail, document.info, + events below

    if doc.events:
        kept = max(1, kept_record_count(doc, subset))
        total += int(events_compressed * kept / len(doc.events)) + per_entry

    total += len(subset.pages) * 82
    total += len(subset.attachments) * 88
    total += len(subset.searches) * 83
    return total