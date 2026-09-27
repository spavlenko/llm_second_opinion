# llm_second_opinion

An open-source harness that runs coding agents on validated C++ tasks and scores every
run on capability, cost, and exposure. A local model does the work; a cloud advisor
gives a "second opinion" through abstracted briefs, and everything sent to it is logged.

- `harness/` — Python package (MLflow 3) for orchestration, grading, tracking, and scoring
- `plugin/advisor-core/` — TypeScript advisor logic, no pi imports
- `plugin/pi-binding/` — pi extension wiring
- `schemas/` — versioned JSON Schemas: run config, events, results
- `tasks/manifests/` — frozen task sets
- `experiments/` — example experiment YAML files
- `docs/` — design notes

v1 targets a single Apple Silicon Mac with arm64 Linux containers (Colima).

## Development

Requires Python 3.11+, Node 22+ and pnpm (`brew install node pnpm`), and a Docker API
(Colima or Docker Desktop) for running experiments.

```sh
cd harness
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,tracking]'
pytest
bench --help
```

```sh
cd plugin
pnpm install
pnpm build && pnpm test
```

```sh
scripts/mlflow-server.sh   # MLflow UI and tracking at http://127.0.0.1:5050
```

Run the toy experiment end to end (containers, grading, ledger, report; no model needed):

```sh
docker build -t llm-second-opinion/toy:1 tasks/toy
export MLFLOW_TRACKING_URI=http://127.0.0.1:5050   # optional
bench run experiments/toy.yaml --parallel 4        # rerun to resume; --dry-run to list items
bench report experiments/toy.yaml --csv runs/toy.csv
```

Results land in `runs/<experiment>/`: `ledger.sqlite` and one directory per arm, task, and
seed with `advisor.json`, `result.json`, `patch.diff`, `events.jsonl`, and `grade.log`.

See [docs/spec.md](docs/spec.md) for the design and [docs/roadmap.md](docs/roadmap.md) for progress.

## License

[MIT](LICENSE)
