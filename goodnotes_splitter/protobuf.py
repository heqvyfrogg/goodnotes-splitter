"""Protobuf wire-format decoding/encoding for GoodNotes index files.

GoodNotes stores index metadata as streams of length-delimited protobuf
messages:  ``<varint len><message>`` and each message is a series of
key-value fields.  We preserve raw bytes where possible so indexes can be
rebuilt without touching the underlying note/attachment payloads.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

WIRETYPE_VARINT = 0
WIRETYPE_FIXED64 = 1
WIRETYPE_LENGTH_DELIMITED = 2
WIRETYPE_START_GROUP = 3
WIRETYPE_END_GROUP = 4
WIRETYPE_FIXED32 = 5


@dataclass(frozen=True)
class Field:
    """One decoded protobuf field.

    ``value`` is either an int (varint/fixed bytes reinterpreted) or bytes
    (length-delimited).  ``raw`` is the exact encoded bytes of the whole
    field (tag + payload) allowing lossless re-serialization.
    """

    number: int
    wire_type: int
    value: Any
    raw: bytes


## ---------------------------------------------------------------------------
## Decoding
## ---------------------------------------------------------------------------


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint, returning (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
    raise ValueError("varint underflow at end of buffer")


def decode_field(data: bytes, pos: int) -> tuple[Field | None, int]:
    """Decode a single field starting at ``pos``.

    Returns (Field, new_pos).  Returns (None, pos) when no field remains.
    """
    if pos >= len(data):
        return None, pos
    tag_start = pos
    tag, pos = read_varint(data, pos)
    number = tag >> 3
    wire_type = tag & 0x07
    start = tag_start  # rewind to beginning of tag for raw capture

    if wire_type == WIRETYPE_VARINT:
        value, new_pos = read_varint(data, pos)
        raw = data[start:new_pos]
        return Field(number, wire_type, value, raw), new_pos
    elif wire_type == WIRETYPE_FIXED64:
        value = struct.unpack("<Q", data[pos : pos + 8])[0]
        new_pos = pos + 8
        raw = data[start:new_pos]
        return Field(number, wire_type, value, raw), new_pos
    elif wire_type == WIRETYPE_LENGTH_DELIMITED:
        length, new_pos = read_varint(data, pos)
        end = new_pos + length
        value = data[new_pos:end]
        raw = data[start:end]
        return Field(number, wire_type, value, raw), end
    elif wire_type == WIRETYPE_FIXED32:
        value = struct.unpack("<I", data[pos : pos + 4])[0]
        new_pos = pos + 4
        raw = data[start:new_pos]
        return Field(number, wire_type, value, raw), new_pos
    else:
        # Group types: still capture raw field but cannot nested-parse reliably.
        # These should not appear in GoodNotes index files.
        raise ValueError(f"unsupported wire type {wire_type} at offset {start}")


def decode_message(data: bytes) -> list[Field]:
    """Decode a full protobuf message into an ordered field list."""
    fields: list[Field] = []
    pos = 0
    while pos < len(data):
        f, pos = decode_field(data, pos)
        if f is None:
            break
        fields.append(f)
    return fields


def fields_map(data: bytes) -> dict[int, list[Field]]:
    """Group decoded fields by field number (order preserved per number)."""
    result: dict[int, list[Field]] = {}
    for f in decode_message(data):
        result.setdefault(f.number, []).append(f)
    return result


def split_delimited(data: bytes) -> list[bytes]:
    """Split a `<varint length><message>` record stream into raw records.

    GoodNotes indexes (index.notes.pb etc.) are such streams.
    """
    records: list[bytes] = []
    pos = 0
    while pos < len(data):
        length, new_pos = read_varint(data, pos)
        end = new_pos + length
        if end > len(data):
            raise ValueError("record length overruns buffer")
        records.append(data[new_pos:end])
        pos = end
    return records


## ---------------------------------------------------------------------------
## Encoding
## ---------------------------------------------------------------------------


def write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def write_tag(number: int, wire_type: int) -> bytes:
    return write_varint((number << 3) | wire_type)


def write_varint_field(number: int, value: int) -> bytes:
    return write_tag(number, WIRETYPE_VARINT) + write_varint(value)


def write_len_delimited(number: int, payload: bytes) -> bytes:
    return write_tag(number, WIRETYPE_LENGTH_DELIMITED) + write_varint(len(payload)) + payload


def write_fixed32_field(number: int, value: int) -> bytes:
    return write_tag(number, WIRETYPE_FIXED32) + struct.pack("<I", value)


def write_fixed64_field(number: int, value: int) -> bytes:
    return write_tag(number, WIRETYPE_FIXED64) + struct.pack("<Q", value)


def write_double_field(number: int, value: float) -> bytes:
    b = struct.pack("<d", value)
    return write_tag(number, WIRETYPE_FIXED64) + b


def join_delimited(records: list[bytes]) -> bytes:
    """Inverse of ``split_delimited``: prefix each record with its length."""
    return b"".join(write_varint(len(r)) + r for r in records)


def encode_message(fields: list[bytes]) -> bytes:
    """Concatenate already-encoded field bytes to form a message."""
    return b"".join(fields)


## ---------------------------------------------------------------------------
## Helpers for interpreting field values
## ---------------------------------------------------------------------------


def field_text(f: Field) -> str | None:
    """UTF-8 decode a length-delimited field value; None if not valid text."""
    if f.wire_type != WIRETYPE_LENGTH_DELIMITED:
        return None
    try:
        s = f.value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s


UUID_RE_ISH = None  # import-time import avoided for caching below


def looks_like_uuid(s: str) -> bool:
    """Heuristic UUID check (36 chars, hyphenated)."""
    if len(s) != 36:
        return False
    if s[8] != "-" or s[13] != "-" or s[18] != "-" or s[23] != "-":
        return False
    return all(
        c.isdigit() or c.upper() in "ABCDEF" or c == "-" for c in s.replace("-", "")
    )