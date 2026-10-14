"""Verified run-state snapshots — the primitive behind segment continuation and
offline branch evaluation.

Why it is built this way
------------------------
The mod exposes no state serialisation; the game's own autosave is the only
restore primitive (see ``local-tools/SPIKE_FINDINGS_restore.md``). Measured
behaviour:

* ``current_run.save`` is written on **room / fight entry**; ``save_and_quit``
  only rewrites ``replays/latest.mcr``.
* ``save_and_quit`` -> ``continue_run`` re-enters at that autosave point,
  deterministically, and discards anything done afterwards.

So the live state at capture time and the state a restore produces are *not*
guaranteed to be the same — the autosave may predate the capture. A snapshot
that does not record which of the two it re-enters is unusable for paired
comparison, which is why :meth:`SnapshotStore.save` verifies itself: it copies
the save, restores once, stores the **entry digest** it actually got, and
reports whether the live state matched. Every later restore re-checks that
digest and fails loudly on drift.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.eval.game_client import EvalGameClient, canonical_state, in_run, run_summary, state_digest
from src.storage import paths

SAVE_FILENAME = "current_run.save"
SAVE_BACKUP_SUFFIX = ".backup"


def find_save_file() -> Path:
    """Locate the newest in-progress run save.

    Layout: ``%APPDATA%/SlayTheSpire2/steam/<steamid>/modded/profile<N>/saves/``.
    ``STS2_SAVE_FILE`` overrides the search (used by tests and by non-Steam builds).
    """
    override = os.getenv("STS2_SAVE_FILE")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"STS2_SAVE_FILE does not exist: {path}")
        return path

    root = Path(os.environ.get("APPDATA", "")) / "SlayTheSpire2" / "steam"
    if not root.is_dir():
        raise FileNotFoundError(f"Slay the Spire 2 save root not found: {root}")
    candidates = [p for p in root.glob(f"*/modded/profile*/saves/{SAVE_FILENAME}") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"no {SAVE_FILENAME} under {root}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def snapshots_root() -> Path:
    root = paths.data_root() / "eval" / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class SnapshotMeta:
    """Everything needed to re-enter a state and to prove we re-entered it."""

    label: str
    created_at: str
    seed: str
    character: str
    ascension: int | None
    act: int | None
    floor: int | None
    screen: str
    hp: int | None
    max_hp: int | None
    gold: int | None
    deck_size: int
    state_version: int | None
    live_digest: str
    entry_digest: str
    # None when the entry state has not been observed yet: a snapshot taken while
    # playing cannot afford the save/quit cycle, so its entry digest is recorded
    # on the first restore instead (see SnapshotStore.restore).
    live_matches_entry: bool | None
    save_file: str
    save_sha256: str
    save_bytes: int
    verified_at: str | None = None
    restore_count: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_state(cls, state: dict[str, Any], **kwargs: Any) -> SnapshotMeta:
        run = state.get("run") or {}
        return cls(
            seed=str(state.get("run_id") or ""),
            character=str(run.get("character_id") or ""),
            ascension=run.get("ascension"),
            act=state.get("act"),
            floor=run.get("floor"),
            screen=str(state.get("screen") or ""),
            hp=run.get("current_hp"),
            max_hp=run.get("max_hp"),
            gold=run.get("gold"),
            deck_size=len(run.get("deck") or []),
            state_version=state.get("state_version"),
            **kwargs,
        )

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> SnapshotMeta:
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def summary(self) -> str:
        if self.live_matches_entry is None:
            fidelity = "entry unverified (recorded on first restore)"
        elif self.live_matches_entry:
            fidelity = "entry==live"
        else:
            fidelity = "entry!=live (autosave predates capture)"
        return (
            f"{self.label}: {self.character} A{self.ascension} F{self.floor} {self.screen} "
            f"HP={self.hp}/{self.max_hp} gold={self.gold} seed={self.seed} [{fidelity}]"
        )


class SnapshotStore:
    """File-backed store of verified state snapshots."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or snapshots_root()

    def dir_for(self, label: str) -> Path:
        return self.root / label

    def labels(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "snapshot.json").is_file())

    def load(self, label: str) -> SnapshotMeta:
        path = self.dir_for(label) / "snapshot.json"
        if not path.is_file():
            raise FileNotFoundError(f"no snapshot {label!r} in {self.root}")
        return SnapshotMeta.read(path)

    def save(
        self,
        client: EvalGameClient,
        label: str,
        *,
        verify: bool = True,
        timeout: float = 60.0,
        notes: dict[str, Any] | None = None,
    ) -> SnapshotMeta:
        """Capture the current autosave, then (by default) prove it re-enters.

        Verification costs one ``save_and_quit`` + ``continue_run`` cycle and is
        what makes the snapshot safe to reuse: it records the digest the restore
        actually produces, not the digest of whatever was on screen.
        """
        target = self.dir_for(label)
        if target.exists():
            raise FileExistsError(f"snapshot {label!r} already exists at {target}")
        target.mkdir(parents=True)

        save_file = find_save_file()
        live = client.settled(timeout=timeout, label="live state before snapshot")
        save_bytes = save_file.read_bytes()
        (target / SAVE_FILENAME).write_bytes(save_bytes)
        backup = save_file.with_name(save_file.name + SAVE_BACKUP_SUFFIX)
        if backup.is_file():
            shutil.copy2(backup, target / (SAVE_FILENAME + SAVE_BACKUP_SUFFIX))

        meta = SnapshotMeta.from_state(
            live,
            label=label,
            created_at=_now(),
            live_digest=state_digest(live),
            entry_digest="",
            live_matches_entry=False,
            save_file=str(save_file),
            save_sha256=_sha256(save_bytes),
            save_bytes=len(save_bytes),
            notes=notes or {},
        )

        if verify:
            restored = self._restore_and_capture(client, timeout=timeout)
            meta.entry_digest = state_digest(restored)
            meta.live_matches_entry = meta.entry_digest == meta.live_digest
            meta.verified_at = _now()
            meta.restore_count = 1
            persist_entry_state(self, label, restored)
        else:
            # Mid-play capture: the save/quit cycle that verification needs would
            # throw the player out of the run. The entry digest is recorded on the
            # first restore instead, which is still a real check from then on.
            meta.entry_digest = ""
            meta.live_matches_entry = None
        meta.write(target / "snapshot.json")
        return meta

    def restore(
        self,
        client: EvalGameClient,
        label: str,
        *,
        verify: bool = True,
        timeout: float = 60.0,
    ) -> tuple[SnapshotMeta, dict[str, Any]]:
        """Put the snapshot's save back and re-enter it.

        Returns the metadata and the settled state. With ``verify=True`` the
        digest must match the recorded entry digest, otherwise
        :class:`RestoreMismatch` is raised with the first differing fields.
        """
        meta = self.load(label)
        target = self.dir_for(label)
        save_file = find_save_file()
        shutil.copy2(target / SAVE_FILENAME, save_file)
        backup_src = target / (SAVE_FILENAME + SAVE_BACKUP_SUFFIX)
        if backup_src.is_file():
            shutil.copy2(backup_src, save_file.with_name(save_file.name + SAVE_BACKUP_SUFFIX))

        state = self._restore_and_capture(client, timeout=timeout)
        meta.restore_count += 1
        if verify:
            digest = state_digest(state)
            if not meta.entry_digest:
                # First restore of a mid-play capture: this *is* the entry state.
                meta.entry_digest = digest
                meta.verified_at = _now()
                persist_entry_state(self, label, state)
            elif digest != meta.entry_digest:
                raise RestoreMismatch(label, meta, state)
        meta.write(target / "snapshot.json")
        return meta, state

    def _restore_and_capture(self, client: EvalGameClient, *, timeout: float) -> dict[str, Any]:
        """Copy the save in, then re-enter it.

        Two entry conditions: a live run (quit first, which is what makes the
        game write its autosave) or a stalled screen — after a branch plays to a
        defeat the game sits on GAME_OVER, where Continue is not offered until we
        walk back to a menu.
        """
        current = client.state()
        if in_run(current):
            client.save_and_quit(timeout=timeout)
        else:
            self._ensure_continue_offered(client, timeout=timeout)
        return client.continue_run(timeout=timeout)

    @staticmethod
    def _ensure_continue_offered(client: EvalGameClient, *, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            available = set(client.state().get("available_actions") or [])
            if "continue_run" in available:
                return
            for candidate in (
                "return_to_main_menu",
                "close_main_menu_submenu",
                "confirm_modal",
                "dismiss_modal",
            ):
                if candidate in available:
                    client.send({"action": candidate})
                    break
            else:
                return
            time.sleep(0.6)

    def diff_against(self, label: str, state: dict[str, Any]) -> list[tuple[str, Any, Any]]:
        """Field-level diff between the recorded entry state and ``state``."""
        meta = self.load(label)
        recorded = json.loads((self.dir_for(label) / "entry_state.json").read_text(encoding="utf-8"))
        return _diff(canonical_state(recorded), canonical_state(state))

    def describe(self) -> str:
        rows = [self.load(label).summary() for label in self.labels()]
        return "\n".join(rows) if rows else "(no snapshots)"


class RestoreMismatch(RuntimeError):
    """Raised when a restore does not reproduce the recorded entry state."""

    def __init__(self, label: str, meta: SnapshotMeta, state: dict[str, Any]) -> None:
        recorded_path = snapshots_root() / label / "entry_state.json"
        detail = ""
        if recorded_path.is_file():
            recorded = json.loads(recorded_path.read_text(encoding="utf-8"))
            diffs = _diff(canonical_state(recorded), canonical_state(state))
            detail = "; ".join(f"{p}: {a!r} != {b!r}" for p, a, b in diffs[:5])
        super().__init__(
            f"snapshot {label!r} did not restore faithfully "
            f"(entry digest {meta.entry_digest}, got {state_digest(state)})"
            + (f" | {detail}" if detail else "")
            + f" | current: {run_summary(state)}"
        )


def _diff(a: Any, b: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    out: list[tuple[str, Any, Any]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append((f"{path}/{key}", a.get(key, "<missing>"), b.get(key, "<missing>")))
                continue
            out.extend(_diff(a[key], b[key], f"{path}/{key}"))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((f"{path}/len", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(_diff(x, y, f"{path}[{i}]"))
    elif a != b:
        out.append((path, a, b))
    return out


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:32]


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def persist_entry_state(store: SnapshotStore, label: str, state: dict[str, Any]) -> None:
    """Record the canonical entry state next to the save file.

    Kept separate from :class:`SnapshotMeta` because it is a full payload, not a
    summary — it is what makes a failed restore debuggable field by field.
    """
    path = store.dir_for(label) / "entry_state.json"
    path.write_text(
        json.dumps(canonical_state(state), indent=2, ensure_ascii=False), encoding="utf-8"
    )
