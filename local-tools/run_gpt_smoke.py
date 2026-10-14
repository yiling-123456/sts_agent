"""The requested one-shot GPT smoke test (original snippet, working base_url).

The original script pointed at https://newapi.metamind.work/v1 and hard-coded the
key. Direct HTTPS from this machine cannot complete the TLS handshake for
httpx/urllib clients, so the URL below targets the local blocking-TLS forwarder
(`relay_proxy.py`), and the key is read from the environment / project `.env`
(`STS2_GPT_API_KEY`) instead of being committed to the repository.

Run:  AgenticSTS\\AgenticSTS\\.venv\\Scripts\\python.exe run_gpt_smoke.py
"""

from __future__ import annotations

import os
from pathlib import Path

from openai import OpenAI


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


client = OpenAI(
    base_url="http://127.0.0.1:8129/v1",
    api_key=gpt_api_key(),
)

completion = client.chat.completions.create(
    model="gpt-5.5",
    messages=[
        {"role": "user", "content": "Explain quantum entanglement in one paragraph."}
    ],
)

print(completion.choices[0].message.content)
