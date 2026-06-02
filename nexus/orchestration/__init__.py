"""Durable multi-agent orchestration: planner, tool registry, run engine, approval gate.

This package turns the single-shot agent runtime into a coordinated, resumable workflow:
a planner authors a DAG of tool calls, the engine drives it to completion over a shared
run blackboard, and outbound actions park at a human approval gate. Progress is streamed
as an append-only event log.
"""
from nexus.orchestration.engine import OrchestrationEngine, get_orchestration_engine

__all__ = ["OrchestrationEngine", "get_orchestration_engine"]
