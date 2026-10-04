"""Reader/parser for GoodNotes .goodnotes archives.

The reader is intentionally raw-preserving: page notes, attachments, and
search blobs are exposed as ZIP entry references and are never decoded into
application-level objects.  Only the index protobufs are interpreted, and
for them we keep both the decoded fields and the raw record bytes so the
writer can faithfully rebuild a subset.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from collections import Counter, defaultdict
from typing import Any, Optional

from .models import Attachment, Document, EventRecord, Page, PaperDef, ZipEntryRef
from .protobuf import (
    decode_message,
    fields_map,
    field_text,
    join_delimited,
    looks_like_uuid,
    read_varint,
    split_delimited,
    write_len_delimited,
    write_varint_field,
)

# Event action field numbers observed in GoodNotes index.events.pb.
EVENT_ACTIONS = {
    2: "paper",
    6: "attachment",
    30: "document",
    54: "page_created",
    55: "page_reordered",
    56: "page_deleted",
    102: "view_state",
    104: "settings",
    105: "page_link",
    106: "stroke_version",
    160: "recording",
    163: "recording_deleted",
}

UUID_RE = re.compile(
    rb"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
    re.IGNORECASE,
)


def _safe_text(f) -> Optional[str]:
    return field_text(f)


class GoodNotesReader:
    """Parses a single .goodnotes file into a Document model."""

    def __init__(self, path: str):
        self.path = path

    def read(self) -> Document:
        doc = Document(source_path=self.path)
        with zipfile.ZipFile(self.path, "r") as z:
            self._index_entries(doc, z)
            self._read_schema(doc, z)
            self._read_metadata_files(doc, z)
            self._read_events(doc, z)
            self._read_notes_index(doc, z)
            self._read_attachments_index(doc, z)
            self._read_search_index(doc, z)
            self._resolve_pages(doc)
            self._resolve_paper_attachments(doc)
            self._resolve_attachment_usage(doc)
            self._scan_note_attachment_refs(doc)
            self._attach_search(doc)
        return doc

    # ------------------------------------------------------------------ ZIP
    def _index_entries(self, doc: Document, z: zipfile.ZipFile) -> None:
        for info in z.infolist():
            if info.is_dir():
                continue
            doc.all_entries[info.filename] = ZipEntryRef(
                filename=info.filename,
                compress_size=info.compress_size,
                file_size=info.file_size,
                crc=info.CRC,
                compress_type=info.compress_type,
            )

    def _raw(self, z: zipfile.ZipFile, name: str) -> bytes:
        return z.read(name)

    def _read_schema(self, doc: Document, z: zipfile.ZipFile) -> None:
        if "schema.pb" in doc.all_entries:
            data = self._raw(z, "schema.pb")
            doc.schema_data = data
            try:
                fields = fields_map(data)
                # schema.pb is a single varint field: tag then value.
                vals = [f.value for f in fields.get(1, []) if f.wire_type == 0]
                doc.schema_version = vals[0] if vals else None
            except Exception:
                doc.schema_version = None

    def _read_metadata_files(self, doc: Document, z: zipfile.ZipFile) -> None:
        if "thumbnail.jpg" in doc.all_entries:
            doc.thumbnail_data = self._raw(z, "thumbnail.jpg")
        if "document.info.pb" in doc.all_entries:
            doc.document_info = self._raw(z, "document.info.pb")

    # ------------------------------------------------------------- events.pb
    def _read_events(self, doc: Document, z: zipfile.ZipFile) -> None:
        if "index.events.pb" not in doc.all_entries:
            return
        doc.events_raw = self._raw(z, "index.events.pb")
        for rec in split_delimited(doc.events_raw):
            fields = decode_message(rec)
            top = frozenset(f.number for f in fields)
            doc.events.append(EventRecord(raw=rec, top_fields=top))

    # ---------------------------------------------------------- notes index
    def _read_notes_index(self, doc: Document, z: zipfile.ZipFile) -> None:
        if "index.notes.pb" not in doc.all_entries:
            return
        data = self._raw(z, "index.notes.pb")
        for rec in split_delimited(data):
            fields = fields_map(rec)
            uuid = self._first_text(fields, 1)
            path = self._first_text(fields, 2)
            if uuid and path:
                doc.notes_index_raw[uuid] = rec
                page = Page(content_uuid=uuid, page_number=len(doc.pages) + 1)
                page.note_entry = doc.all_entries.get(path)
                doc.pages.append(page)

    def _first_text(self, fm: dict, number: int) -> Optional[str]:
        for f in fm.get(number, []):
            s = _safe_text(f)
            if s:
                return s
        return None

    # ----------------------------------------------------- attachments index
    def _read_attachments_index(self, doc: Document, z: zipfile.ZipFile) -> None:
        if "index.attachments.pb" not in doc.all_entries:
            return
        data = self._raw(z, "index.attachments.pb")
        for rec in split_delimited(data):
            fields = fields_map(rec)
            uuid = self._first_text(fields, 1)
            path = self._first_text(fields, 2)
            if uuid and path:
                doc.attachments_index_raw[uuid] = rec
                doc.attachments[uuid] = Attachment(
                    uuid=uuid, path=path, entry=doc.all_entries.get(path)
                )

    # ---------------------------------------------------------- search index
    def _read_search_index(self, doc: Document, z: zipfile.ZipFile) -> None:
        if "index.search.pb" not in doc.all_entries:
            return
        data = self._raw(z, "index.search.pb")
        for rec in split_delimited(data):
            fields = fields_map(rec)
            uuid = self._first_text(fields, 1)
            path = self._first_text(fields, 2)
            if uuid and path:
                doc.search_index_raw[uuid] = rec
                if path in doc.all_entries:
                    doc.search_files[uuid] = doc.all_entries[path]

    # ------------------------------------------------------------ resolution
    def _resolve_pages(self, doc: Document) -> None:
        """Fill per-page entity UUID, order key, paper reference from events."""
        depths = Counter()
        deleted_entities: set[str] = set()
        for i, ev in enumerate(doc.events):
            fields = fields_map(ev.raw)
            # page-created (field 54)
            for f in fields.get(54, []):
                depths["page_created"] += 1
                body = fields_map(f.value)
                entity = self._first_text(body, 2)
                doc_uuid = self._first_text(body, 1)
                if doc_uuid:
                    doc.document_uuid = doc_uuid
                paper_ref = None
                order_key = None
                for s in body.get(3, []):
                    sm = fields_map(s.value)
                    paper_ref = paper_ref or self._first_text(sm, 1)
                for s in body.get(4, []):
                    sm = fields_map(s.value)
                    order_key = order_key or self._first_text(sm, 1)
                page = self._find_page_by_entity(doc, entity)
                if page:
                    page.entity_uuid = entity
                    page.order_key = order_key
                    page.paper_uuid = paper_ref
                    page.owning_events.append(ev.raw)
                    self._record_referenced(ev, page, doc)
                else:
                    print(f"  [warn] page-created entity {entity} has no matching page")

            # page-moved (field 55)
            for f in fields.get(55, []):
                depths["page_moved"] += 1
                body = fields_map(f.value)
                entity = self._first_text(body, 2)
                order_key = None
                for s in body.get(3, []):
                    sm = fields_map(s.value)
                    order_key = order_key or self._first_text(sm, 1)
                page = self._find_page_by_entity(doc, entity)
                if page and order_key:
                    page.order_key = order_key
                    page.owning_events.append(ev.raw)
                    self._record_referenced(ev, page, doc)

            # page-deleted (field 56)
            for f in fields.get(56, []):
                depths["page_deleted"] += 1
                body = fields_map(f.value)
                entity = self._first_text(body, 2)
                if entity:
                    deleted_entities.add(entity.upper())

            # event record subject field 1 is an entity uuid; link pages by
            # matching the record that pairs with a page.
            # page-link (field 105)
            for f in fields.get(105, []):
                depths["page_link"] += 1
                body = fields_map(f.value)
                linked = self._first_text(body, 3)
                if linked:
                    page = self._find_page_by_content(doc, linked)
                    if page:
                        page.owning_events.append(ev.raw)
                        self._record_referenced(ev, page, doc)
            for f in fields.get(106, []):
                depths["stroke_version"] += 1
                body = fields_map(f.value)
                linked = self._first_text(body, 1)
                page = self._find_page_by_content(doc, linked) if linked else None
                if page:
                    page.owning_events.append(ev.raw)
                    self._record_referenced(ev, page, doc)

        # paper definitions
        for i, ev in enumerate(doc.events):
            fields = fields_map(ev.raw)
            for f in fields.get(2, []):
                body = fields_map(f.value)
                paper_uuid = self._first_text(body, 2)
                att_uuid = self._first_text(body, 4)
                name = self._first_text(body, 9)
                dims = None
                for d in body.get(8, []):
                    dm = fields_map(d.value)
                    dims = (self._first_float(dm, 1), self._first_float(dm, 2))
                if paper_uuid:
                    pd = doc.papers.setdefault(
                        paper_uuid,
                        PaperDef(paper_uuid=paper_uuid, raw_record=ev.raw),
                    )
                    pd.attachment_uuid = att_uuid or pd.attachment_uuid
                    pd.name = name or pd.name
                    pd.width = dims[0] if dims else pd.width
                    pd.height = dims[1] if dims else pd.height

        # Filter out deleted pages and sort active pages by device order_key
        doc.pages = [
            p for p in doc.pages
            if (p.entity_uuid or "").upper() not in deleted_entities
        ]
        doc.pages.sort(key=lambda p: p.order_key or "")
        for idx, p in enumerate(doc.pages, start=1):
            p.page_number = idx

        doc.events_aux = {
            "page_created": depths["page_created"],
            "page_moved": depths["page_moved"],
            "page_deleted": depths["page_deleted"],
            "page_link": depths["page_link"],
            "stroke_version": depths["stroke_version"],
        }
    def _record_referenced(self, ev: EventRecord, page: Page, doc: Document) -> None:
        """Scan a record's raw bytes for attachment UUIDS to link usage."""
        if page is None:
            return
        for m in UUID_RE.findall(ev.raw):
            try:
                u = m.decode("ascii").upper()
            except Exception:
                continue
            if u in doc.attachments and u not in page.paper_attachments:
                page.paper_attachments.append(u)

    def _first_float(self, fm: dict, number: int) -> Optional[float]:
        for f in fm.get(number, []):
            import struct

            try:
                return struct.unpack("<f", struct.pack("<I", f.value))[0]
            except Exception:
                return None
        return None

    def _find_page_by_entity(self, doc: Document, entity_uuid: str) -> Optional[Page]:
        if entity_uuid:
            for p in doc.pages:
                if p.entity_uuid == entity_uuid:
                    return p
            # entity = content - 1 in last hex group
            content = self._content_from_entity(entity_uuid)
            if content:
                return self._find_page_by_content(doc, content)
        return None

    @staticmethod
    def _content_from_entity(entity_uuid: str) -> Optional[str]:
        """Content UUID = entity UUID with the last hex group incremented.

        GoodNotes allocates page content UUIDs adjacent to their entity
        UUIDs such that content = entity + 1 (covering hex carries, e.g.
        entity ``...A2F`` -> content ``...A30``)."""
        if not entity_uuid or len(entity_uuid) != 36:
            return None
        group = entity_uuid[24:36]
        try:
            n = int(group, 16) + 1
        except ValueError:
            return None
        if n >= 1 << 48:
            return None
        return entity_uuid[:24] + f"{n:012X}"

    def _find_page_by_content(self, doc: Document, content_uuid: str) -> Optional[Page]:
        if not content_uuid:
            return None
        cu = content_uuid.upper()
        for p in doc.pages:
            if p.content_uuid.upper() == cu:
                return p
        return None

    def _resolve_paper_attachments(self, doc: Document) -> None:
        for p in doc.pages:
            if p.paper_uuid and p.paper_uuid in doc.papers:
                pd = doc.papers[p.paper_uuid]
                if pd.attachment_uuid:
                    p.paper_attachments.append(pd.attachment_uuid)
            p.paper_attachments = list(dict.fromkeys(p.paper_attachments))

    def _resolve_attachment_usage(self, doc: Document) -> None:
        """Attachment UUIDs referenced from F105 page-link bodies of pages."""
        for i, ev in enumerate(doc.events):
            fields = fields_map(ev.raw)
            for fn in (105, 106, 102):
                for f in fields.get(fn, []):
                    body = fields_map(f.value)
                    linked = self._first_text(body, 3 if fn == 105 else 1)
                    if linked and linked.upper() in doc.attachments:
                        page = self._find_page_by_content(doc, linked)
                        if page is not None:
                            continue
                        # attach to any page that owns this event record
                        for p in doc.pages:
                            if ev.raw in p.owning_events:
                                if linked.upper() not in p.paper_attachments:
                                    p.paper_attachments.append(linked.upper())

    def _scan_note_attachment_refs(self, doc: Document) -> None:
        with zipfile.ZipFile(self.path, "r") as z:
            for page in doc.pages:
                if not page.note_entry:
                    continue
                note_name = f"notes/{page.content_uuid}"
                if note_name not in doc.all_entries:
                    continue
                try:
                    data = z.read(note_name)
                except Exception:
                    continue
                for m in UUID_RE.findall(data):
                    u = m.decode("ascii").upper()
                    if u in doc.attachments:
                        page.note_implied_attachments.append(u)

    def _attach_search(self, doc: Document) -> None:
        for page in doc.pages:
            if page.content_uuid in doc.search_files:
                page.search_uuid = page.content_uuid
                page.search_entry = doc.search_files[page.content_uuid]
            else:
                # try the path form search/<uuid>
                for u, ref in doc.search_files.items():
                    if ref.filename == f"search/{page.content_uuid}":
                        page.search_uuid = u
                        page.search_entry = ref
                        break

    # ------------------------------------------------------------- analysis
    def analyze(self, doc: Optional[Document] = None) -> dict[str, Any]:
        doc = doc or self.read()
        return build_analysis(doc)


