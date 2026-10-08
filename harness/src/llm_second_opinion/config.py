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
from pydantic import Field, PrivateAttr, model_validator

from llm_second_opinion.contracts import (
    AdvisorSettings,
    ModelEndpoint,
    PromptSet,
    RunConfig,
    RunIdentity,
    Strict,
)
from llm_second_opinion.prompts import PromptSource, resolve
from llm_second_opinion.tasks import Manifest

ADVISOR_MODEL = "advisor"  # key in `models` that advisor arms consult
DEFAULT_PROMPTS = Path(__file__).resolve().parents[3] / "prompts/default"  # set `default`
_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")
_ROUTING = {"base_url", "headers", "header_env"}  # how a model is reached, not which model
# Advisor settings added after arms had run: at these values they leave the hash as it was.
_LATER_ADVISOR_DEFAULTS = {
    "report_gate": False,
    "closing_report": False,
    "experiment_report": False,
}
_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?(\w+)\s*=\s*(.*?)\s*$")


class ConfigError(ValueError):
    pass


class Limits(Strict):
    wall_minutes: float = Field(gt=0)
    max_turns: int = Field(gt=0)
    max_tokens: int | None = Field(
        None,
        gt=0,
        description="Token budget per item: prompt plus completion tokens over all roles, as "
        "the metering proxy counts them. None means no budget.",
    )


class Price(Strict):
    """A model's price in USD per million tokens, for cost in the ledger and report."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0, description="Covers reasoning tokens too.")
    cached_input_per_mtok: float | None = Field(
        None, ge=0, description="Price of cached prompt tokens; None means the input price."
    )


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
    prompts: dict[str, PromptSource] = Field(
        default_factory=dict,
        description="Prompt sets by name: a directory, or {base: <set>, <slot>: <file>}. "
        "`default` is prompts/default/ unless defined here.",
    )
    prices: dict[str, Price] = Field(
        default_factory=dict,
        description="Prices by key in `models`. Not part of the config hash: cost is derived "
        "from the metered tokens, so a price can be corrected after a run.",
    )
    arms: list[Arm] = Field(min_length=1)
    _prompt_sets: dict[str, PromptSet] = PrivateAttr(default_factory=dict)
    _manifest_version: str | None = PrivateAttr(default=None)

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
            if arm.advisor and arm.advisor.prompts not in self.prompt_sources():
                known = ", ".join(sorted(self.prompt_sources()))
                raise ValueError(
                    f"arm {arm.name}: no prompt set named {arm.advisor.prompts!r}; "
                    f"prompt sets: {known}"
                )
        # Read and check every defined set and every set an arm uses, so a bad file or
        # placeholder fails at load rather than mid-batch.
        used = {arm.advisor.prompts for arm in self.arms if arm.advisor}
        for name in sorted(set(self.prompts) | used):
            self._prompt_sets[name] = resolve(name, self.prompt_sources())
        unknown = sorted(set(self.prices) - set(self.models))
        if unknown:
            raise ValueError(f"prices for models not in models: {', '.join(unknown)}")
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
        if isinstance(raw.get("prompts"), dict):
            raw["prompts"] = {
                name: _relative_to(path.parent, source) for name, source in raw["prompts"].items()
            }
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

    def prompt_sources(self) -> dict[str, PromptSource]:
        return {"default": DEFAULT_PROMPTS, **self.prompts}

    def prompt_set(self, name: str) -> PromptSet:
        """The named prompt set, resolved and hashed; read once, so one batch sees one text."""
        if name not in self._prompt_sets:
            try:
                self._prompt_sets[name] = resolve(name, self.prompt_sources())
            except ValueError as e:
                raise ConfigError(str(e)) from e
        return self._prompt_sets[name]

    @property
    def manifest_version(self) -> str | None:
        """The task manifest's version, read once; None while the file does not exist (only
        in config checks: running or reporting reads the manifest first)."""
        if self._manifest_version is None and self.tasks.is_file():
            self._manifest_version = Manifest.from_yaml(self.tasks).version
        return self._manifest_version

    def config_hash(self, arm: Arm, fingerprint: Mapping[str, str] | None = None) -> str:
        """Hash of everything that changes an arm's behaviour.

        Excludes the arm's name, the name of its `agents` entry, and how a model is reached
        (base URL, headers), so renaming an arm or an agent entry, moving a server, or
        changing credentials keeps results. An advisor arm's prompt set counts by its text
        (the set's hash), not its name or files. Covers the manifest's version, and
        `fingerprint`: what the adapter runs that the config does not name, such as its
        fixed task prompt and the agent bundle's image ID (`AgentAdapter.fingerprint`). Unset
        optional fields of limits and models are left out, so a new optional field keeps
        existing hashes; so are advisor settings added later, while at their default
        (`_LATER_ADVISOR_DEFAULTS`). The task image is not in it: it is part of the item's ledger key.
        """
        arm_payload = arm.model_dump(mode="json", exclude={"name", "agent"})
        if arm.advisor:
            arm_payload["advisor"]["prompts"] = self.prompt_set(arm.advisor.prompts).hash
            for key, default in _LATER_ADVISOR_DEFAULTS.items():
                if arm_payload["advisor"].get(key) == default:
                    del arm_payload["advisor"][key]

        def model(key: str) -> dict:
            return self.models[key].model_dump(mode="json", exclude=_ROUTING, exclude_none=True)

        payload = {
            "arm": arm_payload,
            "agent": self.agent_spec(arm).model_dump(mode="json"),
            "limits": self.limits.model_dump(mode="json", exclude_none=True),
            "executor": model(arm.executor),
            "advisor_model": model(ADVISOR_MODEL) if arm.advisor else None,
            "manifest": self.manifest_version,
            "adapter": dict(fingerprint or {}),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def run_config(
        self, arm: Arm, task: str, seed: int, config_hash: str | None = None
    ) -> RunConfig:
        """The advisor.json contents for one work item; `config_hash` is the arm's hash with
        its adapter's fingerprint (the runner's), else the hash without one."""
        return RunConfig(
            run=RunIdentity(
                experiment=self.name,
                arm=arm.name,
                task=task,
                seed=seed,
                config_hash=config_hash or self.config_hash(arm),
            ),
            executor=self.models[arm.executor],
            advisor_model=self.models[ADVISOR_MODEL] if arm.advisor else None,
            advisor=arm.advisor,
            prompts=self.prompt_set(arm.advisor.prompts) if arm.advisor else None,
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
    are already set. Blank values are skipped, so a template left unfilled reads as unset.
    Values may be quoted; `#` starts a comment line. Returns the names set."""
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
        if value and name not in env:  # a blank `NAME=` is a placeholder, not a value
            env[name] = value
            loaded.append(name)
    return loaded


