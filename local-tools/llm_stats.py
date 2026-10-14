"""Aggregate per-run LLM stats from AgenticSTS JSONL logs.

Usage: .venv\\Scripts\\python.exe llm_stats.py <run_log.jsonl> [...]
"""

from __future__ import annotations

import json
import sys


def summarise(path: str) -> None:
    calls = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if "llm_call" not in line and "llm_request_end" not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") in {"llm_call", "llm_request_end"}:
                calls.append(rec)

    print(f"{path}: events={len(calls)}")
    if not calls:
        return
    print("  sample keys:", sorted(calls[-1]))
    print("  sample:", json.dumps(calls[-1], ensure_ascii=False)[:400])

    def total(*names: str) -> int:
        out = 0
        for rec in calls:
            for name in names:
                value = rec.get(name)
                if isinstance(value, (int, float)):
                    out += value
                    break
        return int(out)

    tokens_in = total("input_tokens", "prompt_tokens")
    tokens_out = total("output_tokens", "completion_tokens")
    cache = total("cache_read_input_tokens", "cache_read_tokens")
    durs = sorted(
        rec.get("latency_ms", rec.get("duration_ms", 0)) or 0
        for rec in calls
        if isinstance(rec.get("latency_ms", rec.get("duration_ms")), (int, float))
    )
    print(f"  tokens: in={tokens_in} out={tokens_out} cache_read={cache}")
    if durs:
        print(f"  calls={len(durs)} median={durs[len(durs) // 2]:.0f}ms max={max(durs):.0f}ms")


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        summarise(arg)
