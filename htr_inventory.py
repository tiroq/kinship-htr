"""Source discovery and inventory for the incremental HTR indexer.

Walks the configured source roots, identifies archival originals (never
thumbnails, crops, AppleDouble `._*` files, `.part` downloads or temporary
files), and maintains a persistent observation state:

  * an image becomes STABLE when its size+mtime are unchanged across two
    consecutive polls (default 60 s apart) — or immediately when a trusted
    completion manifest (GAVO metadata.json + manifest.csv) already records
    it as jpeg-verified;
  * at confirmation the image is decoded once (PIL verify) and its SHA-256
    recorded (taken from a trusted manifest when sizes match, else computed);
  * discovery facts persist in runtime state.json (atomic writes), so a
    restarted worker never reprocesses and never re-hashes unchanged files.

GAVO Watchdog state files, dl-log.jsonl, inventories and manifests are read
only — never written, never locked.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
from pathlib import Path

from htr_config import ensure_runtime_dirs, load as load_config

STATE_VERSION = 1
APPLEDOUBLE = re.compile(r"^\._")
# Ф.И18.Оп.1.Д.0131.00001.jpg / Ф.И18.Оп.1.Д.0131.00001.jpeg
TORRENT_NAME = re.compile(
    r"Ф\.(?P<fond>[^.]+)\.Оп\.(?P<opis>\d+)\.Д\.(?P<delo>\d+)\.(?P<seq>\d+)",
    re.IGNORECASE)
# И331Оп3Д605_0000.jpg  (Cyrillic И/О/Д)
GAVO_NAME = re.compile(r"^И(?P<fond>\d+)Оп(?P<opis>\d+)Д(?P<delo>\d+)_(?P<seq>\d+)")
GAVO_DIR = re.compile(r"^I-(?P<fond>\d+)-op(?P<opis>\d+)-d(?P<delo>\d+)$")


# ---------------------------------------------------------------- state io

def load_state(cfg: dict) -> dict:
    p = Path(cfg["state_path"])
    if p.exists():
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            if st.get("version") == STATE_VERSION:
                return st
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": STATE_VERSION, "files": {}, "alloc": {}}


def save_state(cfg: dict, state: dict) -> None:
    p = Path(cfg["state_path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- discovery

def _iter_source_files(source: dict, cfg: dict):
    root = Path(source["root"])
    if not root.exists():
        return
    exclude = set(cfg.get("exclude_dir_names", []))
    exts = {e.lower() for e in cfg.get("image_extensions",
                                       [".jpg", ".jpeg", ".png"])}
    seen: set[Path] = set()
    for pattern in source.get("include", ["**/*.jpg", "**/*.jpeg"]):
        for p in sorted(root.glob(pattern)):
            if not p.is_file() or p in seen:
                continue
            if p.suffix.lower() not in exts:
                continue
            if APPLEDOUBLE.match(p.name) or p.name == ".DS_Store":
                continue
            if p.name.endswith(".part"):
                continue
            rel_parts = p.relative_to(root).parts
            if any(part in exclude for part in rel_parts[:-1]):
                continue
            if rel_parts[-1] in exclude:
                continue
            seen.add(p)
            yield p


def _manifest_gavo(case_dir: Path) -> tuple[dict, dict]:
    """-> (rows_by_file, case_metadata) from a GAVO case dir; empty if absent."""
    rows: dict[str, dict] = {}
    meta: dict = {}
    m = case_dir / "manifest.csv"
    if m.exists():
        try:
            with m.open(newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    rows[r["file"]] = r
        except (OSError, csv.Error):
            pass
    mj = case_dir / "metadata.json"
    if mj.exists():
        try:
            meta = json.loads(mj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return rows, meta


def _gavo_case_complete(meta: dict) -> bool:
    try:
        return (meta.get("expected") is not None
                and meta.get("downloaded") == meta.get("expected")
                and (meta.get("jpegVerified") or 0) >= meta["downloaded"]
                and not meta.get("bad")
                and not meta.get("missingIndices"))
    except (TypeError, KeyError):
        return False


def _legacy_manifest(cfg: dict) -> dict:
    """path -> (page_id, sha256, width, height) from work/index_manifest.json."""
    out = {}
    p = Path(cfg["legacy_manifest"])
    if p.exists():
        try:
            for pid, (path, sha, w, h) in json.loads(
                    p.read_text(encoding="utf-8")).items():
                out[str(path)] = (pid, sha, w, h)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decode_check(path: Path) -> tuple[bool, int | None, int | None]:
    """Structural decode + dimensions.  Returns (ok, width, height)."""
    try:
        from PIL import Image
    except ImportError:
        return True, None, None      # no PIL: manifest/other checks still apply
    try:
        with Image.open(path) as img:
            w, h = img.size
            img.verify()
        return True, w, h
    except Exception:
        return False, None, None


def _identify(source: dict, path: Path, cfg: dict,
              legacy: dict) -> dict:
    """Derive stable archival identity for one image."""
    root = Path(source["root"])
    rel = str(path.relative_to(root))
    coll = source.get("collection", source["id"])
    ident: dict = {
        "source_id": source["id"], "collection": coll,
        "archive": source.get("archive"), "root": str(root),
        "logical_path": f"{source['id']}/{rel}",
        "original_filename": path.name,
        "fond": None, "opis": None, "delo": None, "scan_number": None,
        "page_id": None, "case_short": None, "case_ref": None,
    }

    # trusted legacy manifest (already-indexed CGAMOS pages)
    leg = legacy.get(str(path))
    if leg:
        pid, sha, w, h = leg
        ident.update(page_id=pid, sha256=sha, width=w, height=h,
                     case_short=pid.split("-")[0],
                     case_ref=f"cgamos:{pid.split('-')[0]}",
                     fond="597", opis="1", delo=pid.split("-")[0],
                     scan_number=int(pid.split("p")[-1]),
                     page_number=int(pid.split("p")[-1]),
                     manifest_trusted=True)
        return ident

    # GAVO: case dir + filename carry fond/opis/delo/seq
    parts = path.parts
    gavo_case = None
    for i, part in enumerate(parts):
        dm = GAVO_DIR.match(part)
        if dm and i + 1 < len(parts) and parts[i + 1] == "originals":
            gavo_case = dm
            fm = GAVO_NAME.match(path.name)
            seq = int(fm.group("seq")) if fm else None
            ident.update(
                fond=f"И{dm.group('fond')}", opis=dm.group("opis"),
                delo=dm.group("delo"), scan_number=seq,
                case_short=f"g{dm.group('delo')}",
                case_ref=(f"gavo-I{dm.group('fond')}-{dm.group('opis')}-"
                          f"{dm.group('delo')}"))
            if seq is not None:
                ident["page_id"] = f"{ident['case_short']}-p{seq:03d}"
                ident["page_number"] = seq
            break

    if ident["page_id"] is None:
        # torrent payload: Ф.И18.Оп.1.Д.0131.00001.jpg
        tm = TORRENT_NAME.match(path.name)
        if tm:
            seq = int(tm.group("seq"))
            fond_digits = re.sub(r"\D", "", tm.group("fond"))
            case_short = (f"i{fond_digits}-{tm.group('opis')}-"
                          f"{int(tm.group('delo'))}")
            ident.update(
                fond=tm.group("fond"), opis=tm.group("opis"),
                delo=str(int(tm.group("delo"))), scan_number=seq,
                case_short=case_short,
                case_ref=f"torrent-{case_short}",
                page_id=f"{case_short}-p{seq:05d}",
                page_number=seq)
        elif gavo_case is not None:
            # GAVO dir, non-standard filename: keep the case, number by stem
            import zlib
            digits = re.search(r"(\d+)$", path.stem)
            seq = (int(digits.group(1)) if digits
                   else zlib.crc32(path.name.encode("utf-8")) % 10**6)
            ident["scan_number"] = seq
            ident["page_id"] = f"{ident['case_short']}-x{seq:06d}"
        else:
            # any other archive file: derive from directory + filename stem
            case_slug = re.sub(r"[^0-9A-Za-z]+", "-",
                               rel.rsplit("/", 1)[0] if "/" in rel
                               else "root").strip("-").lower()
            stem = re.sub(r"[^0-9A-Za-z]+", "-", path.stem).strip("-").lower()
            case_short = f"{source['id']}-{case_slug or 'root'}"[:40]
            ident.update(case_short=case_short,
                         case_ref=f"{source['id']}:{case_slug or 'root'}",
                         page_id=f"{case_short}-{stem}")
    return ident


# ---------------------------------------------------------------- polling

def poll(cfg: dict | None = None, *, decode: bool = True) -> dict:
    """One inventory pass.  Returns a summary dict; state persisted atomically."""
    cfg = cfg or load_config()
    ensure_runtime_dirs(cfg)
    state = load_state(cfg)
    legacy = _legacy_manifest(cfg)
    files = state["files"]
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    summary = {"sources": [], "discovered": 0, "new": 0,
               "stable": 0, "unstable": 0, "manifest_trusted": 0,
               "decode_failed": 0, "errors": []}

    for source in cfg["sources"]:
        src_stat = {"id": source["id"], "found": 0, "new": 0, "stable": 0}
        if not Path(source["root"]).exists():
            summary["errors"].append(f"source root missing: {source['root']}")
            summary["sources"].append(src_stat)
            continue
        gavo_meta_cache: dict[str, tuple[dict, dict]] = {}
        for path in _iter_source_files(source, cfg):
            key = str(path)
            src_stat["found"] += 1
            summary["discovered"] += 1
            try:
                st = path.stat()
            except OSError:
                continue
            rec = files.get(key)
            created = False
            if rec is None:
                ident = _identify(source, path, cfg, legacy)
                if ident.get("page_number") is None:
                    cs = ident.get("case_short") or "root"
                    n = int(state["alloc"].get(cs, 0)) + 1
                    state["alloc"][cs] = n
                    ident["page_number"] = n
                rec = files[key] = {
                    **ident, "size": st.st_size, "mtime": st.st_mtime,
                    "obs": 1, "confirmed_at": None, "decode_ok": None,
                    "sha256": ident.get("sha256"), "width": ident.get("width"),
                    "height": ident.get("height"),
                    "discovered_at": now, "source_id": source["id"],
                }
                created = True
                summary["new"] += 1
                src_stat["new"] += 1
            changed = (rec["size"] != st.st_size
                       or abs(rec["mtime"] - st.st_mtime) > 1e-6)
            if changed:
                rec.update(size=st.st_size, mtime=st.st_mtime, obs=1,
                           confirmed_at=None, decode_ok=None)
            elif not created and rec.get("confirmed_at") is None:
                rec["obs"] = rec.get("obs", 1) + 1

            if rec.get("confirmed_at"):
                src_stat["stable"] += 1
                summary["stable"] += 1
                continue

            # trusted completion manifest (GAVO case fully downloaded+verified)
            trusted = False
            if source.get("manifest") == "gavo":
                case_dir = path.parent.parent
                ck = str(case_dir)
                if ck not in gavo_meta_cache:
                    gavo_meta_cache[ck] = _manifest_gavo(case_dir)
                rows, meta = gavo_meta_cache[ck]
                row = rows.get(path.name)
                if row and _gavo_case_complete(meta):
                    trusted = True
                    if rec.get("sha256") is None:
                        rec["sha256"] = row.get("sha256")
                        rec["width"] = int(row["width"]) if row.get("width") else None
                        rec["height"] = int(row["height"]) if row.get("height") else None
                        summary["manifest_trusted"] += 1
            if source.get("manifest") == "filename" and rec.get("sha256") is None:
                pass  # computed below at confirmation

            need_confirm = trusted or rec.get("obs", 0) >= 2
            if not need_confirm:
                summary["unstable"] += 1
                continue

            ok, w, h = (True, None, None)
            if decode:
                ok, w, h = _decode_check(path)
            if not ok:
                rec["decode_ok"] = False
                summary["decode_failed"] += 1
                # drop from queue: a corrupt/unstable file is re-checked
                # when it changes (size/mtime reset the observation)
                continue
            rec["decode_ok"] = True
            if rec.get("width") is None and w:
                rec["width"], rec["height"] = w, h
            if rec.get("sha256") is None:
                rec["sha256"] = _sha256_file(path)
            rec["confirmed_at"] = now
            summary["stable"] += 1
            src_stat["stable"] += 1

        summary["sources"].append(src_stat)

    save_state(cfg, state)
    return summary


def stable_files(cfg: dict) -> list[dict]:
    """Confirmed images, in stable page-id order, with their state records."""
    state = load_state(cfg)
    out = [dict(rec, path=key)
           for key, rec in state["files"].items()
           if rec.get("confirmed_at") and rec.get("decode_ok")
           and rec.get("page_id")]
    out.sort(key=lambda r: (r.get("source_id") or "", r["page_id"]))
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-decode", action="store_true",
                    help="skip PIL decode checks (dry inventory)")
    ns = ap.parse_args()
    cfg = load_config(ns.config)
    summary = poll(cfg, decode=not ns.no_decode)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
