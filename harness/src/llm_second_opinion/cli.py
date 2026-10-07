"""The `bench` command line."""

import csv
import os
import time
from collections import Counter
from pathlib import Path

import click
from pydantic import ValidationError

from llm_second_opinion import __version__
from llm_second_opinion.config import ConfigError, Experiment, load_dotenv
from llm_second_opinion.contracts import render_schemas, stale_schemas
from llm_second_opinion.ledger import Ledger
from llm_second_opinion.metering import read_usage, spend
from llm_second_opinion.mock_server import MockServer
from llm_second_opinion.picker import RULES, compose, groups
from llm_second_opinion.report import (
    current_rows,
    format_pairs,
    format_pareto,
    format_spend,
    format_table,
    paired_comparisons,
    pareto,
    provenance,
    recorded_hashes,
    report_record,
    spend_summary,
    summarize,
    write_csv,
    write_pairs_csv,
    write_report_json,
)
from llm_second_opinion.review import ADVISOR, Reviewer
from llm_second_opinion.runner import Runner, task_image
from llm_second_opinion.scorers import (
    DEFAULT_LAMBDA,
    DEFAULT_MU,
    Params,
    Prober,
    arm_means,
    attempt_rel,
    format_diagnostics,
    probe_env,
    read_scores,
    score_experiment,
)
from llm_second_opinion.tasks import Manifest
from llm_second_opinion.tracking import DEFAULT_URI, Tracker, TrackingError, check_server

REPO = Path(__file__).resolve().parents[3]
REPO_SCHEMAS = REPO / "schemas"


@click.group()
@click.version_option(__version__)
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=REPO / ".env",
    envvar="BENCH_ENV_FILE",
    show_default=True,
    help="Endpoints and secrets as NAME=value lines (gitignored). Exported variables win.",
)
def main(env_file: Path) -> None:
    """Run and score coding-agent experiments."""
    try:
        load_dotenv(env_file)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e


EXPERIMENT = click.argument("experiment", type=click.Path(exists=True, dir_okay=False))
RUNS_DIR = click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("runs"),
    show_default=True,
    help="Ledger and per-item artifacts go in RUNS_DIR/<experiment>/.",
)


def _load(experiment: str) -> Experiment:
    try:
        return Experiment.from_yaml(experiment)
    except (ConfigError, ValidationError, OSError) as e:
        raise click.ClickException(f"{experiment}: {e}") from e


