# Spike findings — save/restore fidelity & replay determinism

Date: 2026-10-14 · Game v0.107.1 · mod 0.5.5 · seed `366MTMWR9F` (Ironclad A0, F2 combat)

Scripts: `spike_restore_consistency.py` (rounds 1–3), `spike_replay_determinism.py`.

## Why this spike

Segment continuation and offline branch evaluation both rest on one premise: a
state can be captured and re-entered, and re-entering it is faithful enough that
different continuations are comparable. Everything else in the evaluation design
is harness work; this premise is the part the *game* has to support.

## What was measured

**1. Save → quit → continue is deterministic, but coarse.**

Three rounds of: play 3 mechanical actions → capture `/state` (A) → `save_and_quit`
→ `continue_run` → capture `/state` (B) → diff A vs B.

| | A (live) | B (restored) |
|---|---|---|
| hand | `[Bash, Defend]` | `[Defend+, Defend, Strike+, Bash, Defend]` |
| enemy HP | 28/37 | **37/37** |
| discard pile | 3 cards | **0 cards** |
| turn | 1 | 1 |

Verdict: **49 fields differ, identically in all 3 rounds.** The restore lands on
the **opening state of the fight**, not on the moment of quitting: the actions
taken after the last autosave are gone. Restoring twice returns the same state,
so restore itself is deterministic and repeatable; it is the *granularity* that
is coarse.

**2. Which file carries the state.**

| File | Written when |
|---|---|
| `…\modded\profile1\saves\current_run.save` (+ `.backup`) | on **room/fight entry** (autosave); 46 KB |
| `…\modded\profile1\replays\latest.mcr` | on `save_and_quit`; ~1.9 KB |
| `…\saves\history\<epoch>.run` | when a run finishes |

`save_and_quit` did **not** rewrite `current_run.save` — only the replay file.
So the snapshot primitive is the autosaved `current_run.save`, and the restore
point is the last autosave.

**3. Replaying a fixed action sequence re-simulates exactly.**

From the restored fight start: play a recorded 2-action sequence → X1; restore
again; replay the *same* bodies → X2. **0 differing fields → DETERMINISTIC.**
Seed identity survived every restore (`run_id == Rng.StringSeed`, exposed by
[GameStateService.cs:200](../AgenticSTS-Mod/STS2AIAgent/Game/GameStateService.cs#L200)).

## Consequences for the four evaluation modes

| Mode | Verdict |
|---|---|
| 1. Full-run evaluation | Unaffected — already runs end-to-end. |
| 2. Segment continuation | **Feasible today** at room/fight-entry granularity: snapshot `current_run.save`, copy it back, `continue_run`. Room boundaries are exactly where route / rest / shop / card / event decisions live. |
| 3. Controlled policy substitution | **Feasible** — and this explains why the existing skill A/B replay works: the restore point *is* the fight start, so replaying a fight under policy A vs policy B is a clean paired comparison. |
| 4. Offline branch evaluation | Room-boundary branches (rest vs smith, card pick, route, shop): **works with save snapshots**. In-combat single-action branches: **feasible via prefix replay** — restore to fight start, replay the recorded prefix (deterministic, proven above), then diverge at the branch action. |

## Methodology constraints this creates

- **Paired, not independent.** Both branches run on the same seed and therefore
  the same future RNG stream. Report paired differences; do not feed these into
  Wilson/bootstrap CIs built for independent samples.
- **Autosave granularity is a hard boundary.** A mid-turn branch cannot be
  snapshotted directly; it must be reached by replaying a prefix.
- **Replay needs a complete, ordered action log** (the run JSONL already records
  every action, so the prefix is recoverable, but it must include the exact
  action bodies and be free of retries/duplicates).
- **Screen animation.** Reading `/state` the instant a screen appears returns a
  half-built payload (empty hand, energy 0). Any comparison must wait for the
  state to settle — `spike_restore_consistency.wait_settled()` does this.

## Addendum (2026-10-14, `probe_save_moments.py`): what the save contains, per screen

Question: can a player "rewind by clicking a visited map node"? That needs to
know which screens the game ever saves *at*.

Measured round trip — finish the F2 fight, arrive on the map at F2:

* `current_run.save` digest **did not change** across mid-combat → fight won →
  map screen (`1c65c51b9d09` throughout);
* capturing the save *while standing on the map* and restoring it lands on the
  **F2 fight entry**, not the map (`entry_digest 19ec9cb7…`, identical to the
  snapshot taken mid-fight).

So the save always holds the **last room entry**, and map screens are never
saved. Consequences:

| Player wish | Feasible? |
|---|---|
| Restart the current fight / room | ✅ this is exactly what the save holds |
| Replay the current floor's room | ✅ restore the snapshot of the node you travelled to this floor |
| Return to the map and re-pick the path | ❌ no map-state save exists; the closest is restoring the **previous** room's entry and replaying it (same seed ⇒ same events ⇒ same map options) |
| Jump to any visited node | ✅ if a per-node-entry snapshot ring is kept (~46 KB per room) |

The map payload already carries everything a clickable-node UI would key on:
`nodes[].{row,col,node_type,state,visited,is_current,parents,children}` plus
`current_node`, `available_nodes`, `starting_node`, `boss_node` (59 nodes at F2).

## Minimal mod additions still worth building

1. `GET /snapshot_info` → run-save path, sha256, mtime, `run_id`, floor, screen.
2. `POST /snapshot {label}` / `POST /restore {label}` → mod-side copy of
   `current_run.save` (+ `.backup`), avoiding races with the game's own writer.
3. Later, if seed entry exists in the UI: `POST /new_run {seed}` (not required —
   snapshot + prefix replay already covers segment continuation and branching).
