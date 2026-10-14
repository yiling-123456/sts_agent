"""Spike: is a save -> quit -> continue cycle state-faithful?

This is the make-or-break premise for segment continuation and offline branch
evaluation: if the game's own save restores a state exactly (including
mid-combat), then a branch evaluator can snapshot once and re-run many policies
from it. If not, the fallback is seed+action replay.

Protocol per round:
  1. capture canonical /state            ->  A   (live state, ideally mid-combat)
  2. hash every file under the save directory
  3. POST save_and_quit, wait for the main menu
  4. hash the save directory again (did the game rewrite progress.save?)
  5. POST continue_run, wait for a run screen
  6. capture canonical /state            ->  B
  7. diff A vs B field by field; also compare run_id (== Rng.StringSeed)

If no run is in progress it starts one and plays mechanically (no LLM) until it
is inside a fight, so the restore point is a mid-combat state.

Usage:
    python spike_restore_consistency.py                 # 3 rounds, auto-drive
    python spike_restore_consistency.py --rounds 5 --drive-floor 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx

AGENT_DIR = Path(__file__).resolve().parent.parent / "AgenticSTS"
sys.path.insert(0, str(AGENT_DIR))
from src.mcp_client import actions as act  # noqa: E402

MOD_BASE = os.environ.get("STS2_MCP_URL", "http://127.0.0.1:8128")
SAVE_ROOT = Path(os.environ["APPDATA"]) / "SlayTheSpire2" / "steam"

# Fields that legitimately differ between two observations of the same state.
VOLATILE_KEYS = {"request_id"}
ACTIVE_SCREENS = {"MAIN_MENU", "GAME_OVER", "CHARACTER_SELECT", "TIMELINE", "OVERLAY", "UNKNOWN"}


# ── mod API ────────────────────────────────────────────────────────────


def get_state(client: httpx.Client) -> dict[str, Any]:
    resp = client.get(f"{MOD_BASE}/state")
    resp.raise_for_status()
    return resp.json().get("data") or {}


def post_action(client: httpx.Client, body: dict[str, Any]) -> bool:
    try:
        payload = client.post(f"{MOD_BASE}/action", json=body).json()
    except Exception:  # noqa: BLE001
        return False
    if not payload.get("ok", True):
        return False
    data = payload.get("data") or {}
    status = str(data.get("status", "ok")).lower()
    # The mod reports "completed" for actions that changed the screen, and
    # "ok"/"applied" for in-place ones — treat any non-error status as success.
    return status in {"ok", "success", "applied", "completed", "queued", "true"}


def wait_for(client: httpx.Client, pred: Callable[[dict], bool], *, timeout: float, label: str) -> dict:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = get_state(client)
        if pred(last):
            return last
        time.sleep(0.4)
    print(f"  !! timed out waiting for {label} ({timeout:.0f}s); last screen={last.get('screen')}")
    return last


def wait_settled(client: httpx.Client, *, timeout: float, quiet: float = 1.2, label: str = "") -> dict:
    """Poll until two consecutive canonical snapshots agree — screens animate.

    Reading /state the instant a screen appears returns a half-built payload
    (empty hand, energy 0), which would produce a bogus 'fields differ' verdict.
    """
    deadline = time.time() + timeout
    previous: str | None = None
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = get_state(client)
        fingerprint = json.dumps(canonical(last), sort_keys=True, ensure_ascii=False)
        if fingerprint == previous:
            return last
        previous = fingerprint
        time.sleep(quiet)
    print(f"  !! state never settled ({label}); using last read")
    return last


def in_run(state: dict[str, Any]) -> bool:
    return bool((state.get("run") or {}).get("floor")) and state.get("screen") not in ACTIVE_SCREENS


# ── mechanical driver (no LLM) ─────────────────────────────────────────


NON_COMBAT_POLICY: list[tuple[str, Callable[[], dict]]] = [
    ("choose_map_node", lambda: act.choose_map_node(0)),
    ("claim_reward", lambda: act.claim_reward(0)),
    ("choose_reward_card", lambda: act.choose_reward_card(0)),
    ("resolve_rewards", lambda: act.resolve_rewards()),
    ("collect_rewards_and_proceed", lambda: act.collect_rewards_and_proceed()),
    ("proceed", lambda: act.proceed()),
    ("choose_event_option", lambda: act.choose_event_option(0)),
    ("choose_rest_option", lambda: act.choose_rest_option(0)),
    ("close_shop_inventory", lambda: act.close_shop_inventory()),
    ("open_chest", lambda: act.open_chest()),
    ("choose_treasure_relic", lambda: act.choose_treasure_relic(0)),
    ("select_deck_card", lambda: act.select_deck_card(0)),
    ("confirm_selection", lambda: act.confirm_selection()),
    ("confirm_modal", lambda: act.confirm_modal()),
    ("dismiss_modal", lambda: act.dismiss_modal()),
    ("confirm_timeline_overlay", lambda: act.confirm_timeline_overlay()),
]


def play_one(client: httpx.Client, state: dict[str, Any]) -> bool:
    """Take one mechanical action from the given state. Returns False if stuck."""
    acts = set(state.get("available_actions") or [])
    if state.get("in_combat"):
        hand = ((state.get("combat") or {}).get("hand")) or []
        for card in hand:
            if not card.get("playable"):
                continue
            if post_action(client, act.play_card(card["index"])):
                return True
            if post_action(client, act.play_card(card["index"], 0)):
                return True
        if "end_turn" in acts:
            return post_action(client, act.end_turn())
        return False
    for name, build in NON_COMBAT_POLICY:
        if name in acts and post_action(client, build()):
            return True
    return False


def drive_new_run(client: httpx.Client, target_floor: int, max_actions: int, timeout: float) -> dict:
    """Start a run without an LLM and play until we are inside a fight."""
    state = get_state(client)
    acts = set(state.get("available_actions") or [])
    if "open_character_select" in acts:
        post_action(client, act.open_character_select())
        state = wait_for(client, lambda s: "select_character" in (s.get("available_actions") or []),
                         timeout=timeout, label="character select")
    if "select_character" in (state.get("available_actions") or []):
        post_action(client, act.select_character(0))
        time.sleep(0.5)
    state = get_state(client)
    if "embark" in (state.get("available_actions") or []):
        post_action(client, act.embark())
        state = wait_for(client, in_run, timeout=max(timeout, 60.0), label="run started")
    print(f"  run started: {summarise_state(state)}")

    for step in range(max_actions):
        state = get_state(client)
        if not in_run(state):
            print(f"  driver stopped: not in a run (screen={state.get('screen')})")
            return state
        floor = (state.get("run") or {}).get("floor") or 0
        if state.get("in_combat") and floor >= target_floor:
            print(f"  driver reached mid-combat at F{floor} after {step} actions")
            return state
        if not play_one(client, state):
            print(f"  driver stuck: screen={state.get('screen')} "
                  f"actions={sorted(set(state.get('available_actions') or []))[:8]}")
            return state
        time.sleep(0.35)
    print(f"  driver hit the action cap ({max_actions})")
    return get_state(client)


# ── canonicalisation + diff ────────────────────────────────────────────


def canonical(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: canonical(v) for k, v in sorted(node.items()) if k not in VOLATILE_KEYS}
    if isinstance(node, list):
        return [canonical(v) for v in node]
    return node


def diff(a: Any, b: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    out: list[tuple[str, Any, Any]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key in VOLATILE_KEYS:
                continue
            if key not in a or key not in b:
                out.append((f"{path}/{key}", a.get(key, "<missing>"), b.get(key, "<missing>")))
                continue
            out.extend(diff(a[key], b[key], f"{path}/{key}"))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((f"{path}/len", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff(x, y, f"{path}[{i}]"))
    elif a != b:
        out.append((path, a, b))
    return out


def brief(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ── save files ─────────────────────────────────────────────────────────


def hash_saves() -> list[dict[str, Any]]:
    if not SAVE_ROOT.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(SAVE_ROOT.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            continue
        rows.append(
            {
                "path": str(path.relative_to(SAVE_ROOT)),
                "size": stat.st_size,
                "sha256_16": digest,
            }
        )
    return rows


def save_changes(before: list[dict], after: list[dict]) -> list[str]:
    old = {r["path"]: r for r in before}
    new = {r["path"]: r for r in after}
    changes: list[str] = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            changes.append(f"+ {path} ({new[path]['size']} B)")
        elif path not in new:
            changes.append(f"- {path}")
        elif old[path]["sha256_16"] != new[path]["sha256_16"]:
            changes.append(
                f"~ {path}  {old[path]['size']}B/{old[path]['sha256_16']}"
                f" -> {new[path]['size']}B/{new[path]['sha256_16']}"
            )
    return changes


def summarise_state(state: dict[str, Any]) -> str:
    run = state.get("run") or {}
    combat = state.get("combat") or {}
    enemies = combat.get("enemies") or []
    foes = ", ".join(f"{e.get('name')}({e.get('current_hp')})" for e in enemies[:3])
    hand = combat.get("hand") or []
    return (
        f"screen={state.get('screen')} seed={state.get('run_id')} "
        f"{run.get('character_id')} A{run.get('ascension')} F{run.get('floor')} "
        f"HP={run.get('current_hp')}/{run.get('max_hp')} gold={run.get('gold')} "
        f"combat={state.get('in_combat')} turn={state.get('turn')} "
        f"hand={[c.get('name') for c in hand]} foes=[{foes}] "
        f"state_version={state.get('state_version')}"
    )


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--drive-floor", type=int, default=2)
    ap.add_argument("--max-drive-actions", type=int, default=80)
    ap.add_argument("--poke", type=int, default=3, help="mechanical actions between A and the save")
    ap.add_argument("--no-drive", action="store_true")
    args = ap.parse_args()

    verdicts: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        state = get_state(client)
        print(f"start: {summarise_state(state)}")

        if not in_run(state):
            acts = state.get("available_actions") or []
            if "continue_run" in acts:
                post_action(client, act.continue_run())
                state = wait_for(client, in_run, timeout=args.timeout, label="run resumed")
            elif not args.no_drive:
                state = drive_new_run(client, args.drive_floor, args.max_drive_actions, args.timeout)
            if not in_run(state):
                print("!! could not get into a run; aborting")
                return 2

        for round_no in range(1, args.rounds + 1):
            state = get_state(client)
            print(f"\n=== round {round_no} ===")
            saves_at_start = hash_saves()

            # Change the state first: a save that has nothing new to record
            # proves nothing, and a mid-combat restore is the case that matters.
            for _ in range(args.poke):
                if not in_run(state):
                    break
                play_one(client, state)
                state = get_state(client)
                time.sleep(0.3)
            saves_after_poke = hash_saves()
            poke_changes = save_changes(saves_at_start, saves_after_poke)
            state = wait_settled(client, timeout=args.timeout, label="state after poke")
            print(f"A: {summarise_state(state)}")
            print(f"files written while playing: {len(poke_changes)}")
            for line in poke_changes[:4]:
                print(f"   {line}")

            snapshot_a = canonical(state)

            post_action(client, act.save_and_quit())
            menu = wait_for(
                client,
                lambda s: "continue_run" in (s.get("available_actions") or []),
                timeout=max(args.timeout, 60.0),
                label="main menu with continue_run",
            )
            saves_after_quit = hash_saves()
            quit_changes = save_changes(saves_after_poke, saves_after_quit)
            print(f"files written by save_and_quit: {len(quit_changes)}")
            for line in quit_changes[:4]:
                print(f"   {line}")
            print(f"continue_run offered at menu: {'continue_run' in (menu.get('available_actions') or [])}")

            post_action(client, act.continue_run())
            wait_for(client, in_run, timeout=max(args.timeout, 60.0), label="run resumed")
            state = wait_settled(
                client, timeout=max(args.timeout, 60.0), label="restored state"
            )
            snapshot_b = canonical(state)
            print(f"B: {summarise_state(state)}")

            differences = diff(snapshot_a, snapshot_b)
            runtime_only = [d for d in differences if d[0].endswith("state_version")]
            substantive = [d for d in differences if d not in runtime_only]
            print(f"seed preserved: {snapshot_a.get('run_id') == snapshot_b.get('run_id')}")
            print(f"differing fields: {len(differences)}  (state_version-only: {len(runtime_only)})")
            for path, va, vb in substantive[:15]:
                print(f"   {path}: {brief(va)}  !=  {brief(vb)}")
            verdict = "IDENTICAL" if not substantive else f"{len(substantive)} FIELD(S) DIFFER"
            print(f"verdict: {verdict}")
            verdicts.append(verdict)

    print(f"\n=== summary: {verdicts} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
