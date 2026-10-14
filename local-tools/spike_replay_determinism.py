"""Spike 2: given a restored fight start, is replaying the same action sequence
deterministic?

Spike 1 showed a save/restore lands on the fight's opening state (autosave
granularity), not on the moment of quitting. So an in-combat branch can only be
evaluated if (seed + action prefix) re-simulates identically. This script tests
exactly that:

  1. get to a fight start (save_and_quit -> continue_run)
  2. play a short action sequence S, capture state X1
  3. save_and_quit -> continue_run  (back to the same fight start)
  4. replay the *same* S, capture state X2
  5. diff X1 vs X2

DETERMINISTIC  -> in-combat branches are reachable by replaying a prefix and
                  diverging (route b in the design).
DIVERGED       -> only policy-level A/B from the fight start is valid, and a
                  mid-turn snapshot would need a new mod capability.

Usage: python spike_replay_determinism.py [--actions 3]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spike_restore_consistency as spike  # noqa: E402


def back_to_fight_start(client: httpx.Client, timeout: float) -> dict:
    spike.post_action(client, spike.act.save_and_quit())
    spike.wait_for(
        client,
        lambda s: "continue_run" in (s.get("available_actions") or []),
        timeout=max(timeout, 60.0),
        label="main menu",
    )
    spike.post_action(client, spike.act.continue_run())
    spike.wait_for(client, spike.in_run, timeout=max(timeout, 60.0), label="run resumed")
    return spike.wait_settled(client, timeout=max(timeout, 60.0), label="fight start")


def play_sequence(client: httpx.Client, length: int) -> list[dict]:
    """Play mechanically and record the exact action bodies sent."""
    sequence: list[dict] = []
    for _ in range(length):
        state = spike.get_state(client)
        acts = set(state.get("available_actions") or [])
        hand = ((state.get("combat") or {}).get("hand")) or []
        chosen: dict | None = None
        for card in hand:
            if card.get("playable"):
                chosen = spike.act.play_card(card["index"])
                break
        if chosen is None and "end_turn" in acts:
            chosen = spike.act.end_turn()
        if chosen is None:
            break
        if spike.post_action(client, chosen):
            sequence.append(chosen)
        time.sleep(0.45)
    return sequence


def replay(client: httpx.Client, sequence: list[dict]) -> None:
    for body in sequence:
        spike.post_action(client, body)
        time.sleep(0.45)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        if not spike.in_run(spike.get_state(client)):
            print("!! not in a run; run spike_restore_consistency.py first")
            return 2

        start = back_to_fight_start(client, args.timeout)
        print(f"fight start : {spike.summarise_state(start)}")

        sequence = play_sequence(client, args.actions)
        state_x1 = spike.wait_settled(client, timeout=args.timeout, label="after first play")
        print(f"X1 (played {len(sequence)}): {spike.summarise_state(state_x1)}")
        for body in sequence:
            print(f"   sent {body}")

        back_to_fight_start(client, args.timeout)
        replay(client, sequence)
        state_x2 = spike.wait_settled(client, timeout=args.timeout, label="after replay")
        print(f"X2 (same actions): {spike.summarise_state(state_x2)}")

        differences = spike.diff(spike.canonical(state_x1), spike.canonical(state_x2))
        print(f"\ndiffering fields: {len(differences)}")
        for path, va, vb in differences[:15]:
            print(f"   {path}: {spike.brief(va)}  !=  {spike.brief(vb)}")
        print("verdict:", "DETERMINISTIC" if not differences else "DIVERGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
