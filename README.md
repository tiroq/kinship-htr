# kinship-htr — local historical Russian HTR: benchmark + incremental indexer

Fully local pipeline for recognizing Russian archival scans (pre-reform
orthography preserved) and keeping them in a portable, rebuildable,
searchable corpus. No cloud OCR, no paid APIs, no external services.

Two layers of documentation:

- `REPORT.md` — the original benchmark/recovery/research report (historical
  evidence: ~24 s/page on Apple M4 CPU, 4-worker stall of 2026-11-21, 327/329
  CGAMOS pages + 177 GAVO pages already recognized).
- this README — installation, configuration, and operations of the
  **incremental indexer** (`htr_worker.py`).

## Components

| Path | Role |
|---|---|
| `htr_config.py` + `htr-index.example.json` | single-file JSON configuration (stdlib only) |
| `htr_inventory.py` | source discovery, stability confirmation, SHA-256/dims capture |
| `htr_sidecar.py` | portable sidecar schema 1.0.0 (writer/validator) — `docs/htr-sidecar-schema.md` |
| `htr_import.py` | pre-existing recognition artifacts → sidecars (no HTR rerun) |
| `htr_worker.py` | continuous worker CLI: inventory/import/build/process/daemon/status/stop/retry/overlay |
| `htr_monitor.py` | read-only progress & resource monitor |
| `stage_a_runner.py` | hardened recognition slice (reused by the worker: atomic checkpoints, per-page heartbeats, watchdog timeouts) |
| `htr_search/` | SQLite FTS5 + trigram + RapidFuzz search, CLI, HTTP UI |
| `tests/` | self-contained tests (synthetic data) + known-positive regressions |

Storage:

- **Layer A — portable sidecars**: `<runtime>/sidecars/<logical path>.htr.json`
  (mirror mode; `inline` mode writes beside originals). Schema:
  `docs/htr-sidecar-schema.md`.
- **Layer B — unified DB**: `work/index/htr.db` (SQLite, WAL). Rebuildable
  from sidecars alone; human `reviews` survive rebuilds.

## Environment (reuse, do not reinstall)

Native ARM64 conda env **`htrbench`** (Python 3.12, torch 2.14.0, kraken
7.1.1, rapidfuzz, pillow):

```bash
HP=/Users/mysterx/miniconda3/envs/htrbench/bin/python   # "htrbench-python"
```

Pinned versions and model hashes: `env-versions.txt`
(`crnn-ctc-russian/best_model.pt` sha256 `74482bd0…553dd32`,
`pagexml/blla.mlmodel` sha256 `77a638a8…86056`). Models are already cached
locally — nothing is downloaded again.

### Polyscriptor checkout and patch

- Repo `https://github.com/achimrabus/polyscriptor.git`, commit
  `52524ed1f7b32c3b983e6533c442cbe197e34fb0`, checked out at `polyscriptor/`
  (git-ignored).
- Patch: **`polyscriptor-local.patch`** — min line height 40→15 px + 16 px
  width guard. Historically this file also existed as `bench-patch.diff`
  with identical content; it was consolidated into `polyscriptor-local.patch`
  (no second divergent copy; `tests/test_polyscriptor_patch.py` pins that the
  working tree equals the patch and that it is applied exactly once).
- Apply (only if the working tree is clean):

```bash
git -C polyscriptor apply ../polyscriptor-local.patch
```

## Configuration

One JSON file, standard library only:

```bash
cp htr-index.example.json htr-index.json    # gitignored; edit paths
```

Resolution order: `$HTR_INDEX_CONFIG` → `<repo>/htr-index.json` → built-in
defaults (which reproduce the historical hard-coded paths). Keys: source
roots (`sources[]` with `include` globs, `manifest` type, `priority`),
`runtime_root`, `db_path`, `python_env`, `polyscriptor_dir`, resource limits
(`threads`, `workers`, `nice`, `page_timeout_s`, `poll_interval_s`,
`round_pages`, `min_free_gb`, `max_attempts`), `sidecar_mode`.

Sources configured by default:

| id | root | contents |
|---|---|---|
| `gavo` | `~/Downloads/kinship-gavo` (symlink → T7) | ГАВО И331 оп.3 д.605–617,620 — per-case `manifest.csv`+`metadata.json` trusted for completion |
| `torrents` | `/Volumes/T7 Shield/Kinship/torrents` | ГАВО torrent payloads (`.part` files ignored until stable) |
| `cgamos-597` | `kinship-case/sources/cgamos-online` | ЦГА Москвы ф.597 оп.1 д.253/255/256/258 originals |
| `cgamos-misc` | same root | opis/finding-aid scans (lowest priority) |

Discovery rules: two unchanged observations (size+mtime, `poll_interval_s`
apart) ⇒ stable; GAVO cases whose `metadata.json` shows
`downloaded == expected == jpegVerified` are trusted immediately.
AppleDouble `._*`, `.DS_Store`, `*.part`, thumbnails, `crops*/`, `review/`,
`sheets/` are never treated as originals. Originals are only ever read.

## Worker operations (copy-paste)

All commands run from the repo root with the htrbench interpreter.