@main.command()
@EXPERIMENT
@RUNS_DIR
@click.option("--arm", help="Run only this arm.")
@click.option("--task", help="Run only this task id.")
@click.option("--parallel", type=click.IntRange(min=1), help="Override execution.parallel.")
@click.option(
    "--mlflow",
    envvar="MLFLOW_TRACKING_URI",
    default=DEFAULT_URI,
    show_default=True,
    help="MLflow tracking URI ($MLFLOW_TRACKING_URI). Every run is tracked; the server must be up.",
)
@click.option(
    "--no-mlflow",
    is_flag=True,
    help="Skip MLflow. Only for harness tests and CI: results would not be tracked.",
)
@click.option(
    "--final",
    is_flag=True,
    help="Allow tasks in the manifest's held-out `test` split: the one confirmatory run. "
    "Recorded in the ledger and as the MLflow tag lso.final.",
)
@click.option(
    "--no-preflight",
    is_flag=True,
    help="Skip the check that every endpoint reports token usage. Only for the mock server "
    "and tests.",
)
@click.option(
    "--proxy-host",
    help="Address the metering proxy binds. Default: 127.0.0.1 on macOS (Docker Desktop "
    "forwards host.docker.internal there), the Docker bridge gateway on Linux.",
)
@click.option("--dry-run", is_flag=True, help="List the work items and stop.")
def run(
    experiment: str,
    runs_dir: Path,
    arm: str | None,
    task: str | None,
    parallel: int | None,
    mlflow: str,
    no_mlflow: bool,
    final: bool,
    no_preflight: bool,
    proxy_host: str | None,
    dry_run: bool,
) -> None:
    """Run an experiment in containers; resumes where it stopped.

    Tasks in the manifest's `test` split run only with --final: prompts are tuned on `dev`,
    and `test` is run once to confirm the chosen policies.
    """
    exp = _load(experiment)
    try:
        runner = Runner(
            exp,
            runs_dir,
            parallel=parallel,
            final=final,
            echo=click.echo,
            preflight=not no_preflight,
            proxy_host=proxy_host,
        )
        items = runner.items(arm, task)
    except (ConfigError, ValidationError, OSError, KeyError) as e:
        raise click.ClickException(f"{experiment}: {e}") from e
    arms = {i.arm.name: i.arm for i in items}.values()
    click.echo(
        f"{exp.name}: {len(arms)} arm(s) x {len({i.task.id for i in items})} task(s) "
        f"x {exp.seeds} seed(s) = {len(items)} item(s)"
    )
    for a in arms:
        advisor = f"advisor {a.advisor.level}" if a.advisor else "no advisor"
        click.echo(
            f"  {a.name:<12} {a.agent}/{a.executor:<10} {advisor:<12} {runner.hashes[a.name]}"
        )
    if dry_run:
        return
    try:
        if runner.check_final(items):
            click.echo("FINAL run on held-out test tasks (--final)")
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    if no_mlflow:
        click.echo("MLflow is off (--no-mlflow): results go to the ledger only", err=True)
    else:
        try:
            check_server(mlflow)
        except TrackingError as e:
            raise click.ClickException(f"{e}; or pass --no-mlflow (tests and CI only)") from e
        runner.tracker = Tracker(mlflow, exp.name, final=final)
        click.echo(f"tracking in MLflow: {mlflow} (experiment {exp.name!r})")
    try:
        outcomes = runner.run(arm, task)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    counts = Counter(o.status for o in outcomes)
    resolved = sum(bool(o.resolved) for o in outcomes if o.status == "done")
    click.echo(
        f"done {counts['done']}, skipped {counts['skipped']}, failed {counts['failed']}; "
        f"resolved {resolved}/{counts['done']} this session"
    )
    if counts["failed"]:
        raise click.ClickException(f"{counts['failed']} item(s) failed; rerun to retry them")


@main.command()
@EXPERIMENT
@RUNS_DIR
@click.option(
    "--csv",
    "csv_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also write one row per work item.",
)
@click.option(
    "--pairs-csv",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also write the paired comparisons, one row per arm.",
)
@click.option("--baseline", help="Arm to compare the others with [default: A0 when present].")
@click.option("--ceiling", help="Arm for the share of the gap closed [default: A4 when present].")
def report(
    experiment: str,
    runs_dir: Path,
    csv_path: Path | None,
    pairs_csv: Path | None,
    baseline: str | None,
    ceiling: str | None,
) -> None:
    """Summarize finished work items per arm from the ledger, with paired comparisons and
    the total spend. Also writes RUNS_DIR/<experiment>/reports/<time>.json (and report.json)."""
    exp = _load(experiment)
    ledger_path = runs_dir / exp.name / "ledger.sqlite"
    if not ledger_path.exists():
        raise click.ClickException(f"no ledger at {ledger_path}; run the experiment first")
    ledger = Ledger(ledger_path)
    rows = ledger.rows(exp.name)
    attempts = ledger.attempts(exp.name)
    sessions = ledger.sessions(exp.name)
    selected = exp.select(Manifest.from_yaml(exp.tasks)).tasks
    images = {t.id: task_image(t) for t in selected}
    hashes = recorded_hashes(exp, ledger.fingerprints(exp.name))
    try:
        pairs = paired_comparisons(exp, rows, baseline, ceiling, hashes=hashes, images=images)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    summaries = summarize(exp, rows, len(selected), hashes, images, attempts)
    front = pareto(exp, rows, hashes, images)
    spent = spend_summary(
        exp, rows, attempts, ledger.preflight(exp.name), hashes, images, ledger.probe(exp.name)
    )
    diagnostics = _diagnostics(exp, rows, runs_dir / exp.name, hashes, images)
    click.echo(provenance(exp, rows, sessions))
    click.echo()
    click.echo(format_table(summaries))
    click.echo()
    click.echo(format_pairs(pairs))
    click.echo()
    click.echo(format_pareto(front))
    click.echo()
    click.echo(format_spend(spent))
    click.echo()
    click.echo(format_diagnostics(diagnostics, [a.name for a in exp.arms]))
    record = report_record(exp, summaries, pairs, front, spent, rows, sessions, diagnostics)
    click.echo(f"wrote {write_report_json(record, runs_dir / exp.name)}")
    if csv_path:
        write_csv(current_rows(exp, rows, hashes, images), csv_path)
        click.echo(f"wrote {csv_path}")
    if pairs_csv:
        write_pairs_csv(pairs, pairs_csv)
        click.echo(f"wrote {pairs_csv}")


