"""Follow-up policies for controlled policy substitution and branch evaluation.

A branch experiment has two halves:

1. **the injected choice** — one action (or a short prefix) that differs between
   branches;
2. **the follow-up policy** — what plays out the rest of the segment, held
   *fixed* across branches so the comparison isolates the injected choice.

This module provides the second half. Policies are deliberately dumb and
deterministic: a mechanical policy keeps a branch sweep cheap (seconds per
segment, no LLM) and makes the injected choice the only source of variation.
The LLM agent is available as a policy too, at ~50 minutes per branch, by
restoring the snapshot and handing off to ``scripts.run_agent --resume-saved``
(see :mod:`src.eval.segment`).

Screen animation is handled by the client: ``decide`` is only ever called on a
settled state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.eval.game_client import EvalGameClient, in_run
from src.mcp_client import actions as act


class Policy(Protocol):
    """Maps a settled game state to one action, or None to stop."""

    name: str

    def decide(self, state: dict[str, Any]) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class MechanicalPolicy:
    """Deterministic first-option policy — the default fixed follow-up.

    Combat: play the leftmost playable card, else end the turn.
    Everything else: take the first offered option, in a fixed order. No
    strategic reasoning on purpose — its job is to be *constant*.
    """

    name: str = "mechanical-first"
    rest_choice: str = "auto"  # "auto" | "rest" | "smith"

    NON_COMBAT_ORDER: tuple[tuple[str, str], ...] = (
        ("choose_map_node", "index"),
        ("claim_reward", "index"),
        ("choose_reward_card", "index"),
        ("choose_reward_alternative", "index"),
        ("resolve_rewards", "none"),
        ("collect_rewards_and_proceed", "none"),
        ("proceed", "none"),
        ("choose_event_option", "index"),
        ("choose_rest_option", "rest"),
        ("open_chest", "none"),
        ("choose_treasure_relic", "index"),
        ("buy_card", "index"),
        ("close_shop_inventory", "none"),
        ("select_deck_card", "index"),
        ("confirm_selection", "none"),
        ("confirm_modal", "none"),
        ("dismiss_modal", "none"),
        ("confirm_timeline_overlay", "none"),
    )

    def decide(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if not in_run(state):
            return None
        available = set(state.get("available_actions") or [])
        available.discard("save_and_quit")

        if state.get("in_combat"):
            for card in (state.get("combat") or {}).get("hand") or []:
                if card.get("playable"):
                    targets = card.get("valid_target_indices") or card.get("targets") or []
                    return act.play_card(card["index"], targets[0] if targets else None)
            return act.end_turn() if "end_turn" in available else None

        for action_name, kind in self.NON_COMBAT_ORDER:
            if action_name not in available:
                continue
            if kind == "none":
                return {"action": action_name}
            if kind == "index":
                return {"action": action_name, "option_index": 0}
            if kind == "rest":
                return self._rest_action(state)
        return None

    def _rest_action(self, state: dict[str, Any]) -> dict[str, Any]:
        """Rest sites are the classic branch point — honour the configured choice."""
        options = ((state.get("rest") or {}).get("options")) or []
        if self.rest_choice == "auto" or not options:
            return act.choose_rest_option(0)
        wanted = "REST" if self.rest_choice == "rest" else "SMITH"
        for option in options:
            if str(option.get("option_type") or option.get("type") or "").upper() == wanted:
                return act.choose_rest_option(option.get("index", 0))
        return act.choose_rest_option(0)


@dataclass(frozen=True)
class ScriptedPrefix:
    """Replay a recorded action prefix, then fall through to another policy.

    Required for in-combat branch points: the autosave only exists at fight
    entry, so a mid-fight decision is reached by replaying the recorded prefix
    (proven deterministic in the restore spike) and then diverging.
    """

    prefix: tuple[dict[str, Any], ...]
    then: Policy
    name: str = "scripted-prefix"

    def decide(self, state: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError("ScriptedPrefix is consumed by BranchEvaluator, not by decide()")


def play_segment(
    client: EvalGameClient,
    policy: Policy,
    *,
    max_steps: int,
    start_floor: int,
    stop_when: Any | None = None,
    on_step: Any | None = None,
) -> dict[str, Any]:
    """Run ``policy`` until the run ends, the step cap hits, or ``stop_when`` fires.

    Returns the segment outcome in the same currency the agent's own runs report
    (floor, HP, gold, victory, and the repo's fitness shape), so branch results
    line up with ``runs/history.jsonl``.
    """
    steps = 0
    hp_samples: list[float] = []
    last: dict[str, Any] = client.state()
    while steps < max_steps:
        state = client.settled(timeout=45.0, label="segment step")
        last = state
        if not in_run(state):
            break
        if stop_when is not None and stop_when(state):
            break
        run = state.get("run") or {}
        if (run.get("max_hp") or 0) > 0:
            hp_samples.append((run.get("current_hp") or 0) / run["max_hp"])
        body = policy.decide(state)
        if body is None:
            break
        if not client.send(body):
            # One retry with a fallback: mechanical policies can pick an action
            # the mod rejects after a screen change; end_turn is always safe in
            # combat and there is nothing safe to guess outside it.
            if state.get("in_combat") and client.send(act.end_turn()):
                steps += 1
                continue
            break
        steps += 1
        if on_step is not None:
            on_step(steps, body)

    run = last.get("run") or {}
    floor = run.get("floor") or start_floor
    hp_ratio = sum(hp_samples) / len(hp_samples) if hp_samples else 0.0
    gold = run.get("gold") or 0
    victory = last.get("screen") == "GAME_OVER" and bool((last.get("game_over") or {}).get("victory"))
    return {
        "steps": steps,
        "start_floor": start_floor,
        "end_floor": floor,
        "floors_gained": floor - start_floor,
        "end_hp": run.get("current_hp"),
        "end_max_hp": run.get("max_hp"),
        "end_gold": gold,
        "avg_hp_ratio": round(hp_ratio, 4),
        "victory": victory,
        "end_screen": last.get("screen"),
        # Same weights as src/state/run_state.py::fitness, applied to the segment.
        "segment_fitness": round(
            100.0 * int(victory)
            + 2.0 * (floor - start_floor)
            + 50.0 * hp_ratio
            + 20.0 * min(gold / 500.0, 1.0),
            2,
        ),
    }
