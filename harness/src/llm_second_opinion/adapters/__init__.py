"""The AgentAdapter protocol and agent adapters."""

from collections.abc import Callable

from llm_second_opinion.adapters.base import AgentAdapter
from llm_second_opinion.adapters.gold import GoldAdapter
from llm_second_opinion.adapters.pi import PiAdapter
from llm_second_opinion.config import AgentSpec

# Adapter name -> constructor taking the experiment's `agents` entry.
ADAPTERS: dict[str, Callable[[AgentSpec], AgentAdapter]] = {
    "gold": GoldAdapter,
    "pi": PiAdapter,
}

__all__ = ["ADAPTERS", "AgentAdapter", "GoldAdapter", "PiAdapter"]
