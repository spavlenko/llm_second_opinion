from click.testing import CliRunner

from llm_second_opinion import __version__
from llm_second_opinion.cli import main


def test_version():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_run_dry_run_lists_items(repo, tmp_path):
    exp = str(repo / "experiments/toy.yaml")
    result = CliRunner().invoke(main, ["run", exp, "--dry-run", "--runs-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "1 arm(s) x 2 task(s) x 2 seed(s) = 4 item(s)" in result.output


def test_run_reports_a_missing_manifest(repo, tmp_path):
    env = {"LOCAL_MODEL_URL": "http://l/v1", "ADVISOR_URL": "http://a/v1"}
    exp = str(repo / "experiments/abstraction-sweep.yaml")
    result = CliRunner().invoke(main, ["run", exp, "--runs-dir", str(tmp_path)], env=env)
    assert result.exit_code == 1
    assert "cpp-arm64-v1.yaml" in result.output


def test_report_needs_a_ledger(repo, tmp_path):
    exp = str(repo / "experiments/toy.yaml")
    result = CliRunner().invoke(main, ["report", exp, "--runs-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no ledger" in result.output


def test_schemas_check_passes(repo):
    result = CliRunner().invoke(main, ["schemas", "--check", "--out", str(repo / "schemas")])
    assert result.exit_code == 0, result.output
