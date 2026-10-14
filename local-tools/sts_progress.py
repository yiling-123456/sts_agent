"""Summarise the newest AgenticSTS run log (logs/run_*.jsonl).

Reports decision count, floor/act progress, action mix, LLM call latency and
token totals, retries and errors. Read-only; safe to run while the agent plays.

Usage:  .venv\\Scripts\\python.exe ..\\..\\sts_progress.py [run_log.jsonl]
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)))


def newest_log() -> str:
    logs = glob.glob(os.path.join(REPO, "logs", "run_*.jsonl"))
    if not logs:
        raise SystemExit("no run logs found")
    return max(logs, key=os.path.getmtime)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else newest_log()
    perms = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                perms.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    events = Counter(r.get("event") for r in perms)
    decisions = [r for r in perms if r.get("event") == "decision"]
    stages = [r for r in perms if r.get("event") == "perf"]
    results = [r for r in perms if r.get("event") == "action_result"]

    def ts(value: object) -> str:
        try:
            import datetime

            return datetime.datetime.fromtimestamp(float(value)).strftime("%H:%M:%S")
        except (TypeError, ValueError, OSError):
            return str(value)

    def action_name(row: dict) -> str:
        action = row.get("action")
        if isinstance(action, dict):
            return str(action.get("action") or "?")
        return str(action)

    print(f"log      : {os.path.basename(path)}")
    print(f"records  : {len(perms)}  {dict(events)}")
    if decisions:
        print(
            f"decisions: {len(decisions)}  first={ts(decisions[0].get('ts'))}  "
            f"last={ts(decisions[-1].get('ts'))}"
        )
    floors = sorted({d.get("floor") for d in decisions if isinstance(d.get("floor"), int)})
    if floors:
        print(f"floors   : {floors[0]}..{floors[-1]} (max F{max(floors)})")
    print("actions  :", dict(Counter(action_name(d) for d in decisions).most_common()))
    print("states   :", dict(Counter(str(d.get("state_type")) for d in decisions).most_common()))

    # LLM latency from the engine's own perf stages.
    llm = [s for s in stages if "llm" in str(s.get("stage", "")).lower() or "v2" in str(s.get("stage", "")).lower()]
    if llm:
        durs = sorted(s["duration_ms"] for s in llm if isinstance(s.get("duration_ms"), (int, float)))
        if durs:
            mid = durs[len(durs) // 2]
            print(f"llm calls: n={len(durs)} median={mid:.0f}ms max={max(durs):.0f}ms total={sum(durs)/1000:.0f}s")
    print("stages   :", dict(Counter(s.get("stage") for s in stages).most_common(12)))

    bad = [r for r in results if str(r.get("status")).lower() not in {"ok", "success", "applied", "true"}]
    if bad:
        print(f"non-ok action results: {len(bad)}")
        for r in bad[-5:]:
            print("   ", r.get("step"), r.get("action"), r.get("status"), str(r.get("mcp_message"))[:120])


if __name__ == "__main__":
    main()
