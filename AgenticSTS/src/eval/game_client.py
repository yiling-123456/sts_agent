"""Synchronous client for the STS2 mod API, used by the evaluation harness.

The agent runtime drives the mod through an async client
(:mod:`src.mcp_client.client`); the harness is a batch driver, so a small
blocking client keeps the orchestration readable and lets a CLI run without an
event loop.

Screen animation matters: reading ``/state`` the instant a screen appears
returns a half-built payload (empty hand, 0 energy), which silently produces a
false "state differs" verdict. Every capture therefore goes through
:meth:`EvalGameClient.settled`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable

import httpx

from src.mcp_client import actions as act

# ``request_id`` is per-request plumbing; everything else in /state describes the
# game. Drop it before hashing or diffing.
VOLATILE_STATE_KEYS = {"request_id"}

# Screens that are not a live run.
INACTIVE_SCREENS = {
    "MAIN_MENU",
    "GAME_OVER",
    "CHARACTER_SELECT",
    "TIMELINE",
    "OVERLAY",
    "UNKNOWN",
}

# The mod reports "completed" when an action changed the screen, "ok"/"applied"
# for in-place actions. Anything else is treated as a failure.
OK_STATUSES = {"ok", "success", "applied", "completed", "queued", "true"}


def canonical_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return the state with volatile keys dropped and dict keys sorted."""
    if isinstance(state, dict):
        return {
            k: canonical_state(v)
            for k, v in sorted(state.items())
            if k not in VOLATILE_STATE_KEYS
        }
    if isinstance(state, list):
        return [canonical_state(v) for v in state]
    return state


def state_digest(state: dict[str, Any]) -> str:
    payload = json.dumps(canonical_state(state), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def in_run(state: dict[str, Any]) -> bool:
    return bool((state.get("run") or {}).get("floor")) and state.get("screen") not in INACTIVE_SCREENS


def run_summary(state: dict[str, Any]) -> str:
    run = state.get("run") or {}
    combat = state.get("combat") or {}
    enemies = combat.get("enemies") or []
    foes = ", ".join(f"{e.get('name')}({e.get('current_hp')})" for e in enemies[:3])
    hand = [c.get("name") for c in (combat.get("hand") or [])]
    return (
        f"seed={state.get('run_id')} {run.get('character_id')} A{run.get('ascension')} "
        f"F{run.get('floor')} HP={run.get('current_hp')}/{run.get('max_hp')} "
        f"gold={run.get('gold')} combat={state.get('in_combat')} turn={state.get('turn')} "
        f"hand={hand} foes=[{foes}]"
    )


class EvalGameClient:
    """Blocking wrapper over ``GET /state`` and ``POST /action``."""

    def __init__(self, base_url: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or os.getenv("STS2_MCP_URL", "http://127.0.0.1:8128")).rstrip("/")
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EvalGameClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── observation ────────────────────────────────────────────────────

    def state(self) -> dict[str, Any]:
        resp = self._client.get(f"{self.base_url}/state")
        resp.raise_for_status()
        return resp.json().get("data") or {}

    def settled(
        self,
        *,
        timeout: float = 60.0,
        quiet: float = 1.2,
        label: str = "",
    ) -> dict[str, Any]:
        """Poll until two consecutive canonical snapshots agree.

        Screens animate: a read taken as a screen appears carries an empty hand
        and zero energy, so a comparison against it is meaningless.
        """
        deadline = time.time() + timeout
        previous: str | None = None
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.state()
            fingerprint = json.dumps(canonical_state(last), sort_keys=True, ensure_ascii=False)
            if fingerprint == previous:
                return last
            previous = fingerprint
            time.sleep(quiet)
        raise TimeoutError(f"state never settled{f' ({label})' if label else ''}")

    def wait_for(
        self,
        pred: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 60.0,
        label: str = "",
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.state()
            if pred(last):
                return last
            time.sleep(0.4)
        raise TimeoutError(f"timed out waiting for {label or 'condition'}; screen={last.get('screen')}")

    # ── actions ────────────────────────────────────────────────────────

    def send(self, body: dict[str, Any]) -> bool:
        """POST one action; True when the mod reports a non-error status."""
        try:
            payload = self._client.post(f"{self.base_url}/action", json=body).json()
        except Exception:  # noqa: BLE001 - transport hiccups are retried by callers
            return False
        if not payload.get("ok", True):
            return False
        data = payload.get("data") or {}
        return str(data.get("status", "ok")).lower() in OK_STATUSES

    def save_and_quit(self, *, timeout: float = 60.0) -> dict[str, Any]:
        """Quit to the menu; the game writes the autosave and a replay file."""
        self.send(act.save_and_quit())
        return self.wait_for(
            lambda s: "continue_run" in (s.get("available_actions") or []),
            timeout=timeout,
            label="main menu with continue_run",
        )

    def continue_run(self, *, timeout: float = 60.0) -> dict[str, Any]:
        """Re-enter the saved run and return its settled state."""
        self.send(act.continue_run())
        self.wait_for(in_run, timeout=timeout, label="run resumed")
        return self.settled(timeout=timeout, label="restored state")
