# KINSHIP-HTR-BENCH-01 — Local Historical Russian HTR Feasibility Report

Date: 2026-11-21 · Workspace: `/Users/mysterx/dev/kinship-htr-bench` (isolated; the
`kinship-case` repository was not modified — originals untouched, no records changed,
no cloud OCR calls, no paid APIs, no bulk processing: 8 of 329 scans).

## 1. Verdict

**The existing open-source stack is sufficient for a line-level HTR benchmark on this
material. No custom integration is needed beyond one small patch (§4.4).**
polyscriptor (batch CLI) + Kraken blla segmentation + `achimrabus/crnn-ctc-russian`
ran end-to-end, fully local, on 8 representative pages at ~24 s/page on an M4 CPU.
The real bottleneck is now **ground truth**, not tooling: CER is deliberately NOT
reported until a human verifies the reference lines via the review interface (§6).

## 2. Environment (native ARM64 — required)

Host is an Apple M4 whose shell/toolchain runs under Rosetta (reports x86_64).
PyTorch ≥ 2.3 ships **no macOS x86_64 wheels** and polyscriptor's pinned stack
(kraken 7.1.1 → torch ≥ 2.9) is only installable on arm64. omp's managed
Chromium/WebKit (x64 builds) also failed to launch under Rosetta.

- conda env `htrbench`: Python 3.12.13, arm64
- torch 2.14.0, torchvision 0.29.2, kraken 7.1.1, transformers 5.12.1
- polyscriptor git `52524ed` (clone, CPU-only requirements)
- Full pins + SHA-256: `env-versions.txt`
  - `crnn-ctc-russian/best_model.pt` sha256 `74482bd0…553dd32` (43.4 MB)
  - `blla.mlmodel` (shipped in polyscriptor `pagexml/`) sha256 `77a638a8…86056`

## 3. Page selection (8 pages, 2 per archival case)

Cases = ЦГА Москвы ф. 597 оп. 1 (Верейское духовное правление, исповедные ведомости
села Протасьева): д. 253 (1813–19), 255 (1820–24), 256 (1825–29), 258 (1830–34);
329 scans total in `kinship-case/sources/cgamos-online/<case>/originals/`.
Selection via labeled contact sheets (archival cover sheets excluded):

| Case | Typical | Hard | Hard-page challenge |
|---|---|---|---|
| 253 | p032 | p003 | binding/gutter holes, compressed script |
| 255 | p003 | p027 | density, torn left margin |
| 256 | p050 | p003 | blue paper, faded ink |
| 258 | p003 | p049 | max density, marginalia |

Staged copies (originals untouched): `work/pages/`.

## 4. Compatibility findings

1. **Rosetta trap**: everything host-side defaults to x86_64 → torch/kpinned stack
   uninstallable. Resolution: native arm64 miniconda env. Any future bulk run must
   stay native.
2. **polyscriptor `--model-id` is ignored** by the CRNN-CTC engine (`load_model`
   reads only `model_path`). Resolution: pass the preset name
   `--model-path "Russian (6.92% CER)"`, which `hf_hub_download`s from
   `achimrabus/crnn-ctc-russian` and caches locally.
3. **polyscriptor `kraken` segmentation = classical pageseg; `kraken-blla` is the
   neural path** and ships with the repo (`pagexml/blla.mlmodel`). Use
   `--segmentation-method kraken-blla`.
4. **BENCH-PATCH (`bench-patch.diff`)**: stock `min_height_for_cnn = 40` silently
   dropped ~70% of blla lines (confessional-table handwriting is 30–35 px tall):
   pilot 253-p032 found 200 blla lines → 57 transcribed. Patch: height ≥ 15 px +
   width ≥ 16 px guard (also prevents a `max_pool2d "output size too small"`
   crash from degenerate 1–4 px crops; the engine catches per-line errors and the
   batch continues). After the patch: 199/195 lines on the same pages.

## 5. Actual inference results (evidence)

Run: `./run_bench.sh work/pages work/out-8pages` (reproducible, same flags).

- 8/8 pages, **1567 lines, 14 390 characters, avg model confidence 84.6%,
  190 s total (23.7 s/page CPU), 0 errors.**
- Per-page: `work/out-8pages/batch_results.csv`
  (line counts 189–199, confidence 0.79–0.91).
