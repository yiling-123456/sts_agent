"""Payload-realistic smoke test: reproduces the exact request shape
AgenticSTS' V2Backend sends for a GPT-5-family model through the relay
(streaming + tools + reasoning_effort + temperature=0 + max_tokens=4096).

The key is read from the environment / project `.env` (`STS2_GPT_API_KEY`).

Run:  AgenticSTS\\AgenticSTS\\.venv\\Scripts\\python.exe smoke_gpt_relay.py [base_url] [model]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8129/v1"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "gpt-5.5"


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


API_KEY = gpt_api_key()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "play_card",
            "description": "Play a card from hand by index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_index": {"type": "integer"},
                    "target_index": {"type": "integer"},
                },
                "required": ["card_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_turn",
            "description": "End the current turn.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

payload = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a Slay the Spire 2 combat agent. Answer with a tool call."},
        {"role": "user", "content": "Combat: 3 energy, hand = [Strike (6 dmg), Defend (5 block), Strike]. "
                                   "Enemy has 12 HP and attacks for 9. Pick one action."},
    ],
    "max_tokens": 4096,
    "temperature": 0,
    "reasoning_effort": "medium",
    "tools": TOOLS,
    "stream": True,
    "stream_options": {"include_usage": True},
}

print(f"POST {BASE_URL}/chat/completions  model={MODEL}")
t0 = time.time()
first_chunk_at = None
text_parts: list[str] = []
tool_calls: dict[int, dict] = {}
usage = None
finish = None

with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
    with client.stream(
        "POST",
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
    ) as resp:
        print(f"HTTP {resp.status_code}  headers_sent_at={time.time() - t0:.2f}s")
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", "replace")
            print("BODY:", body[:1500])
            raise SystemExit(1)
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if first_chunk_at is None:
                first_chunk_at = time.time() - t0
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = tool_calls.setdefault(tc.get("index", 0), {"name": None, "args": ""})
                    if tc.get("function", {}).get("name"):
                        slot["name"] = tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        slot["args"] += tc["function"]["arguments"]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]

print(f"first_chunk={first_chunk_at:.2f}s  total={time.time() - t0:.2f}s  finish={finish}")
print("text:", "".join(text_parts)[:300] or "(none)")
for idx, slot in sorted(tool_calls.items()):
    print(f"tool_call[{idx}]: {slot['name']} {slot['args'][:200]}")
print("usage:", usage)
