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
