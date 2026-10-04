// GoodNotes Splitter Engine for Browser & Node.js
// Stdlib & JSZip only. Zero external dependencies.

(function (global) {
  'use strict';

  // ---------------------------------------------------------------------------
  // Protobuf Wire Format Low-Level Helpers
  // ---------------------------------------------------------------------------

  function decodeVarint(buf, offset) {
    let res = 0n;
    let shift = 0n;
    let pos = offset;
    while (pos < buf.length) {
      const b = buf[pos++];
      res |= BigInt(b & 0x7f) << shift;
      if (!(b & 0x80)) break;
      shift += 7n;
    }
    return [res, pos];
  }

  function encodeVarint(val) {
    let v = typeof val === 'bigint' ? val : BigInt(val);
    const bytes = [];
    while (v >= 0x80n) {
      bytes.push(Number((v & 0x7fn) | 0x80n));
      v >>= 7n;
    }
    bytes.push(Number(v & 0x7fn));
    return new Uint8Array(bytes);
  }

  function decodeMessage(buf) {
    const fields = [];
    let pos = 0;
    while (pos < buf.length) {
      const tagStart = pos;
      const [tag, nextPos] = decodeVarint(buf, pos);
      pos = nextPos;
      const fieldNum = Number(tag >> 3n);
      const wireType = Number(tag & 0x7n);

      let val = null;
      let rawBytes = null;

      if (wireType === 0) {
        const [v, p2] = decodeVarint(buf, pos);
        val = v;
        rawBytes = buf.subarray(tagStart, p2);
        pos = p2;
      } else if (wireType === 2) {
        const [lenBig, p2] = decodeVarint(buf, pos);
        const len = Number(lenBig);
        val = buf.subarray(p2, p2 + len);
        rawBytes = buf.subarray(tagStart, p2 + len);
        pos = p2 + len;
      } else if (wireType === 1) {
        val = buf.subarray(pos, pos + 8);
        rawBytes = buf.subarray(tagStart, pos + 8);
        pos += 8;
      } else if (wireType === 5) {
        val = buf.subarray(pos, pos + 4);
        rawBytes = buf.subarray(tagStart, pos + 4);
        pos += 4;
      } else {
        break;
      }

      fields.push({
        number: fieldNum,
        wireType: wireType,
        value: val,
        raw: rawBytes,
      });
    }
    return fields;
  }

  function writeLenDelimited(fieldNum, payload) {
    const tag = encodeVarint(BigInt((fieldNum << 3) | 2));
    const len = encodeVarint(BigInt(payload.length));
    const res = new Uint8Array(tag.length + len.length + payload.length);
    res.set(tag, 0);
    res.set(len, tag.length);
    res.set(payload, tag.length + len.length);
    return res;
  }

  function splitDelimited(buf) {
    const records = [];
    let pos = 0;
    while (pos < buf.length) {
      const [lenBig, nextPos] = decodeVarint(buf, pos);
      const len = Number(lenBig);
      const rec = buf.subarray(nextPos, nextPos + len);
      records.push(rec);
      pos = nextPos + len;
    }
    return records;
  }

  function joinDelimited(records) {
    let totalLen = 0;
    const items = [];
    for (const r of records) {
      const lenBytes = encodeVarint(BigInt(r.length));
      totalLen += lenBytes.length + r.length;
      items.push({ lenBytes, r });
    }
    const res = new Uint8Array(totalLen);
    let offset = 0;
    for (const it of items) {
      res.set(it.lenBytes, offset);
      offset += it.lenBytes.length;
      res.set(it.r, offset);
      offset += it.r.length;
    }
    return res;
  }

  function decodeUtf8(bytes) {
    return new TextDecoder().decode(bytes);
  }

  function encodeUtf8(str) {
    return new TextEncoder().encode(str);
  }

  function extractFirstString(fields, fieldNum) {
    for (const f of fields) {
      if (f.number === fieldNum && f.wireType === 2) {
        try {
          return decodeUtf8(f.value);
        } catch (e) {
          return null;
        }
      }
    }
    return null;
  }

  function generateUUID() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      const r = (Math.random() * 16) | 0;
      const v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16).toUpperCase();
    });
  }

  // ---------------------------------------------------------------------------
  // Core Splitter Logic
  // ---------------------------------------------------------------------------

  const UUID_REGEX = /[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}/gi;

  function findUUIDsInBytes(buf) {
    let str = '';
    for (let i = 0; i < buf.length; i++) {
      const c = buf[i];
      str += (c >= 32 && c <= 126) ? String.fromCharCode(c) : ' ';
    }
    const matches = str.match(UUID_REGEX) || [];
    return matches.map((m) => m.toUpperCase());
  }

  function contentToEntity(contentUuid) {
    const c = contentUuid.toUpperCase();
    const last12 = c.slice(24);
    const n = parseInt(last12, 16) - 1;
    return c.slice(0, 24) + n.toString(16).toUpperCase().padStart(12, '0');
  }

  class GoodNotesDoc {
    constructor() {
      this.files = new Map(); // path -> Uint8Array
      this.fileCompressedSizes = new Map(); // path -> number
      this.docUuid = null;
      this.pages = [];
      this.events = [];
      this.attachments = new Set(); // known attachment UUIDs
      this.attachmentPaths = new Map(); // attUuid -> relative zip path
      this.attachmentIndexRaw = new Map(); // uuid -> raw record
      this.notesIndexRaw = new Map(); // uuid -> raw record
      this.searchPaths = new Map(); // uuid -> relative zip path
      this.searchIndexRaw = new Map(); // uuid -> raw record
      this.papers = new Map(); // paperUuid -> { attUuid, ... }
    }
  }

  async function parseGoodNotes(zip) {
    const doc = new GoodNotesDoc();

    // 1. Index all file entries and sizes
    for (const [filename, fileObj] of Object.entries(zip.files)) {
      if (fileObj.dir) continue;
      const data = await fileObj.async('uint8array');
      doc.files.set(filename, data);
      const compSize = fileObj._data ? fileObj._data.compressedSize : data.length;
      doc.fileCompressedSizes.set(filename, compSize || data.length);
    }

    // 2. Parse index.attachments.pb
    if (doc.files.has('index.attachments.pb')) {
      const aRaw = doc.files.get('index.attachments.pb');
      const recs = splitDelimited(aRaw);
      for (const r of recs) {
        const fm = decodeMessage(r);
        const u = extractFirstString(fm, 1);
        const p = extractFirstString(fm, 2);
        if (u && p) {
          const uUp = u.toUpperCase();
          doc.attachments.add(uUp);
          doc.attachmentPaths.set(uUp, p);
          doc.attachmentIndexRaw.set(uUp, r);
        }
      }
    }

    // 3. Parse index.events.pb
    if (doc.files.has('index.events.pb')) {
      const evRaw = doc.files.get('index.events.pb');
      const records = splitDelimited(evRaw);
      for (const r of records) {
        const top = decodeMessage(r);
        const topNums = new Set(top.map((f) => f.number));
        const uuids = findUUIDsInBytes(r);
        const subj = uuids.length > 0 ? uuids[0] : null;
        doc.events.push({ raw: r, topFields: topNums, subject: subj });
      }
    }

    // 4. Parse index.notes.pb
    const rawPages = [];
    if (doc.files.has('index.notes.pb')) {
      const nRaw = doc.files.get('index.notes.pb');
      const recs = splitDelimited(nRaw);
      for (const r of recs) {
        const fm = decodeMessage(r);
        const u = extractFirstString(fm, 1);
        const p = extractFirstString(fm, 2);
        if (u && p) {
          const uUp = u.toUpperCase();
          rawPages.push({ contentUuid: uUp, path: p });
          doc.notesIndexRaw.set(uUp, r);
        }
      }
    }

    // 5. Parse index.search.pb
    if (doc.files.has('index.search.pb')) {
      const sRaw = doc.files.get('index.search.pb');
      const recs = splitDelimited(sRaw);
      for (const r of recs) {
        const fm = decodeMessage(r);
        const u = extractFirstString(fm, 1);
        const p = extractFirstString(fm, 2);
        if (u && p) {
          const uUp = u.toUpperCase();
          doc.searchPaths.set(uUp, p);
          doc.searchIndexRaw.set(uUp, r);
        }
      }
    }

    // 6. Map Pages
    const pageByContent = new Map();
    const pageByEntity = new Map();

    for (const rp of rawPages) {
      const ent = contentToEntity(rp.contentUuid);
      const pageObj = {
        contentUuid: rp.contentUuid,
        entityUuid: ent,
        orderKey: '',
        paperUuid: null,
        noteSize: doc.fileCompressedSizes.get(rp.path) || 0,
        searchSize: 0,
        paperAttachments: new Set(),
      };
      pageByContent.set(rp.contentUuid, pageObj);
      pageByEntity.set(ent, pageObj);
      rawPages[rawPages.indexOf(rp)] = pageObj;
    }

    // Scan events for order keys, moves, deletions, and paper refs
    const deletedEntities = new Set();

    for (const ev of doc.events) {
      const fields = decodeMessage(ev.raw);

      // F54: page-created
      for (const f of fields) {
        if (f.number === 54 && f.wireType === 2) {
          const body = decodeMessage(f.value);
          const ent = extractFirstString(body, 2);
          const docId = extractFirstString(body, 1);
          if (docId && !doc.docUuid) doc.docUuid = docId.toUpperCase();

          let paperRef = null;
          let oKey = null;

          for (const bf of body) {
            if (bf.number === 3 && bf.wireType === 2) {
              const sub = decodeMessage(bf.value);
              paperRef = extractFirstString(sub, 1) || paperRef;
            }
            if (bf.number === 4 && bf.wireType === 2) {
              const sub = decodeMessage(bf.value);
              oKey = extractFirstString(sub, 1) || oKey;
            }
          }

          if (ent) {
            const p = pageByEntity.get(ent.toUpperCase());
            if (p) {
              p.orderKey = oKey || p.orderKey;
              p.paperUuid = paperRef ? paperRef.toUpperCase() : p.paperUuid;
            }
          }
        }

        // F55: page-moved
        if (f.number === 55 && f.wireType === 2) {
          const body = decodeMessage(f.value);
          const ent = extractFirstString(body, 2);
          let oKey = null;
          for (const bf of body) {
            if (bf.number === 3 && bf.wireType === 2) {
              const sub = decodeMessage(bf.value);
              oKey = extractFirstString(sub, 1) || oKey;
            }
          }
          if (ent && oKey) {
            const p = pageByEntity.get(ent.toUpperCase());
            if (p) p.orderKey = oKey;
          }
        }

        // F56: page-deleted
        if (f.number === 56 && f.wireType === 2) {
          const body = decodeMessage(f.value);
          const ent = extractFirstString(body, 2);
          if (ent) deletedEntities.add(ent.toUpperCase());
        }

        // F2: paper-definition
        if (f.number === 2 && f.wireType === 2) {
          const body = decodeMessage(f.value);
          const paperId = extractFirstString(body, 2);
          const attId = extractFirstString(body, 4);
          if (paperId) {
            doc.papers.set(paperId.toUpperCase(), {
              attUuid: attId ? attId.toUpperCase() : null,
            });
          }
        }
      }

      // Check event subject & raw references to link attachments to pages
      const evSubj = ev.subject;
      if (evSubj) {
        let p = pageByEntity.get(evSubj) || pageByContent.get(evSubj);
        if (p) {
          const refs = findUUIDsInBytes(ev.raw);
          for (const u of refs) {
            if (doc.attachments.has(u)) {
              p.paperAttachments.add(u);
            }
          }
        }
      }
    }

    // Scan notes blob bytes for embedded attachment references & add paper attachments
    for (const p of rawPages) {
      const nPath = `notes/${p.contentUuid}`;
      if (doc.files.has(nPath)) {
        const nData = doc.files.get(nPath);
        const refs = findUUIDsInBytes(nData);
        for (const u of refs) {
          if (doc.attachments.has(u)) {
            p.paperAttachments.add(u);
          }
        }
      }
      if (p.paperUuid && doc.papers.has(p.paperUuid)) {
        const att = doc.papers.get(p.paperUuid).attUuid;
        if (att && doc.attachments.has(att)) {
          p.paperAttachments.add(att);
        }
      }
      const sPath = doc.searchPaths.get(p.contentUuid) || `search/${p.contentUuid}`;
      if (doc.files.has(sPath)) {
        p.searchSize = doc.fileCompressedSizes.get(sPath) || 0;
      }
    }

    // Filter out deleted pages and sort active pages by device orderKey
    const activePages = rawPages.filter((p) => !deletedEntities.has(p.entityUuid));
    activePages.sort((a, b) => (a.orderKey < b.orderKey ? -1 : a.orderKey > b.orderKey ? 1 : 0));
    activePages.forEach((p, idx) => {
      p.pageNumber = idx + 1;
    });

    doc.pages = activePages;
    return doc;
  }

  // ---------------------------------------------------------------------------
  // Partition Planner (Target 4.5MB, Hard Limit 5.0MB)
  // ---------------------------------------------------------------------------

  function planPartitions(doc, targetBytes = 4.2 * 1024 * 1024) {
    const parts = [];
    let current = [];
    let currentEst = 0;
    const currentPartAtts = new Set();

    for (const page of doc.pages) {
      let pageEst = page.noteSize + page.searchSize + 500;

      for (const aId of page.paperAttachments) {
        if (!currentPartAtts.has(aId)) {
          const aPath = doc.attachmentPaths.get(aId) || `attachments/${aId}`;
          const sz = doc.fileCompressedSizes.get(aPath) || 0;
          pageEst += sz;
        }
      }

      if (current.length > 0 && currentEst + pageEst > targetBytes) {
        parts.push(current);
        current = [page];
        currentPartAtts.clear();
        for (const aId of page.paperAttachments) currentPartAtts.add(aId);
        currentEst = pageEst;
      } else {
        current.push(page);
        for (const aId of page.paperAttachments) currentPartAtts.add(aId);
        currentEst += pageEst;
      }
    }

    if (current.length > 0) {
      parts.push(current);
    }
    return parts;
  }

  // ---------------------------------------------------------------------------
  // F54 Order Key Rewriter
  // ---------------------------------------------------------------------------

  function rewriteF54OrderKey(recBytes, newKeyStr) {
    const keyBytes = encodeUtf8(newKeyStr);
    const top = decodeMessage(recBytes);
    const rebuiltTop = [];

    for (const f of top) {
      if (f.number !== 54 || f.wireType !== 2) {
        rebuiltTop.push(f.raw);
        continue;
      }
      const body = decodeMessage(f.value);
      const rebuiltBody = [];
      for (const bf of body) {
        if (bf.number !== 4 || bf.wireType !== 2) {
          rebuiltBody.push(bf.raw);
          continue;
        }
        const inner = decodeMessage(bf.value);
        const rebuiltInner = [];
        for (const inf of inner) {
          if (inf.number === 1 && inf.wireType === 2) {
            rebuiltInner.push(writeLenDelimited(1, keyBytes));
          } else {
            rebuiltInner.push(inf.raw);
          }
        }
        const innerBytes = concatArrays(rebuiltInner);
        rebuiltBody.push(writeLenDelimited(4, innerBytes));
      }
      const bodyBytes = concatArrays(rebuiltBody);
      rebuiltTop.push(writeLenDelimited(54, bodyBytes));
    }
    return concatArrays(rebuiltTop);
  }

  function concatArrays(arrays) {
    let total = 0;
    for (const a of arrays) total += a.length;
    const res = new Uint8Array(total);
    let offset = 0;
    for (const a of arrays) {
      res.set(a, offset);
      offset += a.length;
    }
    return res;
  }

  // ---------------------------------------------------------------------------
  // Part Packager (Rebuild events & package zip)
  // ---------------------------------------------------------------------------

  async function buildPartZip(doc, partPages, partIndex, JSZipLib) {
    const newDocUuid = generateUUID();
    const oldDocUuid = doc.docUuid || '';

    // Collect universe of entities to keep
    const universe = new Set();
    const keptContents = new Set();
    const keptAttachments = new Set();
    const pageKeys = new Map(); // entityUuid -> newKey

    partPages.forEach((p, idx) => {
      universe.add(p.contentUuid);
      universe.add(p.entityUuid);
      keptContents.add(p.contentUuid);
      const newKey = `a${String(idx + 1).padStart(4, '0')}`;
      pageKeys.set(p.entityUuid, newKey);

      if (p.paperUuid) {
        universe.add(p.paperUuid);
      }
      for (const aId of p.paperAttachments) {
        universe.add(aId);
        keptAttachments.add(aId);
      }
    });

    const DROP_ACTIONS = new Set([55, 56, 57]);
    const keptEvents = [];

    const oldDocBytes = encodeUtf8(oldDocUuid);
    const newDocBytes = encodeUtf8(newDocUuid);

    for (const ev of doc.events) {
      let drop = false;
      for (const a of ev.topFields) {
        if (DROP_ACTIONS.has(a)) {
          drop = true;
          break;
        }
      }
      if (drop) continue;

      const s = ev.subject;
      if (s !== oldDocUuid && (!s || !universe.has(s))) {
        continue;
      }

      let rec = ev.raw;

      // Swap document UUID
      if (oldDocUuid && newDocUuid !== oldDocUuid) {
        rec = replaceAllBytes(rec, oldDocBytes, newDocBytes);
      }

      // Rewrite F54 order key
      if (ev.topFields.has(54) && s && pageKeys.has(s)) {
        rec = rewriteF54OrderKey(rec, pageKeys.get(s));
      }

      keptEvents.push(rec);
    }

    const outEventsPb = joinDelimited(keptEvents);

    // Build index.notes.pb
    const noteRows = [];
    for (const p of partPages) {
      if (doc.notesIndexRaw.has(p.contentUuid)) {
        noteRows.push(doc.notesIndexRaw.get(p.contentUuid));
      } else {
        const cBytes = encodeUtf8(p.contentUuid);
        const pBytes = encodeUtf8(`notes/${p.contentUuid}`);
        noteRows.push(
          concatArrays([writeLenDelimited(1, cBytes), writeLenDelimited(2, pBytes)])
        );
      }
    }
    const outNotesPb = joinDelimited(noteRows);

    // Build index.attachments.pb
    const attRows = [];
    for (const aId of keptAttachments) {
      if (doc.attachmentIndexRaw.has(aId)) {
        attRows.push(doc.attachmentIndexRaw.get(aId));
      }
    }
    const outAttachmentsPb = joinDelimited(attRows);

    // Build index.search.pb
    const searchRows = [];
    for (const p of partPages) {
      if (doc.searchIndexRaw.has(p.contentUuid)) {
        searchRows.push(doc.searchIndexRaw.get(p.contentUuid));
      }
    }
    for (const aId of keptAttachments) {
      if (doc.searchIndexRaw.has(aId)) {
        searchRows.push(doc.searchIndexRaw.get(aId));
      }
    }
    const outSearchPb = joinDelimited(searchRows);

    // Create new ZIP
    const outZip = new JSZipLib();

    // Copy kept notes & search
    for (const p of partPages) {
      const nPath = `notes/${p.contentUuid}`;
      if (doc.files.has(nPath)) outZip.file(nPath, doc.files.get(nPath));

      const sPath = doc.searchPaths.get(p.contentUuid) || `search/${p.contentUuid}`;
      if (doc.files.has(sPath)) outZip.file(sPath, doc.files.get(sPath));
    }

    // Copy kept attachments & paper search
    for (const aId of keptAttachments) {
      const aPath = doc.attachmentPaths.get(aId) || `attachments/${aId}`;
      if (doc.files.has(aPath)) outZip.file(aPath, doc.files.get(aPath));

      const sPath = doc.searchPaths.get(aId) || `search/${aId}`;
      if (doc.files.has(sPath)) outZip.file(sPath, doc.files.get(sPath));
    }

    // Put rebuilt indexes
    outZip.file('index.events.pb', outEventsPb);
    outZip.file('index.notes.pb', outNotesPb);
    outZip.file('index.attachments.pb', outAttachmentsPb);
    outZip.file('index.search.pb', outSearchPb);

    // Retain fixed entries
    if (doc.files.has('schema.pb')) outZip.file('schema.pb', doc.files.get('schema.pb'));
    if (doc.files.has('thumbnail.jpg'))
      outZip.file('thumbnail.jpg', doc.files.get('thumbnail.jpg'));
    if (doc.files.has('document.info.pb'))
      outZip.file('document.info.pb', doc.files.get('document.info.pb'));

    const zipBlob = await outZip.generateAsync({
      type: 'uint8array',
      compression: 'DEFLATE',
      compressionOptions: { level: 9 },
    });

    return {
      partIndex,
      pages: partPages,
      pageCount: partPages.length,
      startPage: partPages[0].pageNumber,
      endPage: partPages[partPages.length - 1].pageNumber,
      bytes: zipBlob.length,
      data: zipBlob,
    };
  }

  function replaceAllBytes(src, from, to) {
    if (from.length !== to.length) {
      throw new Error('Byte replacement requires equal length');
    }
    const res = new Uint8Array(src);
    for (let i = 0; i <= res.length - from.length; i++) {
      let match = true;
      for (let j = 0; j < from.length; j++) {
        if (res[i + j] !== from[j]) {
          match = false;
          break;
        }
      }
      if (match) {
        res.set(to, i);
        i += from.length - 1;
      }
    }
    return res;
  }

  // Export module
  const SplitterEngine = {
    parseGoodNotes,
    planPartitions,
    buildPartZip,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = SplitterEngine;
  } else {
    global.SplitterEngine = SplitterEngine;
  }
})(typeof window !== 'undefined' ? window : this);
