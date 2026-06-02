"""Planner: turn a high-level goal into a validated DAG of tool calls.

The planner is the "author" half of the orchestrator. It is intentionally *deterministic*
for the goals we ship today — given the same goal + input it emits the same plan — so runs
are reproducible, reviewable, and offline-testable. The shape it returns (a list of step
descriptors with explicit ``depends_on`` edges) is exactly what an LLM-backed planner would
emit later, so swapping in a model-authored plan is a drop-in change behind this seam.

A plan is a list of step dicts::

    {"idx": 0, "tool": "research", "inputs": {}, "depends_on": [], "requires_approval": False}

Invariants enforced here (so the engine can trust its input):
- every ``tool`` is registered,
- ``idx`` values are unique and contiguous from 0,
- every ``depends_on`` edge points at an earlier, in-plan step (no forward refs, no cycles),
- a step inherits ``requires_approval`` from its tool unless the plan overrides it upward,
- the plan never exceeds ``orch_max_steps`` (runaway / cost guard).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nexus.core.config import get_settings
from nexus.orchestration.tools import get_tool


class PlanError(Exception):
    """Raised when a goal is unknown or would produce an invalid plan."""


@dataclass(slots=True)
class PlanStep:
    idx: int
    tool: str
    inputs: dict = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)
    requires_approval: bool = False

    def as_dict(self) -> dict:
        return {
            "idx": self.idx,
            "tool": self.tool,
            "inputs": dict(self.inputs),
            "depends_on": list(self.depends_on),
            "requires_approval": self.requires_approval,
        }


# Goal recipes -----------------------------------------------------------------------
#
# Each recipe is a linear-to-DAG sequence of (tool, inputs) the goal decomposes into.
# Kept declarative so the set of goals reads at a glance and a model-authored planner can
# later replace the body without changing the engine contract.


def _research_account_plan(goal_input: dict) -> list[PlanStep]:
    """The flagship goal: research an account, score it, draft warm outreach, and park
    the send at the approval gate. Each step depends on the previous one because each
    consumes what the prior wrote to the blackboard (facts -> draft -> send)."""
    sequence_inputs = {}
    if goal_input.get("sequence"):
        sequence_inputs["sequence"] = goal_input["sequence"]
    return [
        PlanStep(idx=0, tool="research", depends_on=[]),
        PlanStep(idx=1, tool="scoring", depends_on=[0]),
        PlanStep(idx=2, tool="compose_message", depends_on=[1]),
        PlanStep(
            idx=3,
            tool="send_message",
            inputs=sequence_inputs,
            depends_on=[2],
            requires_approval=True,
        ),
    ]


def _research_only_plan(goal_input: dict) -> list[PlanStep]:
    """A read-only goal: gather facts and score, no outbound action, no approval gate."""
    return [
        PlanStep(idx=0, tool="research", depends_on=[]),
        PlanStep(idx=1, tool="scoring", depends_on=[0]),
    ]


def _discover_plan(goal_input: dict) -> list[PlanStep]:
    """A single read-only discovery step. ICP/target/max_candidates ride on run.goal_input,
    which the DiscoveryTool reads directly — so the step itself needs no inputs."""
    return [PlanStep(idx=0, tool="discovery", depends_on=[], requires_approval=False)]


_RECIPES = {
    "research_account": _research_account_plan,
    "research_only": _research_only_plan,
    "discover": _discover_plan,
}


def available_goals() -> list[str]:
    return sorted(_RECIPES)


class Planner:
    """Authors and validates plans. Stateless; safe to share across requests."""

    def plan(self, goal: str, goal_input: dict | None = None) -> list[dict]:
        recipe = _RECIPES.get(goal)
        if recipe is None:
            raise PlanError(
                f"Unknown goal '{goal}'. Available: {available_goals()}"
            )
        steps = recipe(goal_input or {})
        self._validate(goal, steps)
        return [s.as_dict() for s in steps]

    def _validate(self, goal: str, steps: list[PlanStep]) -> None:
        settings = get_settings()
        if not steps:
            raise PlanError(f"Goal '{goal}' produced an empty plan")
        if len(steps) > settings.orch_max_steps:
            raise PlanError(
                f"Plan for '{goal}' has {len(steps)} steps, exceeds cap "
                f"{settings.orch_max_steps}"
            )

        seen: set[int] = set()
        for expected, step in enumerate(steps):
            if step.idx != expected:
                raise PlanError(
                    f"Plan step idx must be contiguous from 0: got {step.idx} at "
                    f"position {expected}"
                )
            seen.add(step.idx)

            tool = get_tool(step.tool)
            if tool is None:
                raise PlanError(f"Plan references unknown tool '{step.tool}'")

            for dep in step.depends_on:
                if dep not in seen or dep == step.idx:
                    raise PlanError(
                        f"Step {step.idx} depends on {dep}, which is not an earlier "
                        f"in-plan step (forward reference or cycle)"
                    )

            # A tool that is inherently side-effecting must keep its approval gate; the
            # plan may only escalate, never silently drop it.
            if tool.requires_approval and not step.requires_approval:
                step.requires_approval = True


_planner = Planner()


def get_planner() -> Planner:
    return _planner