- Artifacts per page (`work/out-8pages/transcriptions/<page>.{txt,csv,json}`):
  line text, confidence, bbox `[x,y,w,h]`; plus `page_xml/<page>.xml` (PAGE XML
  with coordinates); full log `work/out-8pages/batch_processing.log`.
- Visual review: bbox overlays for all 8 pages in `work/review/overlays/`.
- Segmentation behavior (from overlays): main handwritten rows of both pages of
  each spread covered (~85–90% of rows); printed header rows and far-margin
  totals are NOT segmented; occasional ascender/descender bleed between dense
  rows; region splitter subdivided wide regions into sub-columns.

Sample raw model output (253-p032, uncorrected — illustrates current quality):
```
невени Его анна Смнови          (≈ «жена его Анна Симонова»)
20 дети их Китьтина
24 вена Его издотяй агибронова 36
```
The model recognizes the register's formulaic vocabulary («жена его», «дѣти ихъ»,
names, ages) but confuses graphically similar letters and old orthography; exact
rates await human-verified reference lines (§6). Watermark overlays
(«ГЛАВАРХИВ МОСКВЫ») are present on all scans and did not block segmentation.

## 6. Provisional references and human verification (CER protocol)

**No AI output (CRNN model, vision-model drafts, or otherwise) is treated as
ground truth.** Status:

- 112 lines (14/page) selected → `work/selection.json`;
- AI vision drafts produced per line (provenance explicitly
  `vision-draft (AI-assisted, UNVERIFIED)`) — drafts only, to speed human entry;
- all 1567 line crops + payload: `work/review/` (`crops/`, `payload.json`);
- **review interface: `work/review/review.html`** (verified working in Chrome;
  a persisted tab is open). Workflow per line: see crop → accept/edit the
  diplomatic reference (pre-reform orthography, as written) → tick «проверено
  человеком» → `Export reviewed.json` when done (progress autosaves locally).
- After export: `metrics.py reviewed.json out/metrics` computes **strict
  diplomatic CER** (no normalization; ѣ/і/ѳ, case, punctuation all count) plus
  **genealogy-field heuristics** (name-token accuracy, digit-group accuracy),
  scoring only `verified=true` lines. Smoke-tested end-to-end (synthetic 2-line
  case). **CER numbers are intentionally absent from this report** until that
  human verification happens.

Target: 80–120 verified lines (112 drafted; even 80 verified is enough for a
first CER estimate with per-line breakdown).

## 7. Comparison interface for additional engines

Same-line protocol: every engine consumes the SAME blla segmentation via
PAGE XML —
`batch_processing.py --use-pagexml --xml-folder work/out-8pages/page_xml
--segmentation-method none --engine <engine> …`
then `compare.py <runA> <runB> out.md` joins per-line outputs by (page, line).
This removes segmentation variance from engine comparisons. Candidate second
engine: Kraken's own recognition model (e.g. a Russian kirchenzettel/HTR
`.mlmodel`) or TrOCR-ru via the same CLI.

## 8. Limitations

- Line-level HTR only: table semantics (age columns, confession marks) are lost
  in flat-line reading; genealogy-field extraction will need column-aware
  post-processing (bbox x-coordinates are retained, so column assignment by
  x-bin is possible).
- Printed table headers are not segmented by blla (region type mismatch).
- 256-p003 (blue paper) and 253-p003 (gutter damage) will likely anchor the
  upper error range; page-level CER stratification is in `metrics.py` output.
- Vision drafts in the review UI are often wrong («рвна его» for «жена его») —
  they are typing aids, not candidates for acceptance without reading the crop.
- CPU speed (24 s/page) is fine for benchmarks; a full 329-scan run would be
  ~2.2 h single-threaded and is NOT authorized here.

## 9. Recommendation for the next benchmark stage

1. Human-verify the 112 drafted lines in `review.html` (est. 1–2 h for a reader
   of pre-reform script), export `reviewed.json`, run `metrics.py` → first
   reportable CER + field metrics.
2. If CER is dominated by segmentation bleed (likely on the two hard pages),
   add a column-x-bin post-processing step rather than retraining anything.
3. Run one comparison engine through the PAGE-XML protocol (§7) to separate
   recognition error from segmentation error.
4. Only then decide about extending to more pages / a bulk run — still fully
   covered by the existing tools; **no custom integration is warranted**.

## Evidence index

