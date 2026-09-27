# llm_second_opinion

- `docs/spec.md` is the source of truth for what we are building. Read it before starting work.
  When a decision changes or a new one is made, update the spec and add a row to its Decision log.
- `docs/roadmap.md` tracks progress. Tick items as they land; add items rather than dropping them.

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
```

```sh
scripts/mlflow-server.sh                # MLflow at http://127.0.0.1:5050, data in .mlflow/
```

Toolchain: Python 3.14 (venv in `harness/.venv`), Node 26 + pnpm 12 via Homebrew, TypeScript 7,
vitest 5, MLflow 3.16. Port 5000 is taken by macOS AirPlay, hence 5050.
