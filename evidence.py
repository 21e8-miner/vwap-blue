"""
Evidence per setup class, from the replays the public page links: what each kind of row has actually done.

A grade is a ranking, not a forecast; since v1.7.0 the page shows next to every row the replayed record
of its class (asset class x setup mode x side): trades, sessions, net R per trade after costs, and
"direction", the trade minus the same bracket the other way from the same bar (replay_sessions._mirror),
which is what the trigger's call added. The numbers come from the two replay files the honest banner
links (equities, crypto), so they are the banner's own trades cut by class.

The classes v1.7.0 capped to B (gap fades, alone or with a reclaim) are no longer in a grade-A replay, so
their record comes from the paired same-bars replay of the engine before the cap: the trades it took that
the current engine does not, labelled with the grade they had then. That file carries no mirror, so those
segments have no direction of their own; the cap's union-level direction is kept under "capped".

  python3 evidence.py                      # write research/evidence_v<engine>.json from the banner's files
  python3 evidence.py --embed index.html   # ...and paste it into the page as `const EVIDENCE = {...};`

tests/test_pages_demo.py recomputes the table from the linked files and checks the page carries it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import honest                    # noqa: E402
from engine import ENGINE_VERSION  # noqa: E402

SIDES = ("all", "long", "short")
PAIRED_VERSION = "1.6.0"                                     # the engine before the fade cap
PAIRED_KEY = "paired_v1_6_0_same_bars"                       # replay_sessions' block when run paired with it
AS_GRADED_NOW = "A"
AS_GRADED_CAPPED = f"A before v{ENGINE_VERSION}, B since"


def banner_files(html: str) -> Dict[str, str]:
    """{'equities': 'research/...json', 'crypto': 'research/...json'} from the honest banner's links."""
    banner = re.search(r'<div id="honest"[^>]*>(.*?)</div>', html, re.S).group(1)
    return {label: path for path, label in re.findall(r'blob/main/(research/[^"]+\.json)"[^>]*>([^<]+)</a>', banner)}


def paired_file(path: str, replay: Dict[str, Any]) -> Optional[str]:
    """The same-bars replay of the engine before the cap, when this replay was run paired with one."""
    if PAIRED_KEY not in replay.get("method", {}):
        return None
    cand = path.replace(f"_v{ENGINE_VERSION}.json", f"_v{PAIRED_VERSION}.json")
    return cand if cand != path and (ROOT / cand).exists() else None


