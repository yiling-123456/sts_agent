"""CLI: local rewind panel for the live game (watcher + clickable room history).

    python -m scripts.eval_rewind                 # serve http://127.0.0.1:8760/
    python -m scripts.eval_rewind --open          # ... and open a browser
    python -m scripts.eval_rewind --list          # print the ring and exit
    python -m scripts.eval_rewind --no-watch      # panel only (rewind from existing ring)

While this runs, every room the player enters is snapshotted, and clicking a row
in the page rewinds to that room's entry state. See src/eval/README.md for what
rewinding can and cannot do (the game only saves at room entry).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.eval.game_client import EvalGameClient, run_summary
from src.eval.panel import Panel, serve
from src.eval.ring import RoomRing
from src.eval.snapshot import SnapshotStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--interval", type=float, default=1.0, help="watcher poll interval (seconds)")
    parser.add_argument("--no-watch", action="store_true", help="do not auto-capture rooms")
    parser.add_argument("--open", action="store_true", help="open the panel in a browser")
    parser.add_argument("--list", action="store_true", help="print the ring and exit")
    parser.add_argument("--root", help="snapshot root override")
    args = parser.parse_args(argv)

    store = SnapshotStore(Path(args.root) if args.root else None)
    ring = RoomRing(store)

    if args.list:
        if not ring.entries:
            print("(ring is empty)")
            return 0
        for entry in reversed(ring.entries):
            print(
                f"{entry.key:<10} {entry.screen:<12} HP {entry.hp}/{entry.max_hp} "
                f"gold {entry.gold:<4} deck {entry.deck_size:<3} {entry.captured_at}"
            )
        return 0

    with EvalGameClient() as client:
        state = client.state()
        print(f"live: {run_summary(state)}")
    panel = Panel(ring, port=args.port)
    panel.live = state
    serve(panel, watch=not args.no_watch, interval=args.interval, open_browser=args.open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