```bash
HP=/Users/mysterx/miniconda3/envs/htrbench/bin/python

# START (detached, lock-guarded, kept awake via caffeinate)
$HP htr_worker.py daemon &            # or: nohup … >> work/indexer/logs/daemon.log 2>&1 &

# STATUS / monitor (read-only; same report)
$HP htr_worker.py status
$HP htr_monitor.py --json

# STOP (graceful: finishes the sidecar/DB step, leaves pages resumable)
$HP htr_worker.py stop

# RESUME — same start command; completed pages are never redone
$HP htr_worker.py daemon &

# one-shot inventory / import / single processing round
$HP htr_worker.py inventory
$HP htr_worker.py import --db
$HP htr_worker.py process --limit 10

# inspect errors
tail -50 work/indexer/logs/progress.jsonl        # structured events
$HP htr_monitor.py | sed -n '/Recent errors/,$p'

# retry failed/parked pages
$HP htr_worker.py retry --all --clear-markers

# rebuild the DB from sidecars (no HTR)
$HP htr_worker.py build
```

PID: `work/indexer/worker.pid` · lock: `work/indexer/worker.lock` (duplicate
instances prevented) · heartbeats: `work/indexer/heartbeat/{daemon,w0}.json` ·
progress: `work/indexer/logs/progress.jsonl`.

### Resource protection (default = conservative)

1 worker, 1 inference thread, `nice 10`, per-page timeout 900 s with
external watchdog (stale heartbeat ⇒ SIGKILL + mark failed + respawn, ≤3),
disk floor 5 GB, ≤3 attempts per page then parked (visible in `status`,
re-arm with `retry`). Never raise `workers` above 1 without measurements —
the historical 4-worker run stalled under ~11 GB swap (REPORT §10).

### Priority & fairness

Import first → GAVO → torrents → cgamos-597 → misc, round-robin across
sources so no backlog starves another; images discovered in the last 6 h go
to the front of their source's queue. Pages are processed only when stable
(and decode-checked); in-progress downloads are skipped by the stability
rule, and the downloader's files/locks are never touched.

## Search

```bash
$HP -m htr_search.cli "Волченки" --profile Волченки --limit 50
$HP -m htr_search.cli "Головкин" --fond 597 --delo 255
$HP -m htr_search.cli "Михайловка" --collection gavo --exact
$HP -m htr_search.cli "Шамраев" --json out.json --log-run
$HP -m htr_search.server 8765         # http://127.0.0.1:8765/
```

Filters: `--case`, `--collection`, `--fond`, `--opis`, `--delo`, `--page`,
`--exact` (normalized phrase), `--profile` (documented spellings +
generated inflections + OCR-confusion corruptions), `--multiline`.
Profiles include Волченки/Волченокъ, Головкин(ъ/а), Ефим/Евфимъ, Шамрай/
Шамраев/Шамраи, Михайловка, Протасьево, Демидов, Иванов/Петров/Симеонов…
(`htr_search/profiles.py`).

Every hit is labeled: `evidence_class` = `machine-raw` / `machine-normalized`
/ `machine-fuzzy` / `human-verified`, and `lead_status` = **`SECONDARY-LEAD`**
for every machine reading (only a human `confirmed` review is not). Line
neighbors, page context, bbox + original image path are returned;
`/api/image/<page_id>?highlight=<line_id>` draws the matched line on the
original scan. Absence of hits is never evidence of absence.

## Tests

Self-contained (synthetic data, no archival datasets):

```bash
$HP tests/test_sidecar.py            # schema, bbox conversion, atomicity
$HP tests/test_inventory.py          # discovery, stability, identities
$HP tests/test_legacy_import.py      # artifact import + provenance flags
$HP tests/test_db_rebuild.py         # rebuild, FTS, review preservation
$HP tests/test_search_filters.py     # filters, exact, evidence labels, profiles
$HP tests/test_polyscriptor_patch.py # patch applied exactly once, consolidated
```

Regressions (need the local index `work/index/htr.db`):

```bash
$HP tests/test_known_positive_258p014.py   # 3/3
$HP tests/test_kinship_search02.py         # classifier rules
```

Integration checks that need real archival data (pilot): §14 of the task —
sidecar validation over real imports, overlay alignment, restart/resume,
new-image discovery, rebuild-from-sidecars — are executed against the live
corpus and reported in the PR; they are not part of the self-contained suite.

## Recovery

- Worker crash/interrupt: restart `daemon`; completed pages are never redone
  (sidecar + DB row = done), interrupted pages resume from their absent
  checkpoint, sidecar↔DB gaps are reconciled automatically each round.
- Broken checkpoint: `meta.json` is written last (atomic); a page is either
  fully valid or absent. Failed pages keep diagnostics in the sidecar and
  `pages.error`.
- Corrupt/missing sidecar: regenerate via `htr_worker.py import` (legacy) or
  rebuild recognition for that page (`retry --clear-markers`).
- Full DB loss: `htr_worker.py build` reconstructs everything from sidecars
  without rerunning HTR; reviews are remapped by `(page_id, line_number)`.

## History

- `bench-patch.diff` and `polyscriptor-local.patch` were byte-identical;
  consolidated into `polyscriptor-local.patch` (this README + the patch test
  preserve the historical reference; `REPORT.md` keeps its original wording).
- Benchmark, stall recovery, and research evidence: `REPORT.md`.
