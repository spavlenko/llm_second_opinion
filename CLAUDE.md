# llm_second_opinion

- `docs/spec.md` is the source of truth for what we are building. Read it before starting work.
  When a decision changes or a new one is made, update the spec and add a row to its Decision log.
- `docs/roadmap.md` tracks progress. Tick items as they land; add items rather than dropping them.
- `docs/lab-notes.md` is the lab notebook: record interesting or unexpected findings, one short
  line each. `docs/related-work.md` is the literature survey.

## Commands

```sh
cd harness
.venv/bin/pytest -q                     # tests (Docker ones skip without a daemon)
.venv/bin/ruff check src tests && .venv/bin/ruff format src tests
.venv/bin/bench schemas                 # regenerate schemas/ after changing contracts.py
```

```sh
cd plugin
pnpm gen:types                          # regenerate advisor-core/src/contracts.ts after `bench schemas`
pnpm check:types && pnpm build && pnpm test
```

```sh
docker build -t llm-second-opinion/toy:1 tasks/toy     # toy task image
harness/.venv/bin/bench run experiments/toy.yaml --parallel 4   # full lifecycle, no model
harness/.venv/bin/bench report experiments/toy.yaml --csv runs/toy.csv
harness/.venv/bin/bench pick experiments/gate-a0.yaml --csv runs/gate-a0-best3.csv   # L-best3
harness/.venv/bin/bench mock-server --recordings harness/tests/fixtures/pi-toy-add.jsonl --host 0.0.0.0 &
harness/.venv/bin/bench run experiments/toy-pi.yaml --task toy-add   # pi end to end, mock model
```

```sh
cd harness                              # task pipeline; work files in runs/tasks/<name>/
.venv/bin/bench tasks import --dataset mini                  # -> mswe-mini-cpp
.venv/bin/bench tasks build mswe-mini-cpp --parallel 2       # resumable
.venv/bin/bench tasks validate mswe-mini-cpp --parallel 2    # resumable
.venv/bin/bench tasks freeze mswe-mini-cpp --version mswe-mini-cpp-v2 \
  --out ../tasks/manifests/mswe-mini-cpp-v2.yaml
.venv/bin/bench tasks freeze mswe-full-cpp --version mswe-full-cpp-v1 \
  --out ../tasks/manifests/mswe-full-cpp-v1.yaml --keep-splits ../tasks/manifests/mswe-mini-cpp-v2.yaml
```

```sh
scripts/mlflow-server.sh                # MLflow at http://127.0.0.1:5050, data in .mlflow/;
                                        # `bench run` requires it (--no-mlflow: tests/CI only)
```

Toolchain: Python 3.14 (venv in `harness/.venv`), Node 26 + pnpm 12 via Homebrew, TypeScript 7,
vitest 5, MLflow 3.16. Port 5000 is taken by macOS AirPlay, hence 5050.