def _diagnostics(exp: Experiment, rows: list, exp_dir: Path, hashes, images) -> dict:
    """Per-arm means of the stored scores of the items the report counts."""
    scores = read_scores(exp_dir)
    records = []
    for r in current_rows(exp, rows, hashes, images):
        record = scores.get(attempt_rel(r)) if r["status"] == "done" else None
        if record and record.get("config_hash") == r["config_hash"]:
            records.append(record)
    return arm_means(records)


@main.command()
@EXPERIMENT
@RUNS_DIR
@click.option("--arm", help="Regrade only this arm.")
@click.option("--task", help="Regrade only this task id.")
@click.option("--parallel", type=click.IntRange(min=1), help="Override execution.parallel.")
def regrade(
    experiment: str, runs_dir: Path, arm: str | None, task: str | None, parallel: int | None
) -> None:
    """Grade the stored patches of done items again with the current grader.

    No agent runs. Every config hash in the ledger is covered; the previous grade files are
    kept next to the new ones (grade.v<version>.json), and grade.json records the grader
    version. MLflow runs keep the grade they were logged with.
    """
    exp = _load(experiment)
    if not (runs_dir / exp.name / "ledger.sqlite").exists():
        raise click.ClickException(f"no ledger in {runs_dir / exp.name}; run the experiment first")
    try:
        runner = Runner(exp, runs_dir, parallel=parallel, echo=click.echo)
        outcomes = runner.regrade(arm, task)
    except (ConfigError, KeyError) as e:
        raise click.ClickException(str(e)) from e
    counts = Counter(o.status for o in outcomes)
    resolved = sum(bool(o.resolved) for o in outcomes if o.status == "done")
    click.echo(
        f"regraded {counts['done']}, skipped {counts['skipped']}, failed {counts['failed']}; "
        f"resolved {resolved}/{counts['done']}"
    )
    if counts["failed"]:
        raise click.ClickException(f"{counts['failed']} item(s) could not be regraded")


