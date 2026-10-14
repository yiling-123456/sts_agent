"""Offline branch evaluation: same entry state, one decision changed, fixed follow-up.

Protocol per branch (see ``local-tools/SPIKE_FINDINGS_restore.md`` for why it is
shaped this way):

1. restore the verified snapshot (raises on drift, so a broken restore can never
   silently contaminate a comparison);
2. replay an optional recorded action prefix — needed when the decision sits
   inside a fight, because the autosave only exists at fight entry;
3. inject the branch action;
4. hand over to the **fixed follow-up policy** and measure the segment.

Statistical honesty is part of the output, not a footnote: every branch runs on
the same seed and therefore the same future RNG stream, so results are *paired*
observations, not independent samples. The report says so and only emits paired
deltas against a named baseline.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.eval.game_client import EvalGameClient
from src.eval.policies import Policy, play_segment
from src.eval.snapshot import SnapshotStore


@dataclass(frozen=True)
class Branch:
    """One candidate continuation of a snapshot."""

    name: str
    inject: dict[str, Any] | None = None
    prefix: tuple[dict[str, Any], ...] = ()
    note: str = ""


@dataclass
class BranchOutcome:
    branch: str
    repeat: int
    seed: str
    entry_floor: int
    entry_hp: int | None
    entry_gold: int | None
    floors_gained: int
    end_floor: int
    end_hp: int | None
    end_gold: int | None
    avg_hp_ratio: float
    victory: bool
    steps: int
    segment_fitness: float
    end_screen: str
    wall_s: float
    injected: dict[str, Any] | None = None
    error: str = ""

    def row(self) -> str:
        flag = "W" if self.victory else " "
        err = f"  <{self.error}>" if self.error else ""
        return (
            f"{self.branch:<18} r{self.repeat} {flag} "
            f"F{self.entry_floor}->{self.end_floor} (+{self.floors_gained}) "
            f"HP {self.end_hp}/{self.entry_hp} gold {self.end_gold} "
            f"fit {self.segment_fitness:>6.1f} steps {self.steps:<4} "
            f"{self.wall_s:>5.1f}s{err}"
        )


@dataclass
class BranchReport:
    snapshot: str
    policy: str
    max_steps: int
    stop_after_floors: int | None
    repeats: int
    outcomes: list[BranchOutcome] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    # ── reporting ──────────────────────────────────────────────────────

    def by_branch(self) -> dict[str, list[BranchOutcome]]:
        grouped: dict[str, list[BranchOutcome]] = {}
        for outcome in self.outcomes:
            grouped.setdefault(outcome.branch, []).append(outcome)
        return grouped

    def means(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for branch, rows in self.by_branch().items():
            n = len(rows)
            out[branch] = {
                "n": n,
                "mean_floors_gained": round(sum(r.floors_gained for r in rows) / n, 3),
                "mean_end_hp": round(sum(r.end_hp or 0 for r in rows) / n, 2),
                "mean_segment_fitness": round(sum(r.segment_fitness for r in rows) / n, 3),
                "wins": sum(1 for r in rows if r.victory),
            }
        return out

    def paired_deltas(self, baseline: str) -> list[dict[str, Any]]:
        """Per-repeat difference against the baseline branch — the valid comparison.

        Pairing comes from the shared seed: repeat *i* of branch A and repeat *i*
        of the baseline entered the identical state and drew the same RNG stream.
        """
        grouped = self.by_branch()
        if baseline not in grouped:
            raise KeyError(f"baseline {baseline!r} not in report")
        base_by_repeat = {o.repeat: o for o in grouped[baseline]}
        rows: list[dict[str, Any]] = []
        for branch, outcomes in grouped.items():
            if branch == baseline:
                continue
            for outcome in outcomes:
                base = base_by_repeat.get(outcome.repeat)
                if base is None:
                    continue
                rows.append(
                    {
                        "branch": branch,
                        "baseline": baseline,
                        "repeat": outcome.repeat,
                        "delta_floors_gained": outcome.floors_gained - base.floors_gained,
                        "delta_end_hp": (outcome.end_hp or 0) - (base.end_hp or 0),
                        "delta_segment_fitness": round(
                            outcome.segment_fitness - base.segment_fitness, 3
                        ),
                        "delta_win": int(outcome.victory) - int(base.victory),
                    }
                )
        return rows

    def table(self) -> str:
        lines = [
            f"snapshot={self.snapshot}  policy={self.policy}  repeats={self.repeats} "
            f"max_steps={self.max_steps} stop_after_floors={self.stop_after_floors}",
            "NOTE: branches share one seed and one RNG stream — these are PAIRED "
            "observations; do not compute independent-sample intervals over them.",
            "",
        ]
        lines.extend(o.row() for o in self.outcomes)
        means = self.means()
        lines.append("")
        for branch, stats in means.items():
            lines.append(
                f"  {branch:<18} n={stats['n']} mean_floors+{stats['mean_floors_gained']} "
                f"mean_end_hp={stats['mean_end_hp']} mean_fit={stats['mean_segment_fitness']} "
                f"wins={stats['wins']}"
            )
        return "\n".join(lines)

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"branch_report_{self.snapshot}.json"
        path.write_text(
            json.dumps(
                {
                    "snapshot": self.snapshot,
                    "policy": self.policy,
                    "max_steps": self.max_steps,
                    "stop_after_floors": self.stop_after_floors,
                    "repeats": self.repeats,
                    "started_at": self.started_at,
                    "finished_at": self.finished_at,
                    "paired_observations": True,
                    "outcomes": [asdict(o) for o in self.outcomes],
                    "means": self.means(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path


class BranchEvaluator:
    """Restores a snapshot, injects a choice, plays out a fixed policy."""

    def __init__(
        self,
        client: EvalGameClient,
        store: SnapshotStore,
        policy: Policy,
        *,
        max_steps: int = 400,
        verify_restore: bool = True,
    ) -> None:
        self.client = client
        self.store = store
        self.policy = policy
        self.max_steps = max_steps
        self.verify_restore = verify_restore

    def run_branch(
        self,
        snapshot: str,
        branch: Branch,
        *,
        repeat: int,
        stop_after_floors: int | None,
        stop_when: Callable[[dict[str, Any]], bool] | None = None,
        settle_between_prefix_actions: float = 0.35,
    ) -> BranchOutcome:
        started = time.time()
        meta, entry = self.store.restore(self.client, snapshot, verify=self.verify_restore)
        entry_floor = meta.floor or 0
        entry = self.client.settled(timeout=60.0, label="entry after restore")

        error = ""
        try:
            for body in branch.prefix:
                if not self.client.send(body):
                    error = f"prefix action rejected: {body}"
                    break
                time.sleep(settle_between_prefix_actions)
            if not error and branch.inject is not None:
                if not self.client.send(branch.inject):
                    error = f"injected action rejected: {branch.inject}"
        except Exception as exc:  # noqa: BLE001 - recorded per branch, never fatal
            error = f"{type(exc).__name__}: {exc}"

        limit = (
            (lambda s: ((s.get("run") or {}).get("floor") or 0) >= entry_floor + stop_after_floors)
            if stop_after_floors
            else None
        )

        def combined_stop(state: dict[str, Any]) -> bool:
            if limit is not None and limit(state):
                return True
            return bool(stop_when and stop_when(state))

        outcome_fields: dict[str, Any] = {
            "steps": 0,
            "start_floor": entry_floor,
            "end_floor": entry_floor,
            "floors_gained": 0,
            "end_hp": meta.hp,
            "end_max_hp": meta.max_hp,
            "end_gold": meta.gold,
            "avg_hp_ratio": 0.0,
            "victory": False,
            "end_screen": entry.get("screen", ""),
            "segment_fitness": 0.0,
        }
        if not error:
            outcome_fields = play_segment(
                self.client,
                self.policy,
                max_steps=self.max_steps,
                start_floor=entry_floor,
                stop_when=combined_stop,
            )

        return BranchOutcome(
            branch=branch.name,
            repeat=repeat,
            seed=meta.seed,
            entry_floor=entry_floor,
            entry_hp=meta.hp,
            entry_gold=meta.gold,
            floors_gained=outcome_fields["floors_gained"],
            end_floor=outcome_fields["end_floor"],
            end_hp=outcome_fields["end_hp"],
            end_gold=outcome_fields["end_gold"],
            avg_hp_ratio=outcome_fields["avg_hp_ratio"],
            victory=outcome_fields["victory"],
            steps=outcome_fields["steps"],
            segment_fitness=outcome_fields["segment_fitness"],
            end_screen=outcome_fields["end_screen"],
            wall_s=round(time.time() - started, 1),
            injected=branch.inject,
            error=error,
        )

    def evaluate(
        self,
        snapshot: str,
        branches: list[Branch],
        *,
        repeats: int = 1,
        stop_after_floors: int | None = None,
        stop_when: Callable[[dict[str, Any]], bool] | None = None,
        on_outcome: Callable[[BranchOutcome], None] | None = None,
    ) -> BranchReport:
        from datetime import datetime, timezone

        report = BranchReport(
            snapshot=snapshot,
            policy=self.policy.name,
            max_steps=self.max_steps,
            stop_after_floors=stop_after_floors,
            repeats=repeats,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        # Interleave repeats so a mid-sweep environment change (game patch, load)
        # hits all branches equally instead of only the last one.
        for repeat in range(1, repeats + 1):
            for branch in branches:
                outcome = self.run_branch(
                    snapshot,
                    branch,
                    repeat=repeat,
                    stop_after_floors=stop_after_floors,
                    stop_when=stop_when,
                )
                report.outcomes.append(outcome)
                if on_outcome is not None:
                    on_outcome(outcome)
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return report
