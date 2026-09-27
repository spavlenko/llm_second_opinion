"""The AgentAdapter protocol and agent adapters."""

from llm_second_opinion.adapters.base import AgentAdapter
from llm_second_opinion.adapters.gold import GoldAdapter

ADAPTERS: dict[str, type[AgentAdapter]] = {"gold": GoldAdapter}

__all__ = ["ADAPTERS", "AgentAdapter", "GoldAdapter"]