@main.command()
@EXPERIMENT
@RUNS_DIR
@click.option("--arm", default="A0", show_default=True, help="The arm whose attempts are pooled.")
@click.option("--group", default=3, show_default=True, type=click.IntRange(min=2))
@click.option("--parallel", type=click.IntRange(min=1), help="Override execution.parallel.")
@click.option("--csv", "csv_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--review",
    "review_level",
    type=click.Choice(["L2", "L3"]),
    help="Also run the review consult (the `advisor` model ranks each group's candidates) at "
    "this level. Spends advisor quota; records are kept, so a rerun asks only new groups.",
)
@click.option(
    "--no-preflight",
    is_flag=True,
    help="Skip the advisor endpoint's usage preflight (mock server and tests only).",
)
def pick(
    experiment: str,
    runs_dir: Path,
    arm: str,
    group: int,
    parallel: int | None,
    csv_path,
    review_level: str | None,
    no_preflight: bool,
) -> None:
    """Best-of-GROUP from an arm's attempts (`L-best3`): pick one of seeds group*s ..
    group*s+group-1 locally and score the pick.

    Runs the local check (the repository's own tests, no hidden test patch) on each done
    attempt and on each task's base commit first; checks are kept, so a rerun only adds the
    missing ones. No agent runs.
    """
    exp = _load(experiment)
    if not (runs_dir / exp.name / "ledger.sqlite").exists():
        raise click.ClickException(f"no ledger in {runs_dir / exp.name}; run the experiment first")
    try:
        runner = Runner(exp, runs_dir, parallel=parallel, echo=click.echo)
        checked, failed = runner.local_checks(arm)
        done = runner.done_rows(arm)
        reviews = None
        if review_level:
            reviews = _reviews(exp, runner, done, group, review_level, not no_preflight)
        rows = compose(runner.dir, done, group, reviews)
    except (ConfigError, KeyError, ValueError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"local checks run {checked}, failed {failed}; {len(rows)} group(s) of {group}")
    if not rows:
        return
    click.echo(f"  {'oracle (any candidate resolves)':34} {sum(r['oracle'] for r in rows):>5}")
    click.echo(f"  {'random (expected)':34} {sum(r['mean'] for r in rows):>7.1f}")
    for name in [*RULES, *(["review"] if reviews is not None else [])]:
        click.echo(f"  {name:34} {sum(r[name] for r in rows):>5}")
    if reviews is not None:
        asked = sum(r["review consulted"] for r in rows)
        click.echo(f"  review consulted in {asked} of {len(rows)} group(s)")
    if csv_path:
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    if failed:
        raise click.ClickException(f"{failed} local check(s) failed; rerun to retry them")


def _reviews(
    exp: Experiment, runner: Runner, done: list, group: int, level: str, check: bool
) -> dict[tuple[str, int], dict]:
    """Review records for every complete group, asking the advisor for the missing ones."""
    tasks = {t.id: t for t in runner.manifest.tasks}
    reviewer = Reviewer(exp, runner.dir, level, probe_env(exp.models[ADVISOR], os.environ))
    out = runner.dir / "preflight" / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-review"
    reviewer.start(out if check else None)
    records = {}
    try:
        for g in groups(runner.dir, done, group):
            rec = reviewer.record(g, tasks[g.task])
            if rec is not None:
                records[(g.task, g.s)] = rec
    finally:
        reviewer.stop()
    click.echo(f"review: {reviewer.calls} advisor call(s), the rest from records")
    for e in reviewer.errors:
        click.echo(f"  review error (rerun to retry): {e}", err=True)
    return records


