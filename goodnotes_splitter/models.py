"""Domain models for the GoodNotes splitter.

The model is deliberately close to the on-disk structure so that the writer
can be a thin "select a subset and repackage" step.  Page note data,
attachments, and search blobs are referenced by ZIP entry name and read
lazily from the source archive, never decoded/re-encoded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ZipEntryRef:
    """Raw information about a single ZIP member of the source document."""

    filename: str
    compress_size: int
    file_size: int
    crc: int
    compress_type: int
    # Populated on demand by the reader; not serialized.
    _data: Optional[bytes] = None


@dataclass
class Page:
    """One page of the document.

    ``content_uuid`` is the page's content UUID, also the base name of the
    ``notes/<content_uuid>`` member.  ``entity_uuid`` and ``order_key`` come
    from the ``index.events.pb`` field-54 records.  ``page_number`` is the
    1-based position in the document's page order (from index.notes.pb).
    """

    content_uuid: str
    page_number: int
    entity_uuid: Optional[str] = None
    order_key: Optional[str] = None
    paper_uuid: Optional[str] = None
    note_entry: Optional[ZipEntryRef] = None
    search_entry: Optional[ZipEntryRef] = None
    search_uuid: Optional[str] = None
    # Attachments discovered because this page's paper references them.
    paper_attachments: list[str] = field(default_factory=list)
    # Attachments discovered by scanning the raw note bytes for UUIDs.
    note_implied_attachments: list[str] = field(default_factory=list)
    # Raw event records whose top-level subject equals the page content UUID
    # (used when rebuilding index.events.pb).
    owning_events: list[bytes] = field(default_factory=list)


@dataclass
class PaperDef:
    """A paper (template) definition from index.events.pb field-2 records."""

    paper_uuid: str
    attachment_uuid: Optional[str] = None
    width: Optional[float] = None
    height: Optional[float] = None
    name: Optional[str] = None
    raw_record: Optional[bytes] = None


@dataclass
class Attachment:
    """An attachment (embedded PDF) referenced by a page."""

    uuid: str
    path: str
    entry: Optional[ZipEntryRef] = None


@dataclass
class EventRecord:
    """One raw record from index.events.pb together with its top-level
    field numbers (for classification)."""

    raw: bytes
    top_fields: frozenset[int]


@dataclass
class Document:
    """Parsed representation of a whole .goodnotes document.

    ``pages`` is in document page order.  ``attachments`` maps attachment
    UUID to its model.  ``events`` preserves the full event stream in order
    for the writer to filter.
    """

    source_path: str
    schema_version: Optional[int] = None
    schema_data: bytes = b""
    thumbnail_data: bytes = b""
    document_info: bytes = b""
    pages: list[Page] = field(default_factory=list)
    attachments: dict[str, Attachment] = field(default_factory=dict)
    papers: dict[str, PaperDef] = field(default_factory=dict)
    notes_index_raw: dict[str, bytes] = field(default_factory=dict)
    attachments_index_raw: dict[str, bytes] = field(default_factory=dict)
    search_index_raw: dict[str, bytes] = field(default_factory=dict)
    events_raw: bytes = b""
    events: list[EventRecord] = field(default_factory=list)
    all_entries: dict[str, ZipEntryRef] = field(default_factory=dict)
    document_uuid: Optional[str] = None
    title: Optional[str] = None
    events_aux: dict = field(default_factory=dict)
    # Search index UUIDs that exist in the archive.
    search_files: dict[str, ZipEntryRef] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def total_pages(self) -> int:
        return len(self.pages)