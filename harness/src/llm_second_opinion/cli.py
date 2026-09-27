"""The `bench` command line."""

import os
from collections import Counter
from pathlib import Path

import click
from pydantic import ValidationError

from llm_second_opinion import __version__
from llm_second_opinion.config import ConfigError, Experiment
from llm_second_opinion.contracts import render_schemas, stale_schemas
from llm_second_opinion.ledger import Ledger
from llm_second_opinion.mock_server import MockServer
from llm_second_opinion.report import current_rows, format_table, summarize, write_csv
from llm_second_opinion.runner import Runner
from llm_second_opinion.tasks import Manifest

REPO_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Run and score coding-agent experiments."""


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
    help="MLflow tracking URI (default: $MLFLOW_TRACKING_URI; unset means no MLflow logging).",
)
@click.option("--dry-run", is_flag=True, help="List the work items and stop.")
def run(
    experiment: str,
    runs_dir: Path,
    arm: str | None,
    task: str | None,
    parallel: int | None,
    mlflow: str | None,
    dry_run: bool,
) -> None:
    """Run an experiment in containers; resumes where it stopped."""
    exp = _load(experiment)
    try:
        runner = Runner(exp, runs_dir, parallel=parallel, echo=click.echo)
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
        click.echo(f"  {a.name:<12} {a.agent}/{a.executor:<10} {advisor:<12} {exp.config_hash(a)}")
    if dry_run:
        return
    if mlflow:
        from llm_second_opinion.tracking import Tracker  # mlflow is an optional extra

        runner.tracker = Tracker(mlflow, exp.name)
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
def report(experiment: str, runs_dir: Path, csv_path: Path | None) -> None:
    """Summarize finished work items per arm from the ledger."""
    exp = _load(experiment)
    ledger_path = runs_dir / exp.name / "ledger.sqlite"
    if not ledger_path.exists():
        raise click.ClickException(f"no ledger at {ledger_path}; run the experiment first")
    rows = Ledger(ledger_path).rows(exp.name)
    tasks = len(Manifest.from_yaml(exp.tasks).tasks)
    click.echo(format_table(summarize(exp, rows, tasks)))
    if csv_path:
        write_csv(current_rows(exp, rows), csv_path)
        click.echo(f"wrote {csv_path}")


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
    work_dir: Path,
) -> None:
    """Write the frozen manifest: validated tasks with a dev/test split, and dropped ones."""
    from llm_second_opinion.pipeline.candidates import Records
    from llm_second_opinion.pipeline.validate import freeze
    from llm_second_opinion.pipeline.validate import smoke as smoke_set

    cset, _ = _candidates(work_dir, name, ())
    builds = Records(work_dir / name / "builds.json").data
    validations = Records(work_dir / name / "validation.json").data
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
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    header = (
        f"# Frozen task set; written by `bench tasks freeze {name} --version {version}`\n"
        f"# (split seed {seed}, test fraction {test_fraction}). Do not edit by hand.\n"
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
