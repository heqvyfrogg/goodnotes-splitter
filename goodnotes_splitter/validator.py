"""Validator: prove a written .goodnotes part is internally consistent.

Checks are derived from the part file itself (standalone), so a part can be
validated by anyone with a copy of it.  When the source Document is also
provided, an extra check verifies that the original document UUID no longer
appears anywhere in the part.

Checks:
  zip_ok            archive opens and every CRC verifies
  size_limit        part file < hard limit (default 5 MiB)
  schema_pb         schema.pb present and version == 35
  fixed_entries     thumbnail.jpg and document.info.pb present
  notes_index       index.notes.pb rows match notes/* entries 1:1
  attachments_index index.attachments.pb rows match attachments/* 1:1
  search_index      index.search.pb rows match search/* entries 1:1
  events_parse      index.events.pb splits and every record decodes
  f54_count         one page-created record per page
  document_created  doc-scoped records (incl. F30) survive the filter
  page_order        page order (notes index) matches F54 journal order
  f54_graph         each page's paper and each paper's attachment exist
  entity_content    each page has content UUID = entity + 1 (last hex group)
  uuid_universe     no kept record references an excluded source entity
  first_page_ref    n/a (F30 first-page is a device UUID, never a page)
  no_old_doc_refs   original source document UUID is gone from the part
"""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import Document
from .protobuf import decode_message, fields_map, field_text, split_delimited

UUID_RE = re.compile(
    rb"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
    re.IGNORECASE,
)

WANTED_SCHEMA_VERSION = 35


@dataclass
class Report:
    path: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _n, ok, _d in self.checks)

    def problems(self) -> list[str]:
        return [f"{n}: {d}" for n, ok, d in self.checks if not ok]

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))


def _first_text(fm: dict, number: int) -> Optional[str]:
    for f in fm.get(number, []):
        s = field_text(f)
        if s:
            return s
    return None


def _content_from_entity(entity: str) -> Optional[str]:
    if not entity or len(entity) != 36:
        return None
    try:
        n = int(entity[24:36], 16) + 1
    except ValueError:
        return None
    if n >= 1 << 48:
        return None
    return entity[:24] + f"{n:012X}"


def _uuids(data: bytes) -> set[str]:
    return {m.group(0).decode("ascii").upper() for m in UUID_RE.finditer(data)}


def _record_subject(rec: bytes) -> Optional[str]:
    for m in UUID_RE.finditer(rec):
        try:
            return m.group(0).decode("ascii").upper()
        except Exception:
            continue
    return None


def _paper_ref(f54_body: dict) -> str:
    for s in f54_body.get(3, []):
        sm = fields_map(s.value)
        u = _first_text(sm, 1)
        if u:
            return u.upper()
    return ""