@main.command()
@EXPERIMENT
@RUNS_DIR
@click.option("--arm", help="Score only this arm.")
@click.option("--task", help="Score only this task id.")
@click.option(
    "--probe",
    "probe_key",
    metavar="MODEL_KEY",
    help="Also run the re-identification probe with this model (a key in `models`), through "
    "the metering proxy; answers are cached, so scoring again does not query again.",
)
@click.option(
    "--lambda",
    "cost_lambda",
    type=float,
    default=DEFAULT_LAMBDA,
    show_default=True,
    help="cost_penalised = resolve - LAMBDA * advisor cost (USD).",
)
@click.option(
    "--mu",
    type=float,
    default=DEFAULT_MU,
    show_default=True,
    help="exposure_penalised = resolve - MU * exposure (see --exposure-unit).",
)
@click.option(
    "--exposure-unit",
    type=click.Choice(["ktok", "leaked"]),
    default="ktok",
    show_default=True,
    help="ktok: advisor prompt tokens / 1000; leaked: the leaked_units share.",
)
@click.option(
    "--mlflow",
    envvar="MLFLOW_TRACKING_URI",
    default=DEFAULT_URI,
    show_default=True,
    help="MLflow tracking URI: item runs get score_* metrics, arm runs score_mean_*.",
)
@click.option("--no-mlflow", is_flag=True, help="Skip MLflow (tests and CI).")
@click.option(
    "--no-preflight",
    is_flag=True,
    help="Skip the probe endpoint's usage preflight (mock server and tests only).",
)
def score(
    experiment: str,
    runs_dir: Path,
    arm: str | None,
    task: str | None,
    probe_key: str | None,
    cost_lambda: float,
    mu: float,
    exposure_unit: str,
    mlflow: str,
    no_mlflow: bool,
    no_preflight: bool,
) -> None:
    """Re-run the scorers over stored runs, without running agents.

    Resolve is the acceptance score; everything else (partial, gold similarity, the
    penalised rewards, dependence and exposure) is feedback or a diagnostic. Writes
    scores.json per attempt and RUNS_DIR/<experiment>/scores.jsonl.
    """
    exp = _load(experiment)
    exp_dir = runs_dir / exp.name
    if not (exp_dir / "ledger.sqlite").exists():
        raise click.ClickException(f"no ledger in {exp_dir}; run the experiment first")
    tracker = None
    if no_mlflow:
        click.echo("MLflow is off (--no-mlflow): scores go to files only", err=True)
    else:
        try:
            check_server(mlflow)
        except TrackingError as e:
            raise click.ClickException(f"{e}; or pass --no-mlflow (tests and CI only)") from e
        tracker = Tracker(mlflow, exp.name)
    params = Params(cost_lambda, mu, exposure_unit)
    ledger = Ledger(exp_dir / "ledger.sqlite")
    prober = None
    try:
        if probe_key:
            prober = _prober(exp, probe_key, exp_dir, ledger, preflight=not no_preflight)
        score_experiment(
            exp, runs_dir, arm=arm, task=task, params=params, prober=prober, tracker=tracker,
            echo=click.echo,
        )  # fmt: skip
    except (ConfigError, KeyError) as e:
        raise click.ClickException(str(e)) from e
    finally:
        if prober:
            prober.stop()
        if tracker:
            tracker.close()
    if prober:
        click.echo(f"probe: {prober.calls} new call(s), the rest from the cache")


def _prober(exp: Experiment, key: str, exp_dir: Path, ledger: Ledger, preflight: bool) -> Prober:
    """The probe's proxy, after a usage preflight of its endpoint (recorded in the ledger's
    preflight table, so it counts in the total spend)."""
    if key not in exp.models:
        raise ConfigError(f"--probe {key!r} is not in models: {', '.join(exp.models)}")
    endpoint = exp.models[key]

    def record(out: Path, records: list, cost: float | None) -> None:
        ok = [r for r in records if 200 <= r.status < 300]
        ledger.record_probe(
            exp.name,
            dir=str(out.relative_to(exp_dir)),
            model_key=key,
            model=endpoint.model,
            calls=len(ok),
            failed_calls=len(records) - len(ok),
            prompt_tokens=sum(r.prompt_tokens for r in ok),
            completion_tokens=sum(r.completion_tokens for r in ok),
            cached_tokens=sum(r.cached_tokens for r in ok),
            reasoning_tokens=sum(r.reasoning_tokens for r in ok),
            cost_usd=cost,
        )

    prober = Prober(
        exp, key, exp_dir / "probe-cache", probe_env(endpoint, os.environ), record=record
    )
    if not preflight:
        click.echo("probe preflight skipped (--no-preflight: mock server and tests only)")
        return prober.start(None)
    started = time.time()
    out = exp_dir / "preflight" / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime(started))}-probe"
    try:
        prober.start(out)
    except ConfigError:
        prober.stop()
        raise
    finally:
        usage = read_usage(out / key / "usage.jsonl")
        if usage:
            ok = [r for r in usage if 200 <= r.status < 300]
            ledger.record_preflight(
                exp.name,
                started,
                model_key=key,
                model=endpoint.model,
                calls=len(ok),
                failed_calls=len(usage) - len(ok),
                prompt_tokens=sum(r.prompt_tokens for r in usage),
                completion_tokens=sum(r.completion_tokens for r in usage),
                cached_tokens=sum(r.cached_tokens for r in usage),
                reasoning_tokens=sum(r.reasoning_tokens for r in usage),
                cost_usd=spend(out / key / "usage.jsonl", {"executor": exp.prices.get(key)})[
                    "cost_usd"
                ],
            )
    click.echo(f"probe preflight passed: {key}")
    return prober


