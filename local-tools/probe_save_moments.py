"""Probe: when exactly does the game write current_run.save?

Answering this decides what a player-facing "click a past map node to rewind"
feature can promise:

* a save taken at **map-screen time** makes "restart this floor" possible;
* a save taken only at **room entry** makes it possible only for the room.

Runs the mechanical policy, hashing the run save after every action that changes
the screen, and dumps the map payload when the map appears (that payload is what
a clickable-node UI would have to key snapshots against).

Usage: python probe_save_moments.py
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "AgenticSTS"
sys.path.insert(0, str(AGENT_DIR))

from src.eval.game_client import EvalGameClient, in_run, run_summary  # noqa: E402
from src.eval.policies import MechanicalPolicy  # noqa: E402
from src.eval.snapshot import find_save_file  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    save = find_save_file()
    policy = MechanicalPolicy()
    with EvalGameClient() as client:
        print(f"save file     : {save}")
        print(f"start         : {run_summary(client.state())}")
        last_digest = digest(save)
        print(f"start digest  : {last_digest}")

        for step in range(60):
            state = client.settled(timeout=45.0, label="probe step")
            if not in_run(state):
                print(f"run ended at step {step}: {state.get('screen')}")
                break
            screen = state.get("screen")
            floor = (state.get("run") or {}).get("floor")
            body = policy.decide(state)
            if body is None:
                print(f"policy stuck: {screen} actions={state.get('available_actions')}")
                break
            client.send(body)
            time.sleep(0.4)
            current = digest(save)
            if current != last_digest:
                print(f"SAVE WRITTEN  step {step:>3} screen={screen} floor={floor} -> {current}")
                last_digest = current
            if screen == "MAP" and step > 0:
                payload = state.get("map") or {}
                print(f"\nmap payload keys: {sorted(payload)}")
                nodes = payload.get("nodes") or payload.get("options") or []
                print(f"map nodes: {len(nodes)}")
                for node in nodes[:6]:
                    print(f"   {node}")
                print("\nfinal map digest:", last_digest)
                return 0
            if step % 10 == 0:
                print(f"  step {step:>3} screen={screen} F{floor} digest={last_digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
