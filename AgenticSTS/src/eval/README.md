# Evaluation harness (`src/eval`)

Four evaluation modes over the live game, built on the one restore primitive the
mod actually offers — the game's own autosave. Measured behaviour and the
supporting spike are in
[`local-tools/SPIKE_FINDINGS_restore.md`](../../local-tools/SPIKE_FINDINGS_restore.md).

| Mode | Entry point | Status |
|---|---|---|
| 1. Full-run evaluation | `scripts/run_agent.py`, `scripts/run_ablation.py` | pre-existing |
| 2. Segment continuation | `scripts/eval_snapshot.py restore --handoff` + `run_agent --resume-saved` | works (demonstrated: agent takes over a restored F2 fight) |
| 3. Controlled policy substitution | segment runs with `--extra-arg=--no-skills` / `--no-memory`, or a fixed `MechanicalPolicy` per arm | plumbing in place |
| 4. Offline branch evaluation | `scripts/eval_branch.py` | works (demonstrated: 2 branches × 2 repeats, reproducible paired deltas) |

## The restore primitive, and why snapshots verify themselves

* `current_run.save` is written on **room / fight entry**; `save_and_quit` only
  rewrites `replays/latest.mcr`.
* `save_and_quit` → `continue_run` re-enters at that autosave point,
  deterministically, and **discards everything done since**.
* Therefore the state on screen when you capture is not necessarily the state you
  will re-enter.

`SnapshotStore.save()` therefore restores once, records the **entry digest** it
actually got, and reports whether the live state matched
(`live_matches_entry`). Every later `restore()` re-checks that digest and raises
`RestoreMismatch` with the first differing fields — a broken restore can never
silently contaminate a comparison. `entry_state.json` keeps the full canonical
payload next to the save file so a failure is debuggable.

## Usage

```bash
# 1. capture a state (self-verifying; costs one save/quit/continue cycle)
python -m scripts.eval_snapshot save --label f12-rest
python -m scripts.eval_snapshot list
python -m scripts.eval_snapshot verify --label f12-rest

# 2. segment continuation: restore, then let the LLM agent play it out
python -m scripts.eval_snapshot restore --label f12-rest --handoff \
    --tag seg-f12-skills-on --steps 500

# 3. policy substitution: same segment, different agent configuration
python -m scripts.eval_snapshot restore --label f12-rest --handoff \
    --tag seg-f12-no-skills --steps 500 --extra-arg=--no-skills

# 4. branch evaluation: same entry state, one decision changed, fixed follow-up
python -m scripts.eval_branch --snapshot f12-rest --repeats 3 --floors 5 \
    --branch rest:rest --branch smith:smith --baseline rest
```

Snapshots and reports land in `<data repo>/eval/snapshots/<label>/`
(`snapshot.json`, `entry_state.json`, the save file, `branch_report_<label>.json`).
Segment bookkeeping and agent stdout land in `<data repo>/eval/segments/`.

## Branch evaluation protocol

Per branch, per repeat:

1. restore the verified snapshot → entry state `S0`;
2. replay `branch.prefix` (recorded action bodies) — required for in-combat
   decisions, since the autosave only exists at fight entry;
3. inject `branch.inject` (the decision under test);
4. hand over to the **fixed follow-up policy** and measure the segment.

`BranchEvaluator.evaluate()` interleaves repeats across branches so a mid-sweep
environment change cannot favour one arm.

Measured segment outcome uses the same currency as the agent's own runs:
`floors_gained`, end HP / gold, `victory`, and `segment_fitness` with the weights
from `src/state/run_state.py::fitness` (`100·victory + 2·floor + 50·avg_hp% + 20·gold_eff`)
applied to the segment rather than the whole run.

## What the numbers do and do not mean

* **Paired, not independent.** All branches share one seed and therefore the same
  future RNG stream. `BranchReport.paired_deltas()` reports per-repeat
  differences against a baseline; the report and the CLI both state that
  independent-sample intervals (Wilson, bootstrap over runs) do **not** apply.
* **Autosave granularity is a hard boundary.** A mid-turn decision cannot be
  snapshotted; it must be reached by replaying a prefix (proven deterministic:
  same seed + same action prefix ⇒ identical state).
* **The follow-up policy is part of the claim.** A branch result is only valid
  *for that fixed follow-up*; substituting the LLM agent for the mechanical
  policy changes what the comparison measures.
* Repeats of the same (snapshot, branch, policy) triple are deterministic, so
  repeats validate the harness rather than sampling noise. Diversity comes from
  different seeds or different follow-up policies, not from repeating one cell.

## Not built

* Mod-side `GET /snapshot_info` + `POST /snapshot`/`/restore`: the file-copy
  path above works today, but a mod-side copy would remove the race with the
  game's own writer and let a snapshot be taken without quitting.
* `POST /new_run {seed}`: only needed if the UI can accept a seed; prefix replay
  already covers reaching an arbitrary point of a run.