def expand_sweeps(arms: list[Any]) -> list[Any]:
    """Expand sweeps in `advisor` into one arm per combination, named by suffix.

    A list in `level`, `max_consults`, or `prompts` sweeps; `interventions` sweeps when it is
    a list of lists (a flat list is one value). `level: [L1, L2]` on arm A2 yields A2-L1 and
    A2-L2; `interventions: [[consult], [consult, stuck]]` yields A2-consult and
    A2-consult+stuck.
    """
    expanded = []
    for arm in arms:
        advisor = arm.get("advisor") if isinstance(arm, dict) else None
        axes = {k: v for k, v in (advisor or {}).items() if _is_sweep(k, v)}
        if not axes:
            expanded.append(arm)
            continue
        for combo in itertools.product(*axes.values()):
            variant = copy.deepcopy(arm)
            variant["advisor"].update(zip(axes, copy.deepcopy(combo)))
            variant["name"] = "-".join([str(arm.get("name")), *map(_suffix, combo)])
            expanded.append(variant)
    return expanded


def _is_sweep(key: str, value: Any) -> bool:
    if key == "interventions":
        return isinstance(value, list) and bool(value) and all(isinstance(v, list) for v in value)
    return key in ("level", "max_consults", "prompts") and isinstance(value, list)


def _suffix(value: Any) -> str:
    if isinstance(value, list):  # an interventions value
        return "+".join(map(str, value)) or "none"
    return str(value)


def _relative_to(base: Path, source: Any) -> Any:
    """A prompt set's paths, made absolute against the YAML file's directory."""
    if isinstance(source, str):
        return (base / source).resolve()
    if isinstance(source, dict):
        return {
            k: (base / v).resolve() if k != "base" and isinstance(v, str) else v
            for k, v in source.items()
        }
    return source
