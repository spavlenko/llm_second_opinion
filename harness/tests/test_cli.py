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
    exp = tmp_path / "exp.yaml"
    text = (repo / "experiments/toy.yaml").read_text()
    exp.write_text(text.replace("../tasks/manifests/toy-v1.yaml", "missing-v1.yaml"))
    result = CliRunner().invoke(main, ["run", str(exp), "--runs-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "missing-v1.yaml" in result.output


def test_run_refuses_test_tasks_without_final(repo, tmp_path):
    exp = tmp_path / "exp.yaml"
    manifest = repo / "tasks/manifests/mswe-mini-cpp-v1.yaml"
    text = (repo / "experiments/mswe-smoke-gold.yaml").read_text()
    exp.write_text(text.replace("../tasks/manifests/mswe-mini-cpp-v1-smoke.yaml", str(manifest)))
    args = ["run", str(exp), "--runs-dir", str(tmp_path), "--no-mlflow"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 1
    assert "25 task(s) to run are in the manifest's held-out `test` split" in result.output
    assert "the experiment sets no split" in result.output
    assert "--final" in result.output
    dry = CliRunner().invoke(main, [*args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    exp.write_text(exp.read_text() + "split: test\n")
    result = CliRunner().invoke(main, args)
    assert "split is 'test'" in result.output


def test_report_needs_a_ledger(repo, tmp_path):
    exp = str(repo / "experiments/toy.yaml")
    result = CliRunner().invoke(main, ["report", exp, "--runs-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no ledger" in result.output


def test_schemas_check_passes(repo):
    result = CliRunner().invoke(main, ["schemas", "--check", "--out", str(repo / "schemas")])
    assert result.exit_code == 0, result.output
