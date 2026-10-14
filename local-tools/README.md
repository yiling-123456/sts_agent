# local-tools

Operator scripts written while bringing this checkout up on the GPT-5.5 relay
(2026-10-13). None of them are part of the agent runtime — the agent itself is
unchanged and runs from `scripts.run_agent`.

| File | Purpose |
|---|---|
| `relay_proxy.py` | Local `HTTP → HTTPS` forwarder for `newapi.metamind.work`. The relay only completes *blocking* TLS handshakes, so httpx-based clients (the agent, the OpenAI SDK) hang against it directly. Point clients at `http://127.0.0.1:8129/v1`. |
| `run_gpt_smoke.py` | The one-shot OpenAI-SDK snippet (quantum-entanglement prompt) against the working base URL. |
| `smoke_gpt_relay.py` | Payload-realistic probe: streaming + tools + `reasoning_effort` + `temperature=0`, i.e. the exact shape `V2Backend` sends for a `gpt-5*` model. |
| `effort_probe.py` | Sends one combat prompt at `reasoning_effort` low/medium/high and prints reasoning tokens + latency, to verify the relay honours the parameter. |
| `model_probe.py` | One small prompt per model name (default effort `medium`), reporting status, served model, reasoning tokens and latency — run this before pointing a 50-minute agent run at a new model. |
| `reasoning_stream_probe.py` | Streams a tool-bearing request and counts `delta.reasoning_content` frames, to tell "the model reasons but the relay hides it" from "our capture path is off". gpt-6.1-sol returns 0 frames (only a token count), gpt-5.6-sol / gpt-5.5 return visible reasoning. |
| `sts_progress.py` | Summarises the newest `logs/run_*.jsonl`: decisions, floor/act trajectory, action mix, state types, non-OK action results. |
| `llm_stats.py` | Token + latency totals over one or more run logs. |
| `GPT_AGENTICSTS_A0_RUN_REPORT.md` | Report for the two A0 runs (Ironclad + Silent) done on GPT-5.5. |

## Key handling

No credentials are stored in this directory. Every script that needs the relay
key reads `STS2_GPT_API_KEY` from the environment first and then falls back to
`AgenticSTS/AgenticSTS/.env` (which is git-ignored). The relay proxy itself
needs no key — it forwards the client's `Authorization` header untouched.

## Usage

Paths below are relative to the repository root (`…\sts\AgenticSTS`), where the
venv created for this work lives at `AgenticSTS\.venv`.

```powershell
cd C:\Users\yilin\Desktop\sts\AgenticSTS
$py = "AgenticSTS\.venv\Scripts\python.exe"

# 1) start the forwarder (keep it running for the whole session)
& $py local-tools\relay_proxy.py

# 2) verify the credential end to end
& $py local-tools\run_gpt_smoke.py

# 3) check the model's thinking depth actually varies with effort
& $py local-tools\effort_probe.py
```