## ---------------------------------------------------------------------------
## Analysis helpers
## ---------------------------------------------------------------------------


def build_analysis(doc: Document) -> dict[str, Any]:
    total_size = os.path.getsize(doc.source_path)
    notes_entries = [e for n, e in doc.all_entries.items() if n.startswith("notes/")]
    att_entries = [e for n, e in doc.all_entries.items() if n.startswith("attachments/")]
    search_entries = [e for n, e in doc.all_entries.items() if n.startswith("search/")]

    events_roles = Counter()
    for ev in doc.events:
        for fn in EVENT_ACTIONS:
            if fn in ev.top_fields:
                events_roles[EVENT_ACTIONS[fn]] += 1

    page_entity_map = [
        {
            "page_number": p.page_number,
            "content_uuid": p.content_uuid,
            "entity_uuid": p.entity_uuid,
            "order_key": p.order_key,
            "paper_uuid": p.paper_uuid,
        }
        for p in doc.pages
    ]

    attachment_page_map = {}
    for p in doc.pages:
        used = list(dict.fromkeys(p.paper_attachments + p.note_implied_attachments))
        if used:
            attachment_page_map[str(p.page_number)] = {
                "content_uuid": p.content_uuid,
                "attachments": used,
            }

    extra_search = [u for u in doc.search_files if u not in {p.content_uuid for p in doc.pages}]

    return {
        "document": os.path.basename(doc.source_path),
        "size_bytes": total_size,
        "size_mb": round(total_size / 1024 / 1024, 2),
        "total_entries": len(doc.all_entries),
        "schema_version": doc.schema_version,
        "schema_raw": doc.schema_data.hex(),
        "notes_count": len(notes_entries),
        "non_empty_notes": sum(1 for e in notes_entries if e.file_size > 0),
        "empty_notes": sum(1 for e in notes_entries if e.file_size == 0),
        "attachments_count": len(att_entries),
        "search_count": len(search_entries),
        "indexes": [
            n
            for n in doc.all_entries
            if n.startswith("index.") or n == "schema.pb" or n == "document.info.pb"
        ],
        "page_count": len(doc.pages),
        "events_records": len(doc.events),
        "events_roles": dict(events_roles),
        "document_uuid": doc.document_uuid,
        "pages_ordered": [p.content_uuid for p in doc.pages],
        "page_entity_map": page_entity_map,
        "attachment_page_map": attachment_page_map,
        "papers": [
            {
                "paper_uuid": pd.paper_uuid,
                "attachment_uuid": pd.attachment_uuid,
                "width": pd.width,
                "height": pd.height,
                "name": pd.name,
            }
            for pd in doc.papers.values()
        ],
        "unused_attachments": [
            u for u in doc.attachments if not any(u in (p.paper_attachments + p.note_implied_attachments) for p in doc.pages)
        ],
        "extra_search_files_not_page_uuids": len(extra_search),
        "search_index_records": len(doc.search_index_raw),
    }


def format_events_catalog(doc: Document) -> str:
    """Human-readable catalog of the event stream for debugging/reporting."""
    lines = [f"Events: {len(doc.events)} records"]
    for i, ev in enumerate(doc.events):
        fields = fields_map(ev.raw)
        subject = ""
        for f in fields.get(1, []):
            s = _safe_text(f)
            if s:
                subject = s
                break
        actions = [
            f"{EVENT_ACTIONS.get(fn, fn)}({fn})"
            for fn in sorted(ev.top_fields)
            if fn in EVENT_ACTIONS
        ]
        lines.append(
            f"  [{i:4d}] subj={subject or '-':36s} top={','.join(map(str, sorted(ev.top_fields)))} action={','.join(actions)}"
        )
    return "\n".join(lines)