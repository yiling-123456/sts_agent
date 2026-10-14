"""Compare relay reasoning depth for gpt-5.5 across reasoning_effort levels.

Sends the same combat prompt three times through the local relay proxy with
reasoning_effort = low / medium / high (the values AgenticSTS' fast / strategic /
analysis tiers use) and reports reasoning tokens + latency for each.

The key is read from the environment / project `.env` (`STS2_GPT_API_KEY`).

Usage: AgenticSTS\\AgenticSTS\\.venv\\Scripts\\python.exe effort_probe.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8129/v1"
MODEL = "gpt-5.5"

PROMPT = (
    "Slay the Spire 2, A0 Ironclad, floor 6 elite fight. HP 42/80, energy 3. "
    "Hand: Strike(6dmg), Defend(5blk), Bash(8dmg+2 Vulnerable, 2 energy), "
    "Pommel Strike(9dmg+draw 1). Enemy Gremlin Nob has 48 HP and gains Enrage "
    "whenever you play a Skill. Choose one action and give a one-sentence reason."
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


def probe(effort: str) -> None:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 4096,
        "temperature": 0,
        "reasoning_effort": effort,
    }
    started = time.time()
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        resp = client.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {gpt_api_key()}"},
            json=payload,
        )
    elapsed = time.time() - started
    if resp.status_code != 200:
        print(f"{effort:6s} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return
    body = resp.json()
    usage = body.get("usage", {})
    details = usage.get("completion_tokens_details", {}) or {}
    content = (body["choices"][0]["message"].get("content") or "").strip().replace("\n", " ")
    print(
        f"{effort:6s} -> reasoning_tokens={details.get('reasoning_tokens', 'n/a'):>5} "
        f"completion={usage.get('completion_tokens', 'n/a'):>5} "
        f"latency={elapsed:5.1f}s | {content[:90]}"
    )


if __name__ == "__main__":
    for level in ("low", "medium", "high"):
        probe(level)