@main.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=REPO_SCHEMAS,
    show_default=True,
)
@click.option("--check", is_flag=True, help="Fail if the files differ from the models.")
def schemas(out: Path, check: bool) -> None:
    """Write the contract JSON Schemas generated from the Pydantic models."""
    if check:
        stale = stale_schemas(out)
        if stale:
            raise click.ClickException(f"out of date: {', '.join(stale)}; run `bench schemas`")
        click.echo("schemas are up to date")
        return
    out.mkdir(parents=True, exist_ok=True)
    for name, text in render_schemas().items():
        (out / name).write_text(text)
        click.echo(f"wrote {out / name}")


@main.command("mock-server")
@click.option("--recordings", type=click.Path(dir_okay=False, path_type=Path), required=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
@click.option(
    "--upstream",
    help="Proxy requests past the end of the recordings here and record them. "
    "The API key is read from UPSTREAM_API_KEY.",
)
def mock_server(recordings: Path, host: str, port: int, upstream: str | None) -> None:
    """Replay recorded chat completions over an OpenAI-compatible API."""
    try:
        server = MockServer(
            (host, port),
            recordings,
            upstream=upstream,
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY"),
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    mode = f"recording from {upstream}" if upstream else "replay only"
    click.echo(f"{len(server.recordings)} recording(s) at {server.url} ({mode})")
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


REPO = REPO_SCHEMAS.parent
WORK_DIR = click.option(
    "--work-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=REPO / "runs/tasks",
    show_default=True,
    help="Candidates, build and validation records, and logs go in WORK_DIR/<name>/.",
)
NAME = click.argument("name")
ONLY = click.option("--only", multiple=True, help="Only this candidate id (repeatable).")
PARALLEL = click.option("--parallel", type=click.IntRange(min=1), default=1, show_default=True)


@main.group()
def tasks() -> None:
    """Task pipeline: import candidates, build arm64 images, validate, freeze a manifest."""


@tasks.command("import")
@click.option("--dataset", type=click.Choice(["mini", "full"]), default="mini", show_default=True)
@click.option("--language", default="c++", show_default=True)
@click.option("--instance", multiple=True, help="Only this instance id (repeatable).")
@click.option("--name", help="Candidate set name [default: mswe-<dataset>-<language>].")
@click.option(
    "--cache",
    type=click.Path(file_okay=False, path_type=Path),
    default=REPO / ".cache/datasets",
    show_default=True,
)
@WORK_DIR
def tasks_import(
    dataset: str,
    language: str,
    instance: tuple[str, ...],
    name: str | None,
    cache: Path,
    work_dir: Path,
) -> None:
    """Import Multi-SWE-bench instances as candidates (dataset pinned to a revision)."""
    from llm_second_opinion.pipeline.multi_swe_bench import import_candidates

    name = name or f"mswe-{dataset}-{language.replace('+', 'p')}"
    try:
        cset = import_candidates(dataset, language, cache, set(instance) or None)
    except (ValueError, OSError) as e:
        raise click.ClickException(str(e)) from e
    path = work_dir / name / "candidates.yaml"
    cset.save(path)
    repos = Counter(c.repo for c in cset.candidates)
    click.echo(f"{len(cset.candidates)} candidate(s) from {cset.source} -> {path}")
    for repo, n in repos.most_common():
        click.echo(f"  {repo:<24} {n}")


def _candidates(work_dir: Path, name: str, only: tuple[str, ...]):
    from llm_second_opinion.pipeline.candidates import CandidateSet

    path = work_dir / name / "candidates.yaml"
    if not path.exists():
        raise click.ClickException(f"no {path}; run `bench tasks import` first")
    cset = CandidateSet.load(path)
    unknown = set(only) - {c.id for c in cset.candidates}
    if unknown:
        raise click.ClickException(f"not in {path}: {', '.join(sorted(unknown))}")
    return cset, [c for c in cset.candidates if not only or c.id in only]


@tasks.command("build")
@NAME
@ONLY
@PARALLEL
@click.option("--jobs", type=click.IntRange(min=1), default=4, show_default=True,
              help="Compiler jobs per build.")  # fmt: skip
@click.option("--timeout-minutes", type=float, default=60, show_default=True)
@click.option("--rebuild", is_flag=True, help="Build again even if a record exists.")
@click.option(
    "--recipes",
    type=click.Path(file_okay=False, path_type=Path),
    default=REPO / "tasks/repos",
    show_default=True,
)
@click.option(
    "--mirrors",
    type=click.Path(file_okay=False, path_type=Path),
    default=REPO / ".cache/mirrors",
    show_default=True,
)
@WORK_DIR
def tasks_build(
    name: str,
    only: tuple[str, ...],
    parallel: int,
    jobs: int,
    timeout_minutes: float,
    rebuild: bool,
    recipes: Path,
    mirrors: Path,
    work_dir: Path,
) -> None:
    """Build one arm64 image per candidate; resumes, skipping candidates already built."""
    from concurrent.futures import ThreadPoolExecutor

    from llm_second_opinion.pipeline.candidates import Records
    from llm_second_opinion.pipeline.images import BuildError, build_image

    _, todo = _candidates(work_dir, name, only)
    records = Records(work_dir / name / "builds.json")
    if not rebuild:
        todo = [c for c in todo if records.get(c.id) is None]
    click.echo(f"building {len(todo)} image(s), {parallel} at a time")

    def one(c) -> None:
        log = work_dir / name / c.id / "build.log"
        try:
            record = build_image(
                c, recipes, mirrors, log, jobs=jobs, timeout_s=timeout_minutes * 60
            )
        except BuildError as e:
            record = {"error": e.reason, "detail": e.detail}
        records.put(c.id, record)
        status = f"{record['build_s']} s" if "error" not in record else str(record["error"])
        click.echo(f"{c.id}: {status}")

    with ThreadPoolExecutor(parallel) as pool:
        list(pool.map(one, todo))
    failed = sum("error" in r for r in records.data.values())
    click.echo(f"{len(records.data) - failed} built, {failed} failed ({records.path})")


@tasks.command("validate")
@NAME
@ONLY
@PARALLEL
@click.option("--runs", type=click.IntRange(min=1), default=2, show_default=True)
@click.option("--cpus", type=float, default=4, show_default=True)
@click.option("--memory-gb", type=float, default=3, show_default=True)
@click.option("--timeout-minutes", type=float, default=30, show_default=True)
@click.option("--revalidate", is_flag=True, help="Validate again even if a record exists.")
@WORK_DIR
def tasks_validate(
    name: str,
    only: tuple[str, ...],
    parallel: int,
    runs: int,
    cpus: float,
    memory_gb: float,
    timeout_minutes: float,
    revalidate: bool,
    work_dir: Path,
) -> None:
    """Run each built candidate with and without the gold patch, RUNS times; resumes."""
    from concurrent.futures import ThreadPoolExecutor

    from llm_second_opinion.pipeline.candidates import Records
    from llm_second_opinion.pipeline.validate import validate
    from llm_second_opinion.runtime import Runtime

    _, todo = _candidates(work_dir, name, only)
    builds = Records(work_dir / name / "builds.json")
    records = Records(work_dir / name / "validation.json")
    todo = [c for c in todo if (b := builds.get(c.id)) and "error" not in b]
    if not revalidate:
        todo = [c for c in todo if records.get(c.id) is None]
    click.echo(f"validating {len(todo)} candidate(s), {parallel} at a time, {runs} run(s) each")
    runtime = Runtime()

    def one(c) -> None:
        try:
            record = validate(
                c,
                builds.get(c.id),
                runtime,
                work_dir / name / c.id,
                runs=runs,
                timeout_s=timeout_minutes * 60,
                cpus=cpus,
                memory_gb=memory_gb,
            )
        # Docker or I/O trouble with one candidate must not stop the others; it stays
        # unrecorded, so the next run retries it.
        except Exception as e:  # noqa: BLE001
            click.echo(f"{c.id}: error ({type(e).__name__}: {e}); will retry on the next run")
            return
        records.put(c.id, record)
        if record["status"] == "kept":
            f2p, p2p = len(record["fail_to_pass"]), len(record["pass_to_pass"])
            click.echo(f"{c.id}: kept, {f2p} fail-to-pass, {p2p} pass-to-pass")
        else:
            click.echo(f"{c.id}: dropped, {record['reason']} {record['detail']}".rstrip())

    with ThreadPoolExecutor(parallel) as pool:
        list(pool.map(one, todo))
    kept = sum(r["status"] == "kept" for r in records.data.values())
    click.echo(f"{kept} kept, {len(records.data) - kept} dropped ({records.path})")


@tasks.command("freeze")
@NAME
@click.option(
    "--version", "version", required=True, help="Manifest version, e.g. mswe-mini-cpp-v1."
)
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), required=True)
@click.option("--max-build-minutes", type=float, default=20, show_default=True)
@click.option("--max-test-minutes", type=float, default=10, show_default=True)
@click.option("--test-fraction", type=click.FloatRange(0, 1), default=0.5, show_default=True)
@click.option("--seed", type=int, default=0, show_default=True)
@click.option("--smoke", type=click.IntRange(min=0), default=3, show_default=True,
              help="Also write <out stem>-smoke.yaml with this many quick dev tasks.")  # fmt: skip
