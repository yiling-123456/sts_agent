"""CLI: capture, list and verify run-state snapshots.

    python -m scripts.eval_snapshot save --label f12-rest
    python -m scripts.eval_snapshot list
    python -m scripts.eval_snapshot verify --label f12-rest
    python -m scripts.eval_snapshot restore --label f12-rest

``save`` verifies itself by restoring once and recording the state it actually
re-entered — see ``src/eval/snapshot.py`` for why the live state is not a
sufficient record.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.eval.game_client import EvalGameClient, run_summary
from src.eval.snapshot import RestoreMismatch, SnapshotStore


def _store(args: argparse.Namespace) -> SnapshotStore:
    from pathlib import Path

    return SnapshotStore(Path(args.root) if args.root else None)


def cmd_save(args: argparse.Namespace) -> int:
    store = _store(args)
    with EvalGameClient(timeout=args.timeout) as client:
        meta = store.save(client, args.label, verify=not args.no_verify, timeout=args.timeout)
    print(json.dumps(meta.__dict__, indent=2, ensure_ascii=False))
    print("\n" + meta.summary())
    if not meta.live_matches_entry:
        print(
            "\nNOTE: the autosave predates the captured screen, so this snapshot "
            "re-enters at an earlier state. That is expected mid-room/fight; it "
            "does not make the snapshot invalid, but branches must be described "
            "by the entry state, not by what was on screen."
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    print(store.describe())
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    store = _store(args)
    with EvalGameClient(timeout=args.timeout) as client:
        try:
            meta, state = store.restore(client, args.label, verify=True, timeout=args.timeout)
        except RestoreMismatch as exc:
            print(f"FAIL: {exc}")
            return 1
    print(f"OK: {meta.label} restored faithfully ({meta.restore_count} restores)")
    print(f"    {run_summary(state)}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    store = _store(args)
    with EvalGameClient(timeout=args.timeout) as client:
        meta, state = store.restore(client, args.label, verify=not args.no_verify, timeout=args.timeout)
    print(f"restored {meta.label}")
    print(f"    {run_summary(state)}")
    if args.handoff:
        from src.eval.segment import describe_run, launch

        run, process = launch(
            meta,
            tag=args.tag,
            steps=args.steps,
            extra_args=args.extra_arg,
            wait=args.wait,
        )
        print("\n" + describe_run(run))
        print(f"    agent process: {process.pid if process else 'waited to completion'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", help="snapshot root (default: <data repo>/eval/snapshots)")
    parser.add_argument("--timeout", type=float, default=60.0)
    sub = parser.add_subparsers(dest="command", required=True)

    p_save = sub.add_parser("save", help="capture and verify a snapshot")
    p_save.add_argument("--label", required=True)
    p_save.add_argument("--no-verify", action="store_true", help="skip the restore self-check")
    p_save.set_defaults(func=cmd_save)

    p_list = sub.add_parser("list", help="list snapshots")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="restore and check the entry digest")
    p_verify.add_argument("--label", required=True)
    p_verify.set_defaults(func=cmd_verify)

    p_restore = sub.add_parser("restore", help="restore, optionally handing off to the agent")
    p_restore.add_argument("--label", required=True)
    p_restore.add_argument("--no-verify", action="store_true")
    p_restore.add_argument("--handoff", action="store_true", help="launch the agent on the restored run")
    p_restore.add_argument("--tag", default="segment")
    p_restore.add_argument("--steps", type=int, default=500)
    p_restore.add_argument("--wait", action="store_true", help="block until the agent finishes")
    p_restore.add_argument(
        "--extra-arg", action="append", default=[],
        help="extra argument forwarded to run_agent, e.g. --extra-arg=--no-skills "
             "(policy substitution) or --extra-arg=--no-postrun",
    )
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
