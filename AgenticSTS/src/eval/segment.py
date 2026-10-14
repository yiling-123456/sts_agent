"""Segment continuation: re-enter a snapshot and hand control to the real agent.

This is mode 2 of the evaluation design — start from a state captured on a real
trajectory instead of from character select, so the *task* is controlled (act,
floor, deck, HP, gold) while the agent is unchanged.

The handoff is deliberately a subprocess: the agent owns its own memory/skill
load, logging and postrun, and one segment per process keeps those side effects
isolated the way ``run_ablation`` already does it. The snapshot restore happens
here, before the agent starts, so the agent only ever has to *resume* a saved run
— which needs ``scripts.run_agent --resume-saved``, because the default policy
abandons any in-progress run past floor 1.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from src.eval.game_client import EvalGameClient
from src.eval.snapshot import SnapshotMeta, SnapshotStore
from src.storage import paths


@dataclass
class SegmentRun:
    """Bookkeeping for one segment handed to the agent."""

    snapshot: str
    tag: str
    character: str
    ascension: int | None
    entry_floor: int
    command: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot,
            "tag": self.tag,
            "character": self.character,
            "ascension": self.ascension,
            "entry_floor": self.entry_floor,
            "command": self.command,
        }


def default_python() -> str:
    """Interpreter to launch the agent with — the venv running this process."""
    return sys.executable


def build_command(
    run: SegmentRun,
    *,
    steps: int,
    python: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    return [
        python or default_python(),
        "-m",
        "scripts.run_agent",
        "--steps",
        str(steps),
        "--runs",
        "1",
        "--resume-saved",
        "--character",
        run.character,
        "--ascension",
        str(run.ascension if run.ascension is not None else 0),
        "--experiment-tag",
        run.tag,
        *extra_args,
    ]


def restore_for_segment(
    client: EvalGameClient,
    store: SnapshotStore,
    label: str,
    *,
    verify: bool = True,
) -> SnapshotMeta:
    """Restore the snapshot and confirm the agent will find it in-progress."""
    meta, state = store.restore(client, label, verify=verify)
    run = state.get("run") or {}
    if not run.get("floor"):
        raise RuntimeError(f"restored snapshot {label!r} is not inside a run: {state.get('screen')}")
    return meta


def launch(
    meta: SnapshotMeta,
    *,
    tag: str,
    steps: int = 500,
    python: str | None = None,
    extra_args: Sequence[str] = (),
    log_dir: Path | None = None,
    wait: bool = False,
) -> tuple[SegmentRun, subprocess.Popen | None]:
    """Spawn the agent on the restored save.

    Returns the run record and, when ``wait=False``, the live process so a caller
    can supervise it (the branch harness watches it the way ``run_ablation`` does).
    """
    run = SegmentRun(
        snapshot=meta.label,
        tag=tag,
        character=meta.character or "Ironclad",
        ascension=meta.ascension,
        entry_floor=meta.floor or 0,
        command=[],
    )
    run.command = build_command(run, steps=steps, python=python, extra_args=extra_args)

    log_root = log_dir or (paths.data_root() / "eval" / "segments")
    log_root.mkdir(parents=True, exist_ok=True)
    (log_root / f"{tag}.json").write_text(
        json.dumps(run.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if wait:
        subprocess.run(run.command, cwd=str(paths.project_root()), check=False)
        return run, None

    stdout_path = log_root / f"{tag}.stdout.log"
    handle = stdout_path.open("w", encoding="utf-8")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(  # noqa: S603 - fixed argv built above
        run.command,
        cwd=str(paths.project_root()),
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=env,
    )
    return run, process


def outcome_from_history(tag: str) -> dict[str, Any] | None:
    """Read back the segment's row from the shared run history."""
    history = paths.runs_history_file()
    if not history.is_file():
        return None
    found: dict[str, Any] | None = None
    for line in history.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("experiment_tag") == tag:
            found = row
    return found


def describe_run(run: SegmentRun) -> str:
    return (
        f"segment {run.tag}: {run.character} A{run.ascension} from F{run.entry_floor} "
        f"(snapshot {run.snapshot})\n  " + " ".join(shlex.quote(part) for part in run.command)
    )
