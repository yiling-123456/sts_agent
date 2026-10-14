"""Probe which models the relay actually serves, and what they do with effort.

Sends one small prompt per model (optionally with reasoning_effort) and reports
status, latency, reasoning tokens and a snippet of the answer. Useful before
pointing a 50-minute agent run at a model name.

Usage:
    python model_probe.py gpt-5.6-sol gpt-6-sol gpt-6.1-sol
    python model_probe.py --effort medium gpt-5.6-sol
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8129/v1"
DEFAULT_MODELS = ["gpt-5.6-sol"]
PROMPT = (
    "Slay the Spire 2 A0 Ironclad, floor 6 elite. HP 42/80, 3 energy. "
    "Hand: Strike(6), Defend(5), Bash(8+2 Vulnerable, 2E), Pommel Strike(9+draw). "
    "Enemy Gremlin Nob, 48 HP, gains Enrage when you play a Skill. "
    "Answer with one action and one short reason."
)


def gpt_api_key() -> str:
    """Key from the environment, else the project's .env — never hard-coded here."""
    key = os.environ.get("STS2_GPT_API_KEY", "").strip()
    if key:
        return key
    here = Path(__file__).resolve()
    for base in (here.parent, *here.parents):
        for rel in (
            Path("AgenticSTS") / "AgenticSTS" / ".env",
            Path("AgenticSTS") / ".env",
            Path(".env"),
        ):
            candidate = base / rel
            if candidate.is_file():
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("STS2_GPT_API_KEY="):
                        return line.split("=", 1)[1].strip()
    raise SystemExit(
        "STS2_GPT_API_KEY not found: export it, or add it to AgenticSTS/AgenticSTS/.env"
    )


def probe(model: str, effort: str | None) -> None:
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 2048,
    }
    if effort:
        payload["temperature"] = 0
        payload["reasoning_effort"] = effort
    started = time.time()
    try:
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            resp = client.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {gpt_api_key()}"},
                json=payload,
            )
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        print(f"{model:16s} effort={str(effort):7s} -> transport error {type(exc).__name__}: {exc}")
        return
    elapsed = time.time() - started
    if resp.status_code != 200:
        print(f"{model:16s} effort={str(effort):7s} -> HTTP {resp.status_code}: {resp.text[:160]}")
        return
    body = resp.json()
    usage = body.get("usage", {}) or {}
    details = usage.get("completion_tokens_details", {}) or {}
    served = body.get("model", "?")
    content = (body["choices"][0]["message"].get("content") or "").strip().replace("\n", " ")
    print(
        f"{model:16s} effort={str(effort):7s} -> HTTP 200 served={served} "
        f"reasoning={details.get('reasoning_tokens', 'n/a')} "
        f"completion={usage.get('completion_tokens', 'n/a')} latency={elapsed:5.1f}s | {content[:80]}"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    effort: str | None = "medium"
    if "--effort" in args:
        i = args.index("--effort")
        effort = args[i + 1]
        args = args[:i] + args[i + 2 :]
    models = args or DEFAULT_MODELS
    for name in models:
        probe(name, effort)