| Artifact | Path |
|---|---|
| Runner (exact command) | `run_bench.sh` |
| Env pins + model hashes | `env-versions.txt` |
| Polyscriptor patch | `bench-patch.diff` (git `52524ed`) |
| Batch outputs (txt/csv/json/PAGE XML/log) | `work/out-8pages/` |
| Pilot (pre-patch, documents the 40 px filter loss) | `work/out-pilot2/` |
| Line crops (1567) + overlays + payload | `work/review/` |
| Selected 112 lines + AI drafts | `work/selection.json` |
| Page selection contact sheets | `work/sheets/`, `work/draftsheets/` |
| Crop exporter / review builder / metrics / compare | `export_line_crops.py`, `build_review.py`, `metrics.py`, `compare.py` |

## 10. Stage A recovery (KINSHIP-HTR-SEARCH-01, 2026-11-21/22)

### 10.1 Incident evidence

The full-corpus Stage A run (4 workers, 80 pages/slice) stalled silently:

- `work/index/logs/run_full.log` stops mid-segmentation at **17:06:21** and never
  receives another byte, while workers kept writing `meta.json` until 17:25 —
  the console redirect died mid-run; the old runner had no protection against a
  lost console (`print()` raising inside the page loop would have marked
  finished pages *failed*).
- Per-worker DEBUG logs (`work/index/ocr/_tmp_worker*/batch_processing.log`) all
  end mid-recognition ("Processing N lines…") within **41 s of each other**
  (17:24:14–17:24:55), no exceptions, no `slice done`. All four workers hung
  simultaneously; the sample profiler could not capture stacks.
- System state: **10.95 GB of 12 GB swap in use**, 44% RAM free at inspection
  (after the pool was gone). **Root cause undetermined** — no OOM kill, no
  traceback, no kernel evidence; consistent with a system-wide memory-pressure
  stall but not provable from the artifacts.
- The old parent used `pr.join()` with no timeout: a hung child blocks resume
  forever. The pages at the head of the resume queue (255-p026/028/030/031)
  correspond to the in-flight pages the workers were killed on.

### 10.2 Checkpoint audit (before recovery)

329 manifest pages: **128 status=ok, 0 failed, 201 pending**. Every ok page had
all four artifacts present, non-empty, and decodable (JSON/XML parsed). No
corruption, no half-written outputs. The 8 bench8-reused pages lack
`stage_b.json` by design (gap recovery was benchmark-scope-only) — indexing
treats it as optional, so they stay valid and were **not** recomputed.

### 10.3 Hardening applied to `stage_a_runner.py`

1. **External per-page timeout**: workers heartbeat per page to
   `work/index/heartbeat/wK.json`; the parent watchdog replaces the old
   unconditional `join()`, SIGKILLs a worker whose heartbeat is stale beyond
   `--page-timeout` (default 900 s vs ~25 s/page observed), marks the in-flight
   page failed (re-runnable via `--redo`), and respawns the worker on its
   remaining pages (`--max-restarts` cap).
2. **Atomic per-page checkpoints**: `meta.json` (completion marker, written last)
   and `stage_b.json` via tmp+`os.replace`; a kill at any instant leaves a page
   fully valid or fully absent. Startup `audit_ok()` demotes any ok page whose
   artifacts are missing/empty/corrupt so it recomputes.
3. **Progress heartbeat**: structured events (worker start/page ok/timeout/
   respawn/60 s beats) append to `work/index/logs/progress.jsonl`, independent
   of the console.
4. **Lost-console safety**: `safe_print()` swaps stdout to /dev/null after the
   first `OSError` — a broken console can no longer poison checkpoints.
5. **Thread bounds**: OMP/MKL/OpenBLAS/Accelerate set in the parent before
   spawn (effective at library load), `torch.set_num_threads/_interop_threads`
   bounded per child (`--threads`, default 1); resume runs use
   `--workers 1` (recovery command: `OMP_NUM_THREADS=1 htrbench-python
   stage_a_runner.py --workers 1 --threads 1`).
6. Search-CLI bugfix: `htr_search.search.Searcher._reviews_for` was referenced
   but missing (crash on every query); implemented against the `reviews` table.

### 10.4 Recovery verification

- Recovery test (`--limit 5`, workers=1, OMP=1): **5/5 consecutive pages OK**
  (255-p026→p032, 15–25 s each) — these include the four pages the old workers
  were stuck on. `work/index/logs/recovery_test.log`.
