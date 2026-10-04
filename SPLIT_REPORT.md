# GoodNotes Splitter — Final Report (v3, verified active pages & order)

Tool: `goodnotes_splitter/` (Python 3.13, stdlib only)
Input: `名称未設定 (1).goodnotes` (26,178,717 B, schema 35, 62 active pages)
Output: `out5/part-001..003.goodnotes` (all validated, device-order confirmed)

## Result

| part | source pages | generated pages | byte size | MB | limit 5 MiB | status |
|------|-------------|-----------------|-----------|----|-------------|--------|
| 001  | 1–51        | 51              | 4,075,288 | 4.08 MB | OK | ALL VALID |
| 002  | 52–61       | 10              | 4,000,153 | 4.00 MB | OK | ALL VALID |
| 003  | 62          | 1               |   564,624 | 0.56 MB | OK | ALL VALID |

- **Total pages**: 51 + 10 + 1 = **62 pages** (matches the 62 active pages in the source document).
- **Size constraint**: All parts strictly below 5.0 MiB (5,242,880 bytes).
- **Validation**: 16/16 checks pass across all parts (ZIP integrity, size limit, schema v35, note/search/attachment parity, graph references, monotonic order keys `a0001`..).

## Key Discoveries & Root Causes

1. **Active Page Count (62 pages vs 78 index entries)**:
   - `index.notes.pb` contains 78 rows from historical creation.
   - However, the document contains 16 `F56` (page-deleted) records.
   - The native GoodNotes app only displays the **62 active pages**; deleted pages were previously being packed into splits, causing mismatches with what users see on their devices.
2. **Display Order Determination**:
   - GoodNotes sorts pages on import by the ASCII dictionary order of each page's latest order key.
   - Creation keys in `F54` are updated by user reorders via `F55` (move records).
   - `reader.py` now resolves active pages by applying `F55` key updates, filtering `F56` deletions, and sorting strictly by the latest order key.
3. **Event Preservation**:
   - `writer.py` preserves all necessary lifecycle events for kept pages (`F2` paper def, `F6` attachment def, `F102` strokes, `F105` page links), preventing blank/unrendered pages.
   - Each part generates fresh, strictly increasing order keys (`a0001`, `a0002`...) across its pages so GoodNotes imports them in exact order.

## Device Verification Proof
- Verified on physical iPad using `test_parts/`:
  - `1234_part1.goodnotes`: displayed handwriting and paper in order **2 → 1**.
  - `1234_part2.goodnotes`: displayed handwriting and paper in order **4 → 3**.
