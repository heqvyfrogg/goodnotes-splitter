"""GoodNotes .goodnotes splitter.

Analyzes a GoodNotes document (a ZIP archive of notes/*, attachments/*,
search/* and index.* protobufs), and repackages it into multiple valid
.goodnotes documents whose page subsets stay under a size target.

The design principle is "identify -> select -> copy -> repackage": page
note payloads, embedded PDF attachments, and search blobs are copied as raw
bytes; only the index protobufs are rebuilt.
"""

from .models import Document, Page, Attachment, PaperDef
from .reader import GoodNotesReader
from .writer import select_subset, rebuild_events, write_part, estimate_part_size
from .partitioner import partition_pages
from .validator import validate_part, validate_parts
from . import protobuf

__version__ = "0.1.0"

__all__ = [
    "GoodNotesReader",
    "Document",
    "Page",
    "Attachment",
    "PaperDef",
    "protobuf",
    "select_subset",
    "rebuild_events",
    "write_part",
    "estimate_part_size",
    "partition_pages",
    "validate_part",
    "validate_parts",
    "__version__",
]