- Index rebuilt (`htr_search.db`): 133 pages ok, all 5 new pages present with
  full line counts and FTS-searchable (`htr_search.cli "жена его"` returns hits).
- Full-corpus continuation then launched under the same conservative flags
  (`work/index/logs/run_corpus.log`, progress events in `progress.jsonl`).
- **Final result**: `ok=327/329, failed=2` — 256-p001 and 258-p001 (dense scans,
  blla found no regions and the classical fallback no lines; recorded, not
  lost; re-runnable with `--redo`). During the continuation the failure-path
  bugs that let a single bad page crash both worker and watchdog were fixed
  and verified. Final index: 327 pages, **56 738 OCR lines**, FTS5+trigram
  rebuilt, `missing_from_index` empty, `htr_search.cli` returns hits across
  all four cases. Stage A is complete; no OOM was ever evidenced.

| Recovery artifact | Path |
|---|---|
| Incident logs (stall) | `work/index/logs/run_full.log`, `work/index/ocr/_tmp_worker*/batch_processing.log` |
| Hardened runner | `stage_a_runner.py` |
| 5-page recovery test log | `work/index/logs/recovery_test.log` |
| Watchdog/progress events | `work/index/logs/progress.jsonl` |
| Corpus continuation log | `work/index/logs/run_corpus.log` |

## 11. Stage D/E: discovery searches and review workflow (2026-11-22)

Index at start: 329 pages (327 ok, 2 failed), **56 738 OCR lines**.  Searches run
with the three profiles (documented spellings + generated inflections +
1-2-edit confusion corruptions) over the FTS+trigram+RapidFuzz stack.

### 11.1 Candidate counts and top results (details in `work/discovery/*.json`)

| Target | candidates ≥ min_score | strong | fuzzy | top strong hits |
|---|---|---|---|---|
| Волченки | 5000 (cap) | 5 | 4995 | 258-p014 «…деревни Волченокъ крестьян-», 258-p068 «Волченокъ», 255-p077, 255-p026, 253-p080 |
| Протасьево | 5000 (cap) | 14 | 4986 | 255-p064 «…Села Протасвева Церкви» (heading), 256-p021 «Съ протасьевъ», 253-p074, 253-p035, 256-p058 |
| Головкин | 5000 (cap) | 3 | 4997 | 255-p030 «Екетеринъ Алексинуро вной Головкиной» (≈ графиня Е. А. Головкина), 253-p045 «…Головкина», 255-p016 «головниной дерсьни…» |

Ranking is two-tier: `strong` = line text contains a documented spelling or a
generated inflection (or ≥90 similarity on a line long enough to carry the
name); `fuzzy` = corruption-variant recall, ranked below strong.  Raw
partial_ratio alone let 2-3-char OCR fragments («Ата», «ино») outscore real
mentions — the tiering fixes that.  Textual similarity and OCR confidence are
reported as separate fields in every export (confidence is only a capped ≤3-pt
hint in the total).  **All OCR text is unverified: every candidate is a lead.**

### 11.2 Failed pages resolved

256-p001 and 258-p001 are the **modern ЦГА Москвы archival cover sheets** of the
two files, rotated 90°, printed forms with fond/opis/ed.-khr. numbers only — no
parish-register content (AI-assisted visual inspection + 26-28% dark-pixel
statistics).  An alternative segmentation pass was therefore **not
justified**; the failures stay explicit and explained.

### 11.3 UI verification (API-level, real indexed data)

