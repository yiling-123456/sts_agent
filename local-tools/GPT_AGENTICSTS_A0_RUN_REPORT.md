# AgenticSTS × GPT-5.5 — A0 run report

Date: 2026-10-13 · Workspace: `C:\Users\yilin\Desktop\sts` · Game build served by mod 0.5.5 at `localhost:8128`

## 1. Credentials & wiring

| Item | Value |
|---|---|
| Relay | `https://newapi.metamind.work/v1` (new-api `v1.0.0-rc.30`, nginx 1.19.6) |
| Local entry point | `http://127.0.0.1:8129/v1` |
| Key | stored in `AgenticSTS/AgenticSTS/.env` → `STS2_GPT_API_KEY` |
| Model | `gpt-5.5` on all four tiers (fast / strategic / analysis / evolution) |
| Family | `STS2_MODEL_FAMILY=gpt`, `STS2_MODEL_FAMILY_FALLBACK=gpt` |
| Data repo | `STS2_DATA_REPO=…\AgenticSTS-Data` (frozen L4/L5 stores) |

**Why a local proxy.** Direct HTTPS from this machine to the relay never completes: `httpx`/`urllib` drive
the TLS handshake through a memory-BIO / non-blocking path, and the relay answers nothing
(`ConnectTimeout … _ssl.c:1063: The handshake operation timed out`), while a blocking `ssl.wrap_socket`
handshake succeeds on every attempt with byte-identical ClientHello content. `relay_proxy.py` accepts
plain HTTP on `127.0.0.1:8129` and forwards over a blocking TLS socket, relaying the response body
byte-for-byte so streaming tool-call completions keep working.

**Tier models.** The registry's GPT entries (`gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-thinking`) are not served
by this relay. Its catalog is `gpt-5.5`, `gpt-5.6-{luna,sol,terra}`, `gpt-6*`, `kimi-*`; all tiers were
pinned to `gpt-5.5`, which is also the only naming that hits the backend's `gpt-5` reasoning_effort branch.

## 2. Runs

| | Run 1 | Run 2 |
|---|---|---|
| run_id | `20261013_222053_b2ae67db` | `20261013_232450_7c947efc` |
| Character / ascension | The Ironclad / A0 | The Silent / A0 |
| Outcome | **defeat**, floor 33 | **defeat**, floor 33 |
| Ended at | Act 2 boss *The Insatiable* | Act 2 boss *Crusher + Rocket* |
| Fitness | 106.9 | 111.2 |
| Wall clock | 2974 s (49.6 min) | 3137 s (52.3 min) |
| Player actions | 379 | 469 |
| Strategic LLM calls | 44 | 52 |
| LLM requests logged | 284 | 269 |
| Input / output tokens | 431,678 / 115,916 | 484,513 / 118,921 |
| Cache-read tokens | 78,848 | 106,240 |
| Median / max call latency | 13.0 s / 57.5 s | 12.5 s / 87.5 s |
| Postrun (memory → skills → evolution) | all `done` | all `done` |

Both sessions exited with process code 1 at teardown; every postrun stage logged `done`, run history and
memory/skill stores were written, so this is a shutdown artifact of the harness' `os._exit`, not a failed run.

## 3. Postrun effects (written to `AgenticSTS-Data`)

Run 2 (Silent) detail:

- **Memory**: 15 combat episodes, 2 route memories, 5 event memories, 30 card memories updated;
  deck-build analysis distilled to plan `shiv` (tags `shiv`, `defeat`, 12 card roles).
- **Guides consolidated**: combat 5, route 1, deck 1, event 5 — including *Bygone Effigy* v20,
  *Ceremonial Beast* v30, *Fogmog* v30, *multi:Crusher+Rocket* v35, *multi:Myte+Myte* v44,
  Act 1 route v54, deck `silent/shiv` v40, events NEOW v69 / PAEL v19 / THIS_OR_THAT v19.
- **Skills**: 5 mistakes reviewed by the mistake-driven critic, all judged `no_skill_needed`
  (loss ratios 0.13–0.80 vs baselines), so 0 candidates reached the pre-write A/B gate and the
  library stayed at 20 active (15 seeds + 5 stubs).
- **Non-combat scoring**: 5 skills scored end-of-run, score 44.6 (floor 33, boss HP 64, 1 boss killed).
- **Evolution**: 5 tool-use rounds, 205,393 input / 8,028 output tokens, 3 `author_tool` actions —
  **all three rejected** by the engine's own test-case validator (`execute() missing required
  positional arguments`), so no new dynamic tool was persisted. 0 model fallbacks.
- **Write gate**: 15 conflicts judged, 0 candidates persisted.

Store totals after both runs: 3,262 combat episodes · 408 route · 562 card-build · 349 card memories ·
1,567 event memories · 184 combat guides · 9 route · 8 deck · 146 event guides.

## 4. Known gaps on this machine

1. **L3 game knowledge is empty** — `GameKnowledge loaded: 0 cards, 0 monsters, 0 relics…`. The repo does
   not ship `data/knowledge/upstream/*.json`; regenerating needs GDRE Tools (`gdre_tools.exe`) to unpack
   the game PCK, or `sync_upstream_data` against the mod's `mcp_server/data/eng/*.json`. Neither is present.
   Gameplay still works (card text arrives in the mod's state payload) but one of the five memory layers is thin.
2. **No embedding model on the relay** — write-gate conflict detection requests `text-embedding-3-large`
   and gets `503 model_not_found`, then falls back to lexical-only matching (may flag false positives).
3. **Monitor dashboard has no UI** — `frontend/` was never built; the server on `:8081` exposes REST/WS only.
   The mount decision happens at agent startup, so building `dist/` requires starting the agent afterwards.
4. **Direct relay access** — clients must use `http://127.0.0.1:8129/v1` while the proxy runs; the raw
   `https://newapi.metamind.work/v1` URL is unusable from this machine with httpx-based clients.

## 5. Re-running

```powershell
# 1) relay proxy (needed by every LLM call)
cd C:\Users\yilin\Desktop\sts\AgenticSTS
& AgenticSTS\.venv\Scripts\python.exe local-tools\relay_proxy.py

# 2) A0 run, e.g. Silent
cd C:\Users\yilin\Desktop\sts\AgenticSTS\AgenticSTS
$env:PYTHONUNBUFFERED="1"
& .venv\Scripts\python.exe -m scripts.run_agent --steps 500 --runs 1 `
    --character Silent --ascension 0 --abandon-existing `
    --experiment-tag gpt55-silent-a0-<date>

# helpers (run from the same directory)
& .venv\Scripts\python.exe ..\local-tools\sts_progress.py    # decision/floor/action summary of the newest run
& .venv\Scripts\python.exe ..\local-tools\llm_stats.py logs\run_*.jsonl   # token + latency totals
```

History rows land in `AgenticSTS-Data\runs\history.jsonl` (tagged `gpt55-ironclad-a0-20261013` /
`gpt55-silent-a0-20261013`); per-decision trajectories in `AgenticSTS\logs\run_*.jsonl`.
