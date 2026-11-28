# kinship-htr-sidecar — portable per-image HTR metadata

Schema version **1.0.0** · writer/validator: `htr_sidecar.py` · tests:
`tests/test_sidecar.py`, `tests/test_legacy_import.py`.

One JSON file per original image, never one file for the corpus:

- **mirror mode (default):** `<runtime>/sidecars/<logical_path>.htr.json`,
  where `<logical_path>` = `<source_id>/<path under the source root>` —
  e.g. `sidecars/gavo/I-331-op3-d605/originals/И331Оп3Д605_0000.jpg.htr.json`.
  Symlinked roots (GAVO → T7 Shield) keep the *logical* path; the symlink is
  never resolved.
- **inline mode** (`sidecar_mode: "inline"`): `<original-filename>.htr.json`
  beside the source image. Originals are never modified either way.

Writes are atomic (temp file + `os.replace`): a reader never sees a partial
sidecar. Reprocessing preserves the previous valid revision under
`revisions/<name>.rev<N>.json` and bumps `recognition.revision`.

## Top-level structure

```jsonc
{
  "schema": "kinship-htr-sidecar",
  "schema_version": "1.0.0",
  "source":   { … identity of the original file … },
  "image":    { … geometry … },
  "recognition": { … provenance, status, timing … },
  "lines":    [ … spatial text … ],
  "content_group": { … sha256 dedup / duplicates … },
  "artifact_integrity": { "written_at": "…", "previous_revision": null }
}
```

### `source`

| key | type | meaning |
|---|---|---|
| `source_ref` | str | stable collection+case reference, e.g. `gavo-I331-3-605`, `cgamos:253`, `torrent-i18-1-131` |
| `page_id` | str | **globally unambiguous** page id (unique across collections), e.g. `g605-p000`, `253-p032`, `i18-1-131-p00001` |
| `case_ref`, `case_short`, `page_number` | | case identity and within-case number (DB `UNIQUE(case_short, page_number)`) |
| `original_filename` | str | file name as archived |
| `logical_path` | str | source-relative path (mirror key) |
| `image_path` | str | absolute path of the original at recognition/import time |
| `sha256` | str | content hash (trusted manifest value when available, else computed) |
| `size_bytes` | int\|null | file size; `null` only if the original is missing |
| `collection` | str | `gavo` / `torrent` / `cgamos-597` / `cgamos-misc` / `unknown` |
| `archive` | str\|null | holding archive |
| `fond`, `opis`, `delo` | str\|null | archival references — **null = unknown, never invented** |
| `scan_number` | int\|null | scan/page number within the archival unit |
| `source_id`, `root` | str | configured source id and root |

### `image`

`width`, `height` (original pixels, null only if unknown), `orientation`
(default `normal`), `coordinate_system` =
`"original-image-pixels,origin-top-left"`, `transformations` (list; empty —
no scaling/rotation is applied before recognition).

### `recognition`

`engine`, `engine_version`, `model`, `model_sha256`, `segmentation`,
`segmentation_sha256`, `segmentation_method`, `model_preset`, `patch`,
`device`, `recognized_at`, `duration_s`, `status`
(`ok` | `failed` | `duplicate`), `origin`
(`new` | `imported` | `deduplicated`), `config_fingerprint` (sha256 over the
recognition configuration — dedup key), `revision`, `error` (failed pages:
diagnostics are preserved here *and* in the DB), plus for imports:
`import_validation` ∈ {`verified-sha256`,
`mismatch-requires-verification`, `missing-original-requires-verification`,
`unverifiable-requires-verification`} and `reused_from_benchmark`.

### `lines[]`

| key | meaning |
|---|---|
| `line_id` | stable within the recognition revision: `<page_id>-r<revision>-<nnnn>` |
| `line_number` | original recognition line number — **stable review key** (DB reviews remap by `(page_id, line_number)`) |
| `region_id` | null (Polyscriptor output carries no reliable region ids) |
| `reading_order` | 1-based reading order |
| `text_raw` | diplomatic raw recognition — pre-reform orthography preserved, never modified |
| `text_norm` | normalized copy for retrieval only (see `htr_search/normalize.py`) |
| `bbox` | **`[x_min, y_min, x_max, y_max]` in original-image pixels** (`bbox_convention: "xyxy-original-image-pixels"`) |
| `polygon`, `baseline` | **null** — this pipeline's PAGE XML coordinates are degenerate (2-point); nothing is invented |
| `confidence` | model confidence 0..1 or null |
| `verification_status` | `unverified` for machine output; DB `reviews` are authoritative for human readings |
| `source` | `blla-ocr` (main pass) \| `stage-b-gap` (gap recovery) |

Convention note: Polyscriptor/transcription JSON emits `[x, y, w, h]`;
`htr_sidecar.xywh_to_xyxy` converts on import/recognition, the DB converts
back to `[x, y, w, h]` for the historical server/UI consumers. Both
directions are tested (`tests/test_sidecar.py`, `tests/test_db_rebuild.py`).

### `content_group`

`sha256`, `canonical_page_id`, `duplicate_of` (null unless this file's
content is byte-identical to an already-indexed page — then `status` is
`duplicate`, no lines are stored, and the canonical page id is recorded;
every distinct archival source reference keeps its own sidecar).

## Rebuild rule

The DB (`htr_search.db`) is a **derived index**: it can be rebuilt from the
sidecar tree alone, without rerunning HTR, and human `reviews` survive every
rebuild via the `(page_id, line_number)` key:

```bash
htrbench-python htr_worker.py build        # = python -m htr_search.db
```