@click.option("--keep-splits", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Earlier manifest: its tasks keep their split; only new tasks are split.")  # fmt: skip
@WORK_DIR
def tasks_freeze(
    name: str,
    version: str,
    out: Path,
    max_build_minutes: float,
    max_test_minutes: float,
    test_fraction: float,
    seed: int,
    smoke: int,
    keep_splits: Path | None,
    work_dir: Path,
) -> None:
    """Write the frozen manifest: validated tasks with a dev/test split, and dropped ones."""
    from llm_second_opinion.pipeline.candidates import Records
    from llm_second_opinion.pipeline.validate import freeze
    from llm_second_opinion.pipeline.validate import smoke as smoke_set

    cset, _ = _candidates(work_dir, name, ())
    builds = Records(work_dir / name / "builds.json").data
    validations = Records(work_dir / name / "validation.json").data
    earlier = Manifest.from_yaml(keep_splits) if keep_splits else None
    kept = {t.id: t.split for t in earlier.tasks} if earlier else None
    try:
        manifest = freeze(
            cset,
            builds,
            validations,
            version=version,
            max_build_s=max_build_minutes * 60,
            max_test_s=max_test_minutes * 60,
            test_fraction=test_fraction,
            seed=seed,
            keep_splits=kept,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    header = (
        f"# Frozen task set; written by `bench tasks freeze {name} --version {version}`\n"
        f"# (split seed {seed}, test fraction {test_fraction}"
        + (f"; splits kept from {earlier.version}" if keep_splits else "")
        + "). Do not edit by hand.\n"
    )
    manifest.to_yaml(out, header)
    splits = Counter(t.split for t in manifest.tasks)
    click.echo(
        f"{len(manifest.tasks)} task(s) (dev {splits['dev']}, test {splits['test']}), "
        f"{len(manifest.dropped)} dropped -> {out}"
    )
    for reason, n in Counter(d.reason for d in manifest.dropped).most_common():
        click.echo(f"  dropped {n:>3}  {reason}")
    if smoke:
        small = smoke_set(manifest, builds, validations, smoke)
        smoke_out = out.with_name(f"{out.stem}-smoke{out.suffix}")
        small.to_yaml(smoke_out, header.replace("Frozen task set", "Smoke subset"))
        click.echo(f"{len(small.tasks)} smoke task(s) -> {smoke_out}")
