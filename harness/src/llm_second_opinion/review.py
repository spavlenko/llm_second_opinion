"""The `review` consult on `L-best3` groups: the advisor ranks an item's local candidates and
its first choice replaces the local picker's.

Gated: only usable candidates (`Visible.usable`) are shown, one per distinct patch (whitespace
ignored), and with fewer than 2 there is no consult (the local rule picks). Labels A, B, C are
assigned in an order shuffled by (task, s). The brief and the call are advisor-core's
(`plugin/advisor-core/dist/review-cli.js`, so redaction is the plugin's), through the metering
proxy. Each group's record is kept in `runs/<experiment>/review/<task>-s<s>.json`; a group
with a record is never asked again. An advisor error leaves no record, so a rerun retries it.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
from pathlib import Path
from typing import Any

from llm_second_opinion.config import Experiment
from llm_second_opinion.metering import UPSTREAM_TIMEOUT_S, MeteringProxy, preflight
from llm_second_opinion.picker import Group, Visible, pick_local
from llm_second_opinion.tasks import Task

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "plugin/advisor-core/dist/review-cli.js"
PROMPTS = REPO / "prompts/review"
REVIEW_DIR = "review"  # runs/<experiment>/review/
ADVISOR = "advisor"  # the key in the experiment's models
MAX_DIFF_TOKENS = 6000
MAX_ANSWER_TOKENS = 16000  # a safety ceiling; the prompt asks for 250 words
LABELS = "ABCDEFGH"


def prompts() -> tuple[dict[str, str], str]:
    """The review prompt set and its hash."""
    texts = {name: (PROMPTS / f"{name}.md").read_text() for name in ("system", "brief")}
    digest = hashlib.sha256(json.dumps(texts, sort_keys=True).encode()).hexdigest()[:16]
    return texts, digest


def shown(g: Group) -> list[Visible]:
    """The candidates the advisor sees: usable, one per distinct patch (the local rule's
    favourite among duplicates), in the group's shuffled order."""
    by_patch: dict[str, list[Visible]] = {}
    for v in g.visible:
        if v.usable():
            by_patch.setdefault(re.sub(r"\s+", "", g.patches[v.seed]), []).append(v)
    out = [pick_local(same, g.base) for same in by_patch.values()]
    out.sort(key=lambda v: v.seed)
    random.Random(f"{g.task}/{g.s}").shuffle(out)
    return out


def brief_candidates(g: Group, cands: list[Visible]) -> list[dict[str, Any]]:
    return [
        {
            "label": LABELS[i],
            "diff": g.patches[v.seed],
            "builds": not v.check["build_failed"],
            "broken": sorted(set(g.base["passed"]) - set(v.check["passed"])),
        }
        for i, v in enumerate(cands)
    ]


class Reviewer:
    """Runs review consults for one experiment at one level, through its own metering proxy
    (role `advisor`; calls recorded in `review/usage.jsonl`)."""

    def __init__(self, exp: Experiment, exp_dir: Path, level: str, env: dict[str, str]):
        if ADVISOR not in exp.models:
            raise ValueError(f"no `{ADVISOR}` in the experiment's models")
        if not CLI.exists():
            raise FileNotFoundError(f"{CLI} is missing: run `pnpm build` in plugin/")
        self.endpoint = exp.models[ADVISOR]
        self.dir = exp_dir / REVIEW_DIR
        self.level = level
        self.env = env
        self.templates, self.prompt_hash = prompts()
        self.proxy: MeteringProxy | None = None
        self.meter = None
        self.calls = 0
        self.errors: list[str] = []

    def start(self, preflight_dir: Path | None) -> Reviewer:
        self.proxy = MeteringProxy("127.0.0.1", env=self.env).start()
        if preflight_dir is not None:
            preflight(self.proxy, {ADVISOR: self.endpoint}, preflight_dir)
        self.meter = self.proxy.register({"advisor": self.endpoint}, self.dir / "usage.jsonl", None)
        return self

    def stop(self) -> None:
        if self.proxy:
            if self.meter:
                self.meter.close(UPSTREAM_TIMEOUT_S)
                self.proxy.unregister(self.meter)
            self.proxy.stop()
            self.proxy = None

    def path(self, g: Group) -> Path:
        return self.dir / f"{g.task}-s{g.s}.json"

    def record(self, g: Group, task: Task) -> dict[str, Any] | None:
        """The group's review record: from disk if it has one, else made now (one consult, or
        none when gated). None after an advisor error."""
        path = self.path(g)
        if path.exists():
            return json.loads(path.read_text())
        cands = shown(g)
        fallback = pick_local(g.visible, g.base).seed
        rec: dict[str, Any] = {
            "task": g.task,
            "s": g.s,
            "level": self.level,
            "prompt_hash": self.prompt_hash,
            "labels": {LABELS[i]: v.seed for i, v in enumerate(cands)},
            "consulted": len(cands) >= 2,
            "pick_seed": fallback,
            "fallback": True,
        }
        if rec["consulted"]:
            result = self._call(g, task, cands)
            self.calls += 1
            if result.get("error"):
                self.errors.append(f"{g.task} s{g.s}: {result['error'][:300]}")
                return None
            pick = result.get("pick")
            rec |= {"result": result, "fallback": pick is None}
            if pick is not None:
                rec["pick_seed"] = rec["labels"][pick]
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=2))
        return rec

    def _call(self, g: Group, task: Task, cands: list[Visible]) -> dict[str, Any]:
        assert self.proxy and self.meter
        endpoint = self.endpoint.model_copy(
            update={
                "base_url": self.proxy.url(self.meter, "advisor", "127.0.0.1"),
                "api_key_env": None,
                "headers": {},
                "header_env": {},
            }
        )
        request = {
            "endpoint": endpoint.model_dump(mode="json"),
            "level": self.level,
            "task": g.task,
            "issue": task.problem_statement,
            "candidates": brief_candidates(g, cands),
            "templates": self.templates,
            "max_diff_tokens": MAX_DIFF_TOKENS,
            "max_answer_tokens": MAX_ANSWER_TOKENS,
            "request_id": f"review-{g.task}-s{g.s}",
        }
        done = subprocess.run(
            ["node", str(CLI)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=UPSTREAM_TIMEOUT_S + 60,
            check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(f"review-cli failed: {done.stderr.strip()[-2000:]}")
        return json.loads(done.stdout)