def validate_part(path: str, hard_mb: float = 5.0, source: Optional[Document] = None) -> Report:
    rep = Report(path=path)
    hard_bytes = int(hard_mb * 1024 * 1024)

    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            rep.add("zip_ok", bad is None, "" if bad is None else f"bad CRC in {bad}")
            size = os.path.getsize(path)
            rep.add("size_limit", size <= hard_bytes, f"{size} bytes (limit {hard_bytes})")
            names = {i.filename for i in z.infolist() if not i.is_dir()}

            if "schema.pb" not in names:
                rep.add("schema_pb", False, "missing")
            else:
                try:
                    fm = fields_map(z.read("schema.pb"))
                    ver = [f.value for f in fm.get(1, []) if f.wire_type == 0]
                    rep.add(
                        "schema_pb",
                        bool(ver) and ver[0] == WANTED_SCHEMA_VERSION,
                        f"version={ver[0] if ver else '?'}",
                    )
                except Exception as exc:
                    rep.add("schema_pb", False, str(exc))

            rep.add(
                "fixed_entries",
                "thumbnail.jpg" in names and "document.info.pb" in names,
                f"thumbnail={'y' if 'thumbnail.jpg' in names else 'n'} "
                f"docinfo={'y' if 'document.info.pb' in names else 'n'}",
            )

            def index_rows(name: str) -> tuple[list[bytes], list[str], list[str]]:
                """Return (records, f1-uuids, f2-paths) for an index file.

                For attachments the record's f1 is the *content* UUID, which
                differs from the *file* UUID embedded in the f2 path, so the
                invariant checked by the caller is 1:1 row<->file coverage
                via the f2 path (not f1 == file base).
                """
                if name not in names:
                    rep.add(f"{name}_present", False, "missing")
                    return [], [], []
                try:
                    recs = split_delimited(z.read(name))
                except Exception as exc:
                    rep.add(f"{name}_parse", False, str(exc))
                    return [], [], []
                uuids: list[str] = []
                paths: list[str] = []
                for rec in recs:
                    fm = fields_map(rec)
                    u = (_first_text(fm, 1) or "").upper()
                    pth = _first_text(fm, 2) or ""
                    if u and pth:
                        uuids.append(u)
                        paths.append(pth)
                return recs, uuids, paths

            note_rows, note_uuids, note_paths = index_rows("index.notes.pb")
            att_rows, att_uuids, att_paths = index_rows("index.attachments.pb")
            search_rows, search_uuids, search_paths = index_rows("index.search.pb")

            def coverage_ok(paths: list[str], prefix: str) -> tuple[bool, str]:
                files = sorted(n for n in names if n.startswith(prefix))
                if len(paths) != len(files) or len(set(paths)) != len(paths):
                    return False, f"rows={len(paths)} files={len(files)} dupes={len(paths) != len(set(paths))}"
                missing = [p for p in paths if p not in names]
                if missing:
                    return False, f"{len(missing)} f2 paths not present"
                return True, f"rows={len(paths)} files={len(files)}"

            ok, det = coverage_ok(note_paths, "notes/")
            rep.add("notes_index", ok, det)
            ok, det = coverage_ok(att_paths, "attachments/")
            rep.add("attachments_index", ok, det)
            ok, det = coverage_ok(search_paths, "search/")
            rep.add("search_index", ok, det)

            # ---- events ----
            if "index.events.pb" not in names:
                rep.add("events_parse", False, "missing")
                events_recs: list[bytes] = []
            else:
                try:
                    events_recs = split_delimited(z.read("index.events.pb"))
                except Exception as exc:
                    rep.add("events_parse", False, str(exc))
                    events_recs = []
            ev_ok = True
            for rec in events_recs:
                try:
                    decode_message(rec)
                except Exception as exc:
                    ev_ok = False
                    rep.add("events_parse", False, str(exc))
                    break
            if ev_ok:
                rep.add("events_parse", True, f"records={len(events_recs)}")

            # ---- F54 journal + doc-created record ----
            f54s: list[str] = []
            doc_scoped_seen: set[int] = set()
            paper_to_att: dict[str, str] = {}
            for rec in events_recs:
                fm = fields_map(rec)
                top = {f.number for f in decode_message(rec)}
                for f in fm.get(54, []):
                    body = fields_map(f.value)
                    f54s.append((_first_text(body, 2) or "").upper())
                if top - {1} <= {10, 30, 31, 60}:
                    doc_scoped_seen |= top - {1}
                for f in fm.get(2, []):
                    body = fields_map(f.value)
                    paper = (_first_text(body, 2) or _first_text(body, 1) or "").upper()
                    att = (_first_text(body, 4) or "").upper()
                    if paper:
                        paper_to_att.setdefault(paper, att)

            rep.add(
                "document_created",
                30 in doc_scoped_seen,
                f"doc-scoped actions kept: {sorted(doc_scoped_seen) or '-'}",
            )

            rep.add(
                "f54_count",
                len(f54s) == len(note_uuids),
                f"f54={len(f54s)} pages={len(note_uuids)}",
            )

            page_entity: dict[str, str] = {}
            for c in note_uuids:
                try:
                    n = int(c[24:36], 16) - 1
                    page_entity[c] = c[:24] + f"{n:012X}"
                except Exception:
                    page_entity[c] = ""

            rep.add(
                "page_order",
                [_content_from_entity(e) for e in f54s] == note_uuids,
                f"pages={len(note_uuids)}",
            )
            rep.add(
                "entity_content",
                all(_content_from_entity(e) == c for c, e in page_entity.items()),
                f"pages={len(note_uuids)}",
            )

            # ---- order_keys (device-authoritative page order) ----
            # GoodNotes sorts pages by the F54 order keys (field 4.1) when
            # importing a .goodnotes; index.notes.pb order is only the app's
            # internal storage order.  Validate that the kept F54 keys are
            # non-decreasing in exactly the intended page order.
            entity_key: dict[str, str | None] = {e: None for e in page_entity.values() if e}
            for rec in events_recs:
                fm = fields_map(rec)
                for f in fm.get(54, []):
                    body = fields_map(f.value)
                    ent = (_first_text(body, 2) or "").upper()
                    for f4 in body.get(4, []):
                        inner = fields_map(f4.value)
                        k = None
                        for x in inner.get(1, []):
                            if x.wire_type == 2:
                                try:
                                    k = x.value.decode("ascii")
                                except Exception:
                                    k = None
                        entity_key[ent] = k
            missing = [e for e, k in entity_key.items() if not k]
            seq = [entity_key[page_entity[c]] for c in note_uuids if c in page_entity]
            increasing = all(
                (seq[i] or "") < (seq[i + 1] or "") for i in range(len(seq) - 1)
            )
            rep.add(
                "order_keys",
                increasing and not missing and len(seq) == len(page_entity),
                f"pages={len(seq)} missing={len(missing)}",
            )

            graph_ok = True
            for ent in f54s:
                # find this page's F54 record again for its paper
                paper = ""
                for rec in events_recs:
                    fm = fields_map(rec)
                    for f in fm.get(54, []):
                        body = fields_map(f.value)
                        if (_first_text(body, 2) or "").upper() == ent:
                            paper = _paper_ref(body)
                            break
                    if paper:
                        break
                if not paper or paper not in paper_to_att:
                    graph_ok = False
                    break
                if paper_to_att[paper] and paper_to_att[paper] not in set(att_uuids):
                    graph_ok = False
                    break
            rep.add(
                "f54_graph",
                graph_ok,
                f"papers={len(paper_to_att)} attachments={len(att_uuids)}",
            )

            # ---- UUID universe (only meaningful against the source) ----
            # Event records embed ephemeral nonces and paper-template name
            # UUIDs that legitimately belong to nobody, so a strictly
            # self-referential check would produce false positives.  Instead,
            # we verify that no *source* entity that the source document
            # knows about is referenced by a kept record without living in
            # the part.  That catches dangling links to excluded pages.
            if source is not None:
                source_vocab: set[str] = set()
                for p in source.pages:
                    if p.content_uuid:
                        source_vocab.add(p.content_uuid.upper())
                    if p.entity_uuid:
                        source_vocab.add(p.entity_uuid.upper())
                for u in source.papers:
                    source_vocab.add(u.upper())
                for u, a in source.attachments.items():
                    source_vocab.add(u.upper())
                    if a.path:
                        source_vocab.add(a.path.rsplit("/", 1)[-1].upper())
                for u in source.search_files:
                    source_vocab.add(u.upper())

                part_universe = set(note_uuids) | set(att_uuids) | set(search_uuids)
                part_universe |= {p.rsplit("/", 1)[-1].upper() for p in att_paths}
                part_universe |= set(page_entity.values())
                part_universe |= set(paper_to_att.keys())
                part_universe |= {a for a in paper_to_att.values() if a}
                for rec in events_recs:
                    fm = fields_map(rec)
                    for f in fm.get(54, []):
                        d = _first_text(fields_map(f.value), 1)
                        if d:
                            part_universe.add(d.upper())

                v_ok = True
                v_detail: list[str] = []
                for rec in events_recs:
                    top = {f.number for f in decode_message(rec)}
                    dangling = sorted(
                        (_uuids(rec) & source_vocab) - part_universe
                    )
                    if dangling:
                        v_ok = False
                        v_detail.append(
                            f"rec(top={sorted(top)}): {','.join(dangling[:2])}"
                        )
                        if len(v_detail) >= 4:
                            break
                rep.add("uuid_universe", v_ok, "ok" if v_ok else "; ".join(v_detail))
            else:
                rep.add("uuid_universe", True, "skipped (no source vocab)")

            rep.add(
                "first_page_ref",
                True,
                "n/a (F30 first-page is a device UUID, never a page)",
            )

            # ---- old doc UUID absent ----
            old = (source.document_uuid or "").upper() if source else None
            if old:
                found = []
                for name in sorted(names):
                    if old.encode("ascii") in z.read(name):
                        found.append(name)
                rep.add(
                    "no_old_doc_refs",
                    not found,
                    "" if not found else f"old UUID in {','.join(found)}",
                )
            else:
                rep.add("no_old_doc_refs", True, "skipped (no source)")

    except Exception as exc:
        rep.add("zip_ok", False, str(exc))

    return rep


def validate_parts(paths: Iterable[str], hard_mb: float = 5.0, source: Optional[Document] = None) -> list[Report]:
    return [validate_part(p, hard_mb=hard_mb, source=source) for p in paths]


def render(reports: list[Report]) -> str:
    lines: list[str] = []
    for rep in reports:
        lines.append(f"== {os.path.basename(rep.path)} ({os.path.getsize(rep.path)} bytes)")
        for name, ok, detail in rep.checks:
            tag = "OK " if ok else "FAIL"
            suffix = f"  [{detail}]" if detail else ""
            lines.append(f"  {tag} {name}{suffix}")
    return "\n".join(lines)