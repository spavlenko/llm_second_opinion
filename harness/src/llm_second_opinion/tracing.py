"""Spans for a work item's trace, built after the fact from what the agent recorded.

Adapters turn their agent's logs into `Span` trees (`AgentAdapter.spans`); the runner adds
the item and grading spans, and `tracking.Tracker` writes the tree to MLflow as one trace.
Kept free of MLflow imports, so adapters do not depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# MLflow span types used here: https://mlflow.org/docs/latest/genai/tracing/
Kind = Literal["AGENT", "CHAIN", "CHAT_MODEL", "TOOL", "EVALUATOR", "UNKNOWN"]

MAX_TEXT = 20_000  # characters kept per input or output value


@dataclass
class Span:
    name: str
    kind: Kind
    start_ns: int
    end_ns: int
    inputs: Any = None
    outputs: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    children: list[Span] = field(default_factory=list)


def ms_to_ns(ms: float) -> int:
    return int(ms * 1_000_000)


def clip(value: Any) -> Any:
    """Long strings shortened, keeping both ends; nested values clipped recursively."""
    if isinstance(value, str) and len(value) > MAX_TEXT:
        half = MAX_TEXT // 2
        return (
            f"{value[:half]}\n[... {len(value) - MAX_TEXT} characters omitted ...]\n{value[-half:]}"
        )
    if isinstance(value, dict):
        return {k: clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clip(v) for v in value]
    return value