Verified: profile/case/review filters; line detail with 8 neighbors;
`/api/image` byte-identical to the original scan; `/api/crop` bbox alignment
pixel-checked (dims match bbox+pad, mean |Δ| 2.7/255 = JPEG re-encode only);
`confirmed` without verified text rejected (400); review POST → SQLite →
visible via `/api/line`, `/api/stats`, `review_filter=reviewed`; JSON/CSV
export with source SHA-256.  One honest smoke-test review was recorded
(`uncertain`, notes explicitly marked "WORKFLOW SMOKE TEST by AI assistant —
NOT a human verification") and exported to `work/evidence/reviews-smoke-test.json`.
No human confirmations were fabricated; no OCR match is treated as verified
fact.  Front-end `renderResults` was missing its loop openers (script would
not parse — UI dead); fixed and node-checked.  Browser automation was
impossible under Rosetta (REPORT §2), so visual confirmation was left to the
user's default browser (opened live); all data-path checks above are
programmatic.

Bugs fixed en route: `Searcher.log_run` column count mismatch;
`pages.width/height` NULL (now from the manifest); reviews now remapped by
(page_id, line_number) on index rebuild instead of being silently orphaned.

### 11.4 Settlement-heading coverage

59 pages have no OCR line in the top 12% of the page.  Sampled 3 (AI-assisted
visual): the band contains printed table headers («Число / Лѣта отъ рожденія /
Показаніе дѣйствія») and page numbers — not settlement names.  Real parish
headings in this corpus sit at y≈0.08–0.26 and **were captured**
(255-p046, 255-p064, 256-p019).  The 59-page queue is saved as a human
spot-check fallback: `work/discovery/heading-review-queue.json`.

### 11.5 Open the interface

```bash
/Users/mysterx/miniconda3/envs/htrbench/bin/python -m htr_search.server 8765
# → http://127.0.0.1:8765/   (search: Волченки / Протасьево / Головкин + profile)
```

### 11.6 Blind spots (explicit)

- OCR of body text is unverified; headline candidates must be human-read in
  the review UI before any genealogical claim.
- Recall cap: candidate pools hit the 5000-line scorer limit per target.
- 2 cover pages carry no OCR; 59-page top-band queue unreviewed by a human.
- Vision-based page inspection is AI-assisted and disclosed; it is not OCR and
  not ground truth.

## 12. Systematic name search across the corpus (2026-11-22)

Profiles extended (`htr_search/profiles.py`): Егоров, Царев, Демидов,
Мефодиев + low-priority Иванов/Петров/Симеонов (Головкин already existed).
Historical spellings (Егоровъ, Царёв, Демидовъ, Мефодьев…) are preserved for
display; normalized variants (incl. 1-2-edit OCR confusions) drive retrieval
only.  Pipeline: `name_search.py` → `work/discovery/names/<Profile>.json`.

### 12.1 деревня Волченокъ sections per case (`volchenok_sections.py`)

Derived from the village-formula headers «Той же вотчины деревни N крестьяне…»
(formula line + same-y-band name fragment; two register pages share one scan;
sections run to the next village/year header).  UNVERIFIED OCR readings:

| Case | Pages | Header reads | Status |
|---|---|---|---|
| 253 | 80–97 | «волченой» | confirmed-form (1-edit; clerical variant «Волченойъ», verify visually) |
| 255 | 26–36 | «волченокъ» | confirmed |
| 255 | 42–54 | «валченогъ» | likely (same village assumed) |
| 255 | 77–82 | «Волченокъ» | confirmed |
| 256 | 16–19 | «волчьникъ» | possible — may be another village |
| 256 | 53–58 | «Во2ченок» | likely |
| 258 | 14–20 | «Волченокъ» | confirmed |
| 258 | 32–43 | «Волченойъ» | likely |
| 258 | 68–73 | «Волченокъ» | confirmed |

Stored with header crops and evidence: `work/discovery/volchenok-sections.json`.

### 12.2 Candidate counts and strongest matches (all UNVERIFIED OCR leads)

| Profile | ≥ min_score (cap) | strong | strong in Волченокъ | headline leads |
|---|---|---|---|---|
| Егоров | 5000 | **104** | 17 | 255-p052 «Симионъ Егоровъ» · 258-p016 «жена его Лукерзъ Егорова» · 258-p043 «сени Его Китерина Егоров» |
| Царев | 5000 | **0** | 0 | no confident form; 30 lines read «Фецор(ов)ъ» — most plausibly misOCR of «Феодоровъ» (common patronymic), kept as fuzzy leads only |
| Головкин | 5000 | **3** | 1 | 255-p030 «Екетеринъ Алексинуро вной Головкиной» (≈ графиня Е. А. Головкина, владелица) · 253-p045 «…Головкина» |
| Демидов | 5000 | **0** | 0 | absent from OCR text (or corrupted beyond retrieval) |
| Мефодиев | 5000 | **0** | 0 | absent from OCR text |
| Иванов | 5000 | **623** | 100 | low priority (common); in-section hits e.g. 255-p077 «…тихокъ ивановъ…» |
| Петров | 5000 | **251** | 49 | 258-p014 «172 ена его Марфа Петрова» · 258-p015 «538 женаего Марина Петрова» |
| Симеонов | 5000 | **54** | 11 | 258-p016 «Михаилъ Симеоновъ» · 258-p015 «жена его афимы Симеонова» · 258-p034 «Михаилъ Симеоновъ» |

Ranking: strong tier above fuzzy; inside-Волченокъ above outside.  Every
candidate export carries bbox, OCR text, ocr_confidence separate from textual
similarity, ±6-line household context (relation words «жена/сын/дети/ихъ…»
and numeric-age lines marked), patronymic-vs-surname annotation, page groups
(no identity merging), and review links.

### 12.3 Patronymics vs hereditary surnames

In 1813–34 peasant confession registers -ов/-ев/-ина forms are usually
patronymics; proven hereditary surnames are rare.  The pipeline annotates
every -ов/-ева hit as «patronymic-or-surname» with household context for a
human to adjudicate — it never auto-decides, and repeated names are grouped
by (page, section) for review only, never merged into identities.

### 12.4 Limitations

- 5000-candidate scorer cap per profile may hide low-total matches.
- Царев/Демидов/Мефодиев: zero confident OCR occurrences — either absent or
  corrupted beyond current recall; the two failed cover pages and the
  59-page top-band queue remain human-review blind spots.
- «Волченойъ»/«валченогъ»/«волчьникъ» header readings and all section ranges
  are OCR-derived and need visual confirmation in the review UI.
- No OCR match here is a verified person; no kinship is inferred without a
  human reading the source.

## 13. KINSHIP-HTR-FAMILY-01 — retrieval regression, settlement verification, household pilot (2026-11-22)

### 13.1 Phase A — known-positive Демидов retrieval audit (258-p014)

Human transcript (case file, scan 258-p014; NOT altered to match OCR):
«Той же вотчины деревни Волченокъ крестьянин Никита Демидов, жена его
Устинья Мартынова.»  Raw OCR at the exact location (lines 46643-46646,
conf .91-.99): «Тогми вотчены деревни Волченокъ крестьян- / Никата
умитревъ. / 113 жена его устиньй Мартинова- / рат его спиридон».

Miss-stage verdict: **recognition** destroyed the surname («Демидовъ» ->
«умитревъ», ≥5 edits; visual re-read at 4x/5x favors Дмитревъ-type
letterforms — an open discrepancy with the transcript, recorded, not
resolved). Segmentation and indexing are fine. Candidate generation can
NEVER reach the head line for Демидов variants: «никата умитрев» shares
ZERO trigrams with all 218 generated corruptions (д->u yields «уемидов»,
no gram overlap) — proven, not assumed. The fixable retrieval defects
were in ranking: (a) 2-4 char fragments («нов», «деми») scored
partial_ratio 100 and outranked genuine matches; (b) the profile run
pre-trimmed the pool corpus-wide (limit=5000, layout-inflated total)
before section-aware ranking.

Fixes: short-fragment damping in `Searcher.search_lines` (partial_ratio
scaled by len(text)/len(variant)); `limit=None` keeps full pools;
Демидов profile gained `household_anchors_documented`
(никита/устинья/мартынова from the transcript) and `name_search.py`
gained `anchor_leads()` (households with ≥2 distinct co-name matches in a
±6-line window, exported as `household_anchor_leads` with explicit
provenance — NOT surname matches).

Corrected retrieval: Мартынова -> known wife line rank 1/2731 (was 12);
Волченок -> header rank 3/110; Устинья -> rank 30/1308; Никита -> rank
179/1274 (behind genuine exact hits only); Демидов profile: strong=0
(correct — no OCR line carries a Демидов-like form), anchor lead #1 of
42 = the known household with all three co-anchors matched.
Regression test: `tests/test_known_positive_258p014.py` — 3/3 PASS.
Evidence: `work/evidence/phaseA-258-p014-demidov.json` + crops.

### 13.2 Phase B — settlement sections visually verified

All 9 candidate headings re-read from original scans (AI-assisted vision,
disclosed, NOT paleographic certification; «Волченойъ»/«валченогъ»/
«волчьникъ» NOT accepted as spelling variants). Full evidence with
coordinates and crops: `work/discovery/volchenok-sections-verified.json`.

| Section | Verified span | Classification |
|---|---|---|
| 258-A | 258-p014 l46 -> p020 LEFT end | confirmed |
| 258-C | 258-p068 right -> p073 closing formula | confirmed |
| 253-A | p080 l37 -> p083 LEFT end (next year's ведомость at p083 right y≈262) | probable (heading word unresolved; content corroborates) |
| 255-A | p026 l75 -> p030 right y≈1488 (owner+village heading «…Головкиной деревни [unread]») | probable |
| 255-B | p042 l69 -> p046 right y≈1456 («деревни Васильев-») | probable |
| 255-C | p077 l47 -> p082 end of case | probable |
| 256-A | p016 l69 -> p019 LEFT end (new-year header p019 right y≈386) | probable |
| 256-B | p053 l56 -> p058 LEFT end (new-year header p058 right y≈370) | probable |
| 258-B | p032 l64 -> p043 right y≈1004 («деревни Тимофеева», visually read) | probable |

Detector range corrections: 253-A end 97 -> 83-left; 255-A 36 ->
30-right@1488; 255-B 54 -> 46-right@1456; 256-A/B and 258-A ends are
LEFT-page ends (year boundaries mid-scan). Every intervening page was
checked for intra-range headings; the over-extensions contained other
villages' lists.

### 13.3 Phase C — household reconstruction pilot (section 258-A)

8 households extracted from 258-p014 l46-67 and 258-p015, each with
archive references (ЦГА Москвы ф.597 оп.1 д.258, scan/page/line/bbox),
head/spouse/children/siblings, ages, exact OCR forms, per-field
uncertainty flags, and dual provenance (OCR suggestion vs AI-assisted
visual draft vs the one human transcript fragment).
Dataset: `work/households/volchenok-258-households.json`.
Human-review interface (opened in the default browser):
`work/households/households-review.html` — crops, per-person
«человеческая выверка» fields, verified checkbox, localStorage autosave,
JSON export. Builder: `build_households_review.py`. Nothing is treated
as verified until a human completes it; hereditary surnames are NOT
inferred from -ов/-ев forms.
Key open uncertainties: H01 head's second name (Демидов vs Дмитрев-form);
Устинья's age (57 vs 37); Спиридон's age (48 vs 37); H02/H03 head-line
ambiguity (possible blla segmentation loss); H04 head missing.

### 13.4 Phase D — cross-year linkage candidates (ALL PROVISIONAL)

Full evidence: `work/discovery/cross-year-links.json`. Highlights:
- STRONG: 258-A H01 == 258-B first household (same corruption signature
  «умитревъ», wife Устинья 57 -> 58, consecutive yearly lists).
- MODERATE-STRONG: == 256-B first household (Никита Дмитревъ + Устинья
  [Мартынова-form] + брат Спиридон; Никита 56->59 plausible but the
  3/5 digit is open); Спиридон as head of 258-C household №469 with the
  same village sequence repeating (Монъ Андреевъ + Фекла Климова cluster).
- MODERATE with open contradiction: 255-C first household (жена Устинья
  Мартинова + брат Спиридон; Спиридон's age chain 41 -> 45/46 -> 48 is
  consistent, Устинья's 32/52 digit is open); 258-C «вдова Устинья
  Дмитрева» as the widowed wife of H01's head (widow formula fits, age
  51 vs expected 60+ unresolved).
- WEAK/OPEN: 253-p080 «Никита дми тревъ» (age 26 CONTRADICTS identity
  with the 59-year-old of 258-A — likely a different namesake or a
  misassigned age); 255-A first household (wife's name unresolved).
No identities were merged; every link awaits the human review queue
(13.4 file, section "human_review_queue").

## 14. KINSHIP-SEARCH-02 — Ivan/Pyotr contextual family search (2026-11-22)

Targeted search over the whole 327-page index (no OCR rerun, no new models,
no household-reconstruction scope): all occurrences of the given names
ИВАН and ПЁТР across cases 253/255/256/258, given-name forms strictly
distinguished from patronymic forms («Иван Петровъ» ≠ «Пётр Ивановъ»),
±8-line column-aware context, household boundaries from numbering and
relationship markers, P1–P5 classification and ranking.

Pipeline: `kinship_search02.py` (reuses the Searcher FTS5+trigram recall
layer, `profiles.corrupt`, `normalize`, RapidFuzz, line bboxes; a
full-corpus token pass cross-checks the recall pool for complete counts) →
`work/discovery/kinship-search02/kinship-search02.json`;
`build_search02_review.py` → source crops (30 groups, 22 MB) +
`kinship-search02-review.html` (opened in the default browser; relay
browser automation remains impossible under Rosetta, REPORT §2 — verified
programmatically: 30 cards, 60 crop refs all on disk, inline JS
node-parses, all group ids cross-checked against the JSON).

### 14.1 Occurrence totals (UNVERIFIED OCR, line-level)

| measure | Иван | Пётр |
|---|---|---|
| given-name lines (exact incl. glued OCR) | 656 | 296 |
| + strong fuzzy forms (similarity ≥80) | 273 | 126 |
| + weak fuzzy candidates (74–79, mostly noise) | 515 | 85 |
| patronymic-only lines (Ивановъ/Петровъ без имени) | 759 | 233 |
| all patronymic-form occurrences | 814 | 238 |

Person-level estimates (heuristic segmentation): ~514 exact + 482 fuzzy
Иван, ~188 exact + 113 fuzzy Пётр.

### 14.2 P1–P5 candidate groups (587 distinct household groups, 76 in
verified Волченокъ sections)

P1 = 1, P2 = 2, P3 = 6, P4 = 9, P5 = 569.  Documented father–son forms are
rare; P5 (Иван without Пётр) is the large pool, ranked confirmed-Волченокъ
first.  Top of the ranking:

1. **P1 258-p029R** — widower «Иванъ Ев(стигне)евъ» with child «Петръ»
   (~1831); AI-assisted visual re-read of the crop confirms both rows.
2. **P3 pair 258-p011R / 258-p065R** (~1831 / ~1834, cross-year recurrence
   flagged) — widower «Иванъ Ев(е/а)гнеевъ», «жена его Васса», children
   Праскева/**Петръ**/Ольга/Тимофей, plus «Ивановъ братъ Иванъ вдовъ»:
   two Иван-persons, so Пётр's father is NOT auto-attributed (visual
   re-read confirms the two-Ivan structure on p011).
3. **P2 253-p058L** «Петръ Ивановъ, холостъ» — the only clean Пётр
   Ивановъ form in the corpus.
4. **P4 258-p061R** — «Нагаръ Петровъ» (implied father Пётр) with sons
   «Иванъ» and «Петръ»: three generations of the name pair in one
   household.
5. **P5 + recurrence 258-p016L / 258-p070R (confirmed Волченокъ, 258-A
   ~1831 & 258-C ~1833-34) vs 253-p081R / 253-p067R (probable 253-A,
   1813-19)** — widower «Иванъ Прок(опи)евъ» with son «Иванъ»; a son Пётр
   born after any list-year is chronologically possible; linking years is
   a manual job, no identity asserted.

### 14.3 Method notes and blind spots

- Widow/widower lines break households; garbled head lines, y-gaps,
  implausible ages and small chislos carry explicit uncertainty flags;
  cross-page continuations merge with a flag; nothing is silently
  corrected.
- Weak fuzzy (74–79) never drives P1–P4 claims; strong fuzzy (≥80) may,
  always flagged.  «сина» was removed from the relation vocabulary
  («вина» false positives); fused «дети ихъ» garbles are matched with a
  stripped retry.
- Years are nearest OCR year-token attributions (UNVERIFIED; out-of-range
  tokens such as «1836» in case 258 or «1818» in case 255 are displayed
  with an explicit warning); pages without tokens fall back to the case
  archive range.
- Recall blind spot (unchanged from §13.1): names corrupted ≥2 edits with
  no surviving trigram are unreachable (Демидов→«умитревъ» class); the
  FTS/trigram pool cross-check (8510 lines) found 6210 pool lines without
  a token-level match — substring/glue noise, not recall losses.
- Landowner surnames (Головкина, Шуваловы, Трубниковы) appear only as
  owner context and never as kinship evidence; no identities were merged
  and nothing was connected to the established genealogy.
- Regression tests: `tests/test_kinship_search02.py` (classifier edge
  rules, ALL PASS) + `tests/test_known_positive_258p014.py` still 3/3.

| Artifact | Path |
|---|---|
| Search pipeline | `kinship_search02.py` |
| Full export (totals, 587 groups, top-30, conclusion) | `work/discovery/kinship-search02/kinship-search02.json` |
| Review page + crops | `work/discovery/kinship-search02/kinship-search02-review.html`, `crops/` |
| Crop/HTML builder | `build_search02_review.py` |
| Classifier regression tests | `tests/test_kinship_search02.py` |
