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
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from llm_second_opinion.contracts import (
    AdvisorSettings,
    ModelEndpoint,
    RunConfig,
    RunIdentity,
    Strict,
)
from llm_second_opinion.tasks import Manifest

ADVISOR_MODEL = "advisor"  # key in `models` that advisor arms consult
_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")
_ROUTING = {"base_url", "headers", "header_env"}  # how a model is reached, not which model
_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?(\w+)\s*=\s*(.*?)\s*$")


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
    split: Literal["dev", "test"] | None = Field(
        None, description="Run only the manifest's tasks in this split; None runs all of them."
    )
    task_ids: list[str] | None = Field(
        None, description="Run only these tasks (after `split`); None runs all of them."
    )
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

    def select(self, manifest: Manifest) -> Manifest:
        """The manifest restricted to `split` and `task_ids`. Selection is not part of the
        config hash: it picks which items run, not how they behave."""
        tasks = [t for t in manifest.tasks if self.split in (None, t.split)]
        if self.task_ids is not None:
            unknown = sorted(set(self.task_ids) - {t.id for t in tasks})
            if unknown:
                where = f"split {self.split!r} of " if self.split else ""
                raise ConfigError(f"task_ids not in {where}{self.tasks}: {', '.join(unknown)}")
            tasks = [t for t in tasks if t.id in self.task_ids]
        if not tasks:
            raise ConfigError(f"no tasks selected from {self.tasks}")
        return manifest.model_copy(update={"tasks": tasks})

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

        Excludes the arm's name, the name of its `agents` entry, and how a model is reached
        (base URL, headers), so renaming an arm or an agent entry, moving a server, or
        changing credentials keeps results.
        """
        payload = {
            "arm": arm.model_dump(mode="json", exclude={"name", "agent"}),
            "agent": self.agent_spec(arm).model_dump(mode="json"),
            "limits": self.limits.model_dump(mode="json"),
            "executor": self.models[arm.executor].model_dump(mode="json", exclude=_ROUTING),
            "advisor_model": (
                self.models[ADVISOR_MODEL].model_dump(mode="json", exclude=_ROUTING)
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
    """Replace ${VAR} and ${VAR:-default} in every string, mapping keys included (so header
    names can stay out of the YAML too); fail listing all missing vars."""
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
            return {walk(k): walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(value)
    if missing:
        raise ConfigError(f"unset environment variables: {', '.join(sorted(missing))}")
    return result


def load_dotenv(path: Path, env: dict[str, str] | os._Environ[str] = os.environ) -> list[str]:
    """Set `NAME=value` lines from a .env file into `env`, without overriding variables that
    are already set. Values may be quoted; `#` starts a comment line. Returns the names set."""
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _DOTENV_LINE.match(line)
        if not match:
            raise ConfigError(f"{path}: cannot parse a line (expected NAME=value)")
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name not in env:
            env[name] = value
            loaded.append(name)
    return loaded


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
