"""Room ring — one verified-on-demand snapshot per room the player has entered.

This is what makes a player-facing "jump back to an earlier room" possible: the
game only ever saves at room entry (``local-tools/SPIKE_FINDINGS_restore.md``), so
a copy taken while standing in a room *is* that room's entry state, and a ring of
such copies is a rewind history of the run.

Capture rules that matter:

* The key is ``a{act}-f{floor}`` — in this game one map row is one floor, so the
  pair identifies the room uniquely even though combat screens carry no map data.
* Capturing must not disturb play, so the ring uses ``verify=False``: no
  save/quit cycle. The entry digest is recorded the first time the room is
  actually restored (see :meth:`SnapshotStore.restore`).
* The game writes its save *at* room entry, so the watcher waits for the save
  file to change before copying; copying earlier would store the previous room.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.eval.game_client import EvalGameClient, in_run
from src.eval.snapshot import (
    SAVE_BACKUP_SUFFIX,
    SAVE_FILENAME,
    SnapshotMeta,
    SnapshotStore,
    find_save_file,
)


def room_key(state: dict[str, Any]) -> str | None:
    """``a1-f12`` for the run currently being played, else None."""
    if not in_run(state):
        return None
    run = state.get("run") or {}
    floor = run.get("floor")
    if floor is None:
        return None
    return f"a{state.get('act') or 1}-f{floor}"


@dataclass
class RoomEntry:
    key: str
    label: str
    captured_at: str
    act: int | None
    floor: int | None
    screen: str
    character: str
    ascension: int | None
    hp: int | None
    max_hp: int | None
    gold: int | None
    deck_size: int
    seed: str
    node: dict[str, Any] = field(default_factory=dict)
    entry_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RoomRing:
    """Ordered, de-duplicated history of rooms entered this run."""

    def __init__(self, store: SnapshotStore | None = None) -> None:
        self.store = store or SnapshotStore()
        self.root = self.store.root / "ring"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.entries: list[RoomEntry] = self._load()

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> list[RoomEntry]:
        if not self.index_path.is_file():
            return []
        raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        return [RoomEntry(**row) for row in raw]

    def _flush(self) -> None:
        self.index_path.write_text(
            json.dumps([e.as_dict() for e in self.entries], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── capture ────────────────────────────────────────────────────────

    def capture(
        self,
        client: EvalGameClient,
        state: dict[str, Any],
        *,
        node: dict[str, Any] | None = None,
        wait_for_save_change: float = 8.0,
    ) -> RoomEntry | None:
        """Snapshot the room the player is standing in.

        Waits for the game to (re)write its save, because the write happens at
        room entry and a copy taken before it would hold the previous room.
        """
        key = room_key(state)
        if key is None:
            return None
        if any(entry.key == key for entry in self.entries):
            return None

        save_file = find_save_file()
        before = _digest(save_file)
        deadline = time.time() + wait_for_save_change
        while time.time() < deadline and _digest(save_file) == before:
            time.sleep(0.3)

        label = f"ring-{key}"
        if label in self.store.labels():
            # Re-entering a room on a new timeline: keep the newest copy.
            self.store.dir_for(label).rename(self.store.dir_for(f"{label}-prev-{int(time.time())}"))
        meta: SnapshotMeta = self.store.save(client, label, verify=False, notes={"ring_key": key})
        run = state.get("run") or {}
        entry = RoomEntry(
            key=key,
            label=label,
            captured_at=meta.created_at,
            act=state.get("act"),
            floor=run.get("floor"),
            screen=str(state.get("screen") or ""),
            character=meta.character,
            ascension=meta.ascension,
            hp=meta.hp,
            max_hp=meta.max_hp,
            gold=meta.gold,
            deck_size=meta.deck_size,
            seed=meta.seed,
            node=node or {},
        )
        self.entries.append(entry)
        self._flush()
        return entry

    # ── query / rewind ─────────────────────────────────────────────────

    def latest(self) -> RoomEntry | None:
        return self.entries[-1] if self.entries else None

    def find(self, key_or_label: str) -> RoomEntry | None:
        for entry in self.entries:
            if key_or_label in (entry.key, entry.label):
                return entry
        return None

    def rewind(
        self,
        client: EvalGameClient,
        key_or_label: str,
        *,
        verify: bool = True,
        timeout: float = 60.0,
    ) -> tuple[RoomEntry, RoomEntry | None, dict[str, Any]]:
        """Restore a room and return (target, previous, live state).

        Also records the entry digest on the ring row so the panel can show
        whether a given room has ever been re-entered consistently.
        """
        target = self.find(key_or_label)
        if target is None:
            raise KeyError(f"no ring entry for {key_or_label!r}")
        previous = self.latest()
        meta, state = self.store.restore(client, target.label, verify=verify, timeout=timeout)
        target.entry_digest = meta.entry_digest
        self._flush()
        return target, previous, state

    def as_dicts(self) -> list[dict[str, Any]]:
        return [entry.as_dict() for entry in reversed(self.entries)]


def watch_rooms(
    client: EvalGameClient,
    ring: RoomRing,
    *,
    interval: float = 1.0,
    stop: Callable[[], bool] | None = None,
    on_capture: Callable[[RoomEntry], None] | None = None,
    on_state: Callable[[dict[str, Any]], None] | None = None,
    pause: Any | None = None,
) -> None:
    """Poll the game and ring every newly entered room.

    ``pause`` is a thread lock shared with the rewinder: while a restore runs, the
    watcher must not observe the transitional screens and record them as rooms.
    """
    last_node: dict[str, Any] = {}
    while not (stop and stop()):
        if pause is not None and pause.locked():
            time.sleep(interval)
            continue
        try:
            state = client.state()
        except Exception:  # noqa: BLE001 - game may be closed or between screens
            time.sleep(interval)
            continue
        if on_state is not None:
            on_state(state)
        if state.get("screen") == "MAP":
            node = (state.get("map") or {}).get("current_node") or {}
            if node:
                last_node = node
        entry = ring.capture(client, state, node=last_node)
        if entry is not None and on_capture is not None:
            on_capture(entry)
        time.sleep(interval)


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


__all__ = [
    "RoomEntry",
    "RoomRing",
    "SAVE_BACKUP_SUFFIX",
    "SAVE_FILENAME",
    "room_key",
    "watch_rooms",
]
