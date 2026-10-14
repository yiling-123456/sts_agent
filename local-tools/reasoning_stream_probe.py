"""Check whether the relay streams visible reasoning text per model.

Streams a tool-bearing request (the shape the agent uses for every real
decision) and counts ``delta.reasoning_content`` frames, so we can tell
"the model reasons but the relay hides it" from "our capture path is off".

Usage: python reasoning_stream_probe.py gpt-6.1-sol gpt-5.5
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8129/v1"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "play_card",
            "description": "Play a card from hand by index.",
            "parameters": {
                "type": "object",
                "properties": {"card_index": {"type": "integer"}},
                "required": ["card_index"],
            },
        },
    }
]
PROMPT = (
    "Slay the Spire 2 A0 Ironclad floor 6 elite. HP 42/80, 3 energy. "
    "Hand: Strike(6), Defend(5), Bash(8+2 Vulnerable, 2E), Pommel Strike(9+draw). "
    "Enemy Gremlin Nob has 48 HP and gains Enrage on every Skill you play. "
    "Pick one action with the play_card tool."
)


def gpt_api_key() -> str:
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
    raise SystemExit("STS2_GPT_API_KEY not found")


def probe(model: str) -> None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 2048,
        "temperature": 0,
        "reasoning_effort": "medium",
        "tools": TOOLS,
        "tool_choice": "required",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    reasoning_frames = 0
    reasoning_chars = 0
    content_chars = 0
    usage: dict = {}
    started = time.time()
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        with client.stream(
            "POST",
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {gpt_api_key()}"},
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                print(f"{model:16s} -> HTTP {resp.status_code}: {resp.read().decode('utf-8', 'replace')[:200]}")
                return
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content")
                    if rc:
                        reasoning_frames += 1
                        reasoning_chars += len(rc)
                    if delta.get("content"):
                        content_chars += len(delta["content"])
    details = (usage or {}).get("completion_tokens_details", {}) or {}
    print(
        f"{model:16s} -> reasoning_content frames={reasoning_frames} chars={reasoning_chars} "
        f"visible_content_chars={content_chars} usage_reasoning_tokens={details.get('reasoning_tokens', 'n/a')} "
        f"latency={time.time() - started:.1f}s"
    )


if __name__ == "__main__":
    for name in sys.argv[1:] or ["gpt-6.1-sol", "gpt-5.5"]:
        probe(name)
