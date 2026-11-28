"""Synthetic live monitor regression tests (no models, no archive)."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("live_monitor", ROOT / "htr-live-monitor.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

records = [{"page_id": p, "source_id": "torrents", "confirmed_at": "yes",
            "decode_ok": True} for p in ("a", "b", "c", "d", "e", "f")]
records[-1]["parked"] = True
stable = {r["page_id"]: r for r in records}
rows = {"a": ("ok", 4), "b": ("duplicate", 0), "e": ("failed", 0)}
meta = {"a": {"status": "ok"}, "c": {"status": "ok"}}
sources, total = m.classify(records, stable, rows, meta, 3)
assert total["indexed"] == 1 and total["duplicate"] == 1
assert total["ocr_ready"] == 1 and total["pending"] == 1
assert total["retry"] == 1 and total["parked"] == 1
assert total["stable"] == total["discovered"] == 6
# A live page moves out of the pending/retry bucket, not into a second count.
assert m.pipeline_counts(total, "d", stable, rows, meta, 3) == (0, 1, 1)
assert m.pipeline_counts(total, "e", stable, rows, meta, 3) == (1, 0, 1)
assert m.pipeline_counts(total, "c", stable, rows, meta, 3) == (1, 1, 0)
assert m.pipeline_counts(total, "f", stable, rows, meta, 3) == (1, 1, 0)
assert m.pipeline_counts(total, None, stable, rows, meta, 3) == (1, 1, 0)
assert m.fit("12345", 4) == "123…"

assert sum(total[k] for k in ("indexed", "duplicate", "ocr_ready", "pending", "retry", "parked")) == 6

events = [
    {"event": "round_finished", "round_no": 1, "ts": "a", "finalize": {"indexed": 20}},
    {"event": "round_timing", "round_no": 1, "ts": "b", "loop_s": 300},
    {"event": "daemon_started"},
    {"event": "round_timing", "round_no": 1, "loop_s": 1},
    {"event": "round_finished", "round_no": 1, "finalize": {"indexed": 10}},
    {"event": "round_timing", "round_no": 1, "loop_s": 300},
]
assert m.rates(events) == (180.0, 180.0)
assert m.rates([]) == (None, None)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    d = root / "c"
    d.mkdir()
    p = d / "meta.json"
    cache = m.Checkpoints(root)
    (d / "meta.json.tmp").write_text("{}")
    assert cache.scan() == {}
    p.write_text(json.dumps({"page_id": "c", "status": "ok",
                             "completed_at": "2026-11-25T21:00:00+07:00",
                             "line_count": 12, "extra_line_count": 2,
                             "elapsed_s": 15.1}))
    result = cache.scan()
    assert result["c"]["status"] == "ok"
    op = m.operations(result, rows, stable, 10)[0]
    assert op[3] == "WAIT DB" and op[5] == 14
    assert m.operations(result, {"c": ("ok", 14)}, stable, 10)[0][3] == "INDEXED"
    assert m.operations(result, {"c": ("duplicate", 0)}, stable, 10)[0][3] == "ALIAS"
    p.unlink()
    assert cache.scan() == {}
# Same color semantics as GAVO; plain output stays pipe-friendly.
assert m.state_color("INDEXED") == m.GREEN
assert m.state_color("WAIT DB") == m.CYAN
assert m.state_color("FAILED") == m.RED
assert m.elapsed_color(2, 20) == m.GREEN
assert m.elapsed_color(6, 20) == m.YELLOW
assert m.elapsed_color(12, 20) == m.ORANGE
assert m.elapsed_color(18, 20) == m.RED
m.COLOR = False
assert m.bar(1, 2, 4) == "██░░  50.0%"
m.COLOR = True
assert m.paint("OK", m.GREEN) == m.GREEN + "OK" + m.RESET
assert m.GREEN + "██" + m.RESET in m.bar(1, 2, 4)
print("test_live_monitor: ALL PASS")
