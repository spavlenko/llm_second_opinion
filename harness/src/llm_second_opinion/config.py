"""Experiment configuration: YAML loading, sweeps, validation, and a stable hash per arm."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from llm_second_opinion.contracts import (
    AdvisorSettings,
    ModelEndpoint,
    RunConfig,
    RunIdentity,
    Strict,
)

ADVISOR_MODEL = "advisor"  # key in `models` that advisor arms consult
_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


class Limits(Strict):
    wall_minutes: float = Field(gt=0)
    max_turns: int = Field(gt=0)


class Execution(Strict):
    """How work items are scheduled. Not part of the config hash: it must not change results."""

    parallel: int = Field(
        1,
        ge=1,
        description="Work items at once. Above 1 only if the model servers take concurrent requests.",
    )
    cpus: float = Field(4, gt=0, description="CPU cap per container.")
    memory_gb: float = Field(8, gt=0, description="Memory cap per container.")
    retries: int = Field(1, ge=0, description="Extra attempts after an infrastructure error.")
    grade_minutes: float = Field(30, gt=0, description="Time limit for the grading tests.")


class AgentSpec(Strict):
    """An entry in the experiment's `agents`: which adapter, which version, and its options."""

    adapter: str = Field(description="A name in `adapters.ADAPTERS`.")
    version: str | None = Field(None, description="Agent version; None means the default.")
    options: dict[str, Any] = Field(
        default_factory=dict, description="Validated by the adapter's own options model."
    )


class Arm(Strict):
    name: str = Field(min_length=1)
    agent: str = Field("pi", description="A key in `agents`, or an adapter name with defaults.")
    executor: str = Field(description="Key in the experiment's models that the agent runs on.")
    advisor: AdvisorSettings | None = None

    def with_advisor(self, name: str | None = None, **changes: Any) -> Arm:
        """Copy this arm with advisor settings changed, e.g. with_advisor(level="L3", name="A3")."""
        base = self.advisor.model_dump() if self.advisor else {}
        advisor = AdvisorSettings.model_validate({**base, **changes})
        return self.model_copy(update={"name": name or self.name, "advisor": advisor})


class Experiment(Strict):
    name: str = Field(min_length=1)
    tasks: Path = Field(description="Frozen task manifest.")
    seeds: int = Field(ge=1, description="Number of seeds; runs use seeds 0..n-1.")
    limits: Limits
    execution: Execution = Field(default_factory=Execution)
    models: dict[str, ModelEndpoint]
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    arms: list[Arm] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_arms(self) -> Experiment:
        names = [arm.name for arm in self.arms]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate arm names: {', '.join(duplicates)}")
        for arm in self.arms:
            if arm.executor not in self.models:
                raise ValueError(f"arm {arm.name}: executor {arm.executor!r} is not in models")
            if arm.advisor and ADVISOR_MODEL not in self.models:
                raise ValueError(
                    f"arm {arm.name} has an advisor but models has no {ADVISOR_MODEL!r}"
                )
        return self

    @classmethod
    def from_yaml(cls, path: str | Path, env: Mapping[str, str] = os.environ) -> Experiment:
        path = Path(path)
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: expected a mapping at the top level")
        raw = interpolate_env(raw, env)
        raw["arms"] = expand_sweeps(raw.get("arms") or [])
        if "tasks" in raw:
            raw["tasks"] = (path.parent / raw["tasks"]).resolve()
        return cls.model_validate(raw)

    def arm(self, name: str) -> Arm:
        for arm in self.arms:
            if arm.name == name:
                return arm
        raise KeyError(f"no arm named {name!r}; arms: {', '.join(a.name for a in self.arms)}")

    def agent_spec(self, arm: Arm) -> AgentSpec:
        """The arm's `agents` entry; a bare adapter name means that adapter's defaults."""
        return self.agents.get(arm.agent) or AgentSpec(adapter=arm.agent)

    def config_hash(self, arm: Arm) -> str:
        """Hash of everything that changes an arm's behaviour.

        Excludes the arm's name, the name of its `agents` entry, and model base URLs, so
        renaming an arm or an agent entry, or moving a server to another port, keeps results.
        """
        payload = {
            "arm": arm.model_dump(mode="json", exclude={"name", "agent"}),
            "agent": self.agent_spec(arm).model_dump(mode="json"),
            "limits": self.limits.model_dump(mode="json"),
            "executor": self.models[arm.executor].model_dump(mode="json", exclude={"base_url"}),
            "advisor_model": (
                self.models[ADVISOR_MODEL].model_dump(mode="json", exclude={"base_url"})
                if arm.advisor
                else None
            ),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def run_config(self, arm: Arm, task: str, seed: int) -> RunConfig:
        """The advisor.json contents for one work item."""
        return RunConfig(
            run=RunIdentity(
                experiment=self.name,
                arm=arm.name,
                task=task,
                seed=seed,
                config_hash=self.config_hash(arm),
            ),
            executor=self.models[arm.executor],
            advisor_model=self.models[ADVISOR_MODEL] if arm.advisor else None,
            advisor=arm.advisor,
        )


def interpolate_env(value: Any, env: Mapping[str, str]) -> Any:
    """Replace ${VAR} and ${VAR:-default} in every string; fail listing all missing vars."""
    missing: set[str] = set()

    def sub(match: re.Match[str]) -> str:
        name, default = match.groups()
        if name in env:
            return env[name]
        if default is None:
            missing.add(name)
            return match.group(0)
        return default

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return _ENV_REF.sub(sub, node)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(value)
    if missing:
        raise ConfigError(f"unset environment variables: {', '.join(sorted(missing))}")
    return result


def expand_sweeps(arms: list[Any]) -> list[Any]:
    """Expand list-valued `advisor.level` / `advisor.max_consults` into one arm per combination.

    `level: [L1, L2]` on arm A2 yields arms A2-L1 and A2-L2.
    """
    expanded = []
    for arm in arms:
        advisor = arm.get("advisor") if isinstance(arm, dict) else None
        axes = {
            k: v
            for k, v in (advisor or {}).items()
            if k in ("level", "max_consults") and isinstance(v, list)
        }
        if not axes:
            expanded.append(arm)
            continue
        for combo in itertools.product(*axes.values()):
            variant = copy.deepcopy(arm)
            variant["advisor"].update(zip(axes, combo))
            variant["name"] = "-".join([str(arm.get("name")), *map(str, combo)])
            expanded.append(variant)
    return expanded