def _classic(replay: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [t for t in replay["trades"] if t.get("model") == "classic"]


def _key(t: Dict[str, Any]):
    return (t["session"], t["ticker"], t["trigger_time"])


def _stat(trades: List[Dict[str, Any]], key) -> Optional[Dict[str, Any]]:
    vals = [key(t) for t in trades]
    if any(v is None for v in vals):
        return None
    s = honest.clustered(vals, [t["session"] for t in trades])
    return {"mean": s["mean"], "se": s["se"], "t": s["t"], "verdict": honest.verdict(s)}


def _segments(trades: List[Dict[str, Any]], asset: str, modes: List[str], as_graded: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for mode in modes:
        for side in SIDES:
            sel = [t for t in trades if (mode == "any" or t["setup_mode"] == mode) and (side == "all" or t["side"] == side)]
            if not sel:
                continue
            out[f"{asset}|{mode}|{side}"] = {
                "n": len(sel), "sessions": len({t["session"] for t in sel}), "as_graded": as_graded,
                "net": _stat(sel, lambda t: t["r_net"]),
                "mirror": _stat(sel, lambda t: t.get("mirror_r_net")),
                "direction": _stat(sel, lambda t: None if t.get("mirror_r_net") is None else t["r_net"] - t["mirror_r_net"]),
                "stopped": sum(1 for t in sel if t["exit_reason"] == "stop"),
                "target": sum(1 for t in sel if t["exit_reason"] == "target"),
            }
    return out


def segments(replay: Dict[str, Any], asset: str) -> Dict[str, Any]:
    """The replay's grade-A classic trades cut by setup mode (and 'any') and side."""
    trades = _classic(replay)
    return _segments(trades, asset, sorted({t["setup_mode"] for t in trades}) + ["any"], AS_GRADED_NOW)


def capped_segments(replay: Dict[str, Any], paired: Dict[str, Any], asset: str) -> Dict[str, Any]:
    """
    The paired (pre-cap) replay's classic trades the current replay does not take: the classes the cap
    moved to B, cut by their setup mode and side and labelled with the grade they had then. Their count
    must be the overlap the paired replay reported, so a stale pairing fails loudly.
    """
    kept = {_key(t) for t in _classic(replay)}
    dropped = [t for t in _classic(paired) if _key(t) not in kept]
    overlap = replay["method"][PAIRED_KEY].get("classic_overlap", {})
    if overlap and len(dropped) != overlap.get("dropped"):
        raise SystemExit(f"{asset}: {len(dropped)} trades dropped against the paired replay, it reported {overlap}")
    return _segments(dropped, asset, sorted({t["setup_mode"] for t in dropped}), AS_GRADED_CAPPED)


def build(files: Dict[str, str]) -> Dict[str, Any]:
    table: Dict[str, Any] = {"engine_version": ENGINE_VERSION, "sources": {}, "segments": {}, "capped": {}}
    for label, asset in (("equities", "equity"), ("crypto", "crypto")):
        path = files.get(label)
        if not path:
            continue
        replay = json.loads((ROOT / path).read_text())
        if replay.get("engine_version") != ENGINE_VERSION:
            raise SystemExit(f"{path} is engine {replay.get('engine_version')}, not {ENGINE_VERSION}")
        src = {"file": path, "window": replay["method"].get("window"),
               "sessions": replay["method"].get("sessions_replayed"), "grade": "decision-time ≥ A"}
        table["segments"].update(segments(replay, asset))
        prev = paired_file(path, replay)
        if prev:
            paired = json.loads((ROOT / prev).read_text())
            if paired.get("engine_version") != PAIRED_VERSION:
                raise SystemExit(f"{prev} is engine {paired.get('engine_version')}, not {PAIRED_VERSION}")
            src["paired"] = prev
            table["segments"].update(capped_segments(replay, paired, asset))
            cap = replay["method"][PAIRED_KEY].get("capped_to_B_same_bars")
            if cap:
                table["capped"][asset] = {"n": cap["n"], "what": cap.get("what"), "net": cap.get("net"),
                                          "direction": cap.get("direction_over_mirror"), "verdict": cap.get("verdict")}
        table["sources"][asset] = src
    return table


def embed(html_path: Path, table: Dict[str, Any]) -> None:
    s = html_path.read_text()
    blob = json.dumps(table, separators=(",", ":"), ensure_ascii=False)
    new, n = re.subn(r"const EVIDENCE = \{.*?\};\n", f"const EVIDENCE = {blob};\n", s, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"{html_path}: no `const EVIDENCE = {{...}};` line to replace")
    html_path.write_text(new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="index.html", help="the page whose banner names the replay files")
    ap.add_argument("--embed", nargs="*", help="pages to paste the table into (also mirrors to cf-pages/ if given there)")
    ap.add_argument("--out", help=f"JSON path (default research/evidence_v{ENGINE_VERSION}.json)")
    a = ap.parse_args()
    files = banner_files((ROOT / a.page).read_text())
    table = build(files)
    out = Path(a.out) if a.out else ROOT / "research" / f"evidence_v{ENGINE_VERSION}.json"
    out.write_text(json.dumps(table, indent=1, ensure_ascii=False))
    capped = sum(1 for s in table["segments"].values() if s["as_graded"] != AS_GRADED_NOW)
    print(f"wrote {out}: {len(table['segments'])} segments ({capped} from the paired pre-cap replays) from {list(files.values())}")
    for p in a.embed or []:
        embed(ROOT / p, table)
        print(f"embedded into {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
