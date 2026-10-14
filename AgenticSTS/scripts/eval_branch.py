"""CLI: offline branch evaluation from a verified snapshot.

Example — the campfire question ("rest or upgrade?"), repeated three times, with
a fixed mechanical follow-up policy and segments capped at five floors:

    python -m scripts.eval_branch --snapshot f12-rest --repeats 3 --floors 5 \\
        --branch rest:choose_rest_option --branch smith:choose_rest_option

Each ``--branch`` is ``name:action`` where ``action`` is either a bare mod action
(``end_turn``, ``proceed``) or one of the injected presets understood here
(``choose_rest_option`` picks the rest option, ``smith`` the smith option, and so
on). Branches run interleaved so a mid-sweep environment change cannot favour one
of them, and the report states that observations are paired on a shared seed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.eval.branch import Branch, BranchEvaluator, BranchReport
from src.eval.game_client import EvalGameClient
from src.eval.policies import MechanicalPolicy
from src.eval.snapshot import SnapshotStore
from src.mcp_client import actions as act

# Presets that need more than a bare action body.
REST_PRESETS = {
    "rest": lambda state: _rest_option(state, "REST"),
    "smith": lambda state: _rest_option(state, "SMITH"),
    "choose_rest_option": lambda state: act.choose_rest_option(0),
}


def _rest_option(state: dict, wanted: str) -> dict:
    for option in ((state.get("rest") or {}).get("options")) or []:
        kind = str(option.get("option_type") or option.get("type") or "").upper()
        if kind == wanted:
            return act.choose_rest_option(option.get("index", 0))
    return act.choose_rest_option(0)


def _play_first(state: dict) -> dict:
    """Play the leftmost playable card — the combat counterpart of 'take option 0'."""
    for card in (state.get("combat") or {}).get("hand") or []:
        if card.get("playable"):
            targets = card.get("valid_target_indices") or card.get("targets") or []
            return act.play_card(card["index"], targets[0] if targets else None)
    return act.end_turn()


BARE_ACTIONS = {
    "end_turn": lambda state: act.end_turn(),
    "play_first": _play_first,
    "proceed": lambda state: act.proceed(),
    "resolve_rewards": lambda state: act.resolve_rewards(),
    "claim_reward": lambda state: act.claim_reward(0),
    "choose_map_node": lambda state: act.choose_map_node(0),
    "open_chest": lambda state: act.open_chest(),
    "choose_treasure_relic": lambda state: act.choose_treasure_relic(0),
    "close_shop_inventory": lambda state: act.close_shop_inventory(),
    "confirm_selection": lambda state: act.confirm_selection(),
}


def parse_branch(spec: str, *, prefix: list[dict] | None = None) -> Branch:
    name, _, action = spec.partition(":")
    if not action:
        raise SystemExit(f"branch spec must be name:action, got {spec!r}")
    builders = {**REST_PRESETS, **BARE_ACTIONS}
    builder = builders.get(action)
    if builder is None:
        raise SystemExit(f"unknown branch action {action!r}; known: {sorted(builders)}")
    return Branch(name=name, inject=None, prefix=tuple(prefix or ()), note=action), builder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--branch", action="append", default=[], help="name:action (repeatable)")
    parser.add_argument("--baseline", help="branch name used as the pairing baseline")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--steps", type=int, default=400, help="cap on follow-up actions per branch")
    parser.add_argument("--floors", type=int, default=None, help="stop a segment after N floors")
    parser.add_argument("--policy", default="mechanical-first", choices=["mechanical-first"])
    parser.add_argument("--rest-choice", default="auto", choices=["auto", "rest", "smith"],
                        help="how the follow-up policy handles later rest sites")
    parser.add_argument("--no-verify", action="store_true", help="skip restore digest checks (unsafe)")
    parser.add_argument("--root", help="snapshot root override")
    args = parser.parse_args(argv)

    if not args.branch:
        raise SystemExit("need at least one --branch name:action")
    store = SnapshotStore(Path(args.root) if args.root else None)
    if args.snapshot not in store.labels():
        raise SystemExit(f"unknown snapshot {args.snapshot!r}; have {store.labels()}")

    policy = MechanicalPolicy(name=args.policy, rest_choice=args.rest_choice)

    # Resolve presets against the snapshot's entry state, so "rest" vs "smith"
    # is decided by the options actually offered at that rest site.
    with EvalGameClient() as client:
        _, entry = store.restore(client, args.snapshot, verify=not args.no_verify)
        branches: list[Branch] = []
        resolvers = []
        for spec in args.branch:
            branch, builder = parse_branch(spec)
            branches.append(Branch(name=branch.name, inject=builder(entry), note=branch.note))
            resolvers.append(builder)

        evaluator = BranchEvaluator(
            client, store, policy, max_steps=args.steps, verify_restore=not args.no_verify
        )
        print(f"entry state: {entry.get('screen')} F{(entry.get('run') or {}).get('floor')} "
              f"actions={sorted(set(entry.get('available_actions') or []))}")
        report: BranchReport = evaluator.evaluate(
            args.snapshot,
            branches,
            repeats=args.repeats,
            stop_after_floors=args.floors,
            on_outcome=lambda outcome: print("  " + outcome.row(), flush=True),
        )

    print("\n" + report.table())
    baseline = args.baseline or branches[0].name
    try:
        deltas = report.paired_deltas(baseline)
    except KeyError:
        deltas = []
    if deltas:
        print(f"\npaired deltas vs {baseline!r} (same seed, same RNG stream):")
        for row in deltas:
            print(
                f"  {row['branch']:<18} r{row['repeat']} "
                f"d_floors={row['delta_floors_gained']:+d} "
                f"d_hp={row['delta_end_hp']:+d} "
                f"d_fit={row['delta_segment_fitness']:+.2f} "
                f"d_win={row['delta_win']:+d}"
            )
        print(
            "\nThese are paired observations. Report them as paired differences "
            "(or a paired test); independent-sample intervals (Wilson, bootstrap "
            "over runs) do not apply."
        )

    out_dir = store.root / args.snapshot
    path = report.write(out_dir)
    print(f"\nreport written: {path}")
    print(json.dumps(report.means(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
