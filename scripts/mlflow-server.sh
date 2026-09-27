#!/usr/bin/env bash
# Local MLflow tracking server: SQLite metadata and artifacts under .mlflow/ (gitignored).
# Point the harness at it with MLFLOW_TRACKING_URI=http://127.0.0.1:5050 (5000 is taken by macOS AirPlay).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/.mlflow/artifacts"
exec "$root/harness/.venv/bin/mlflow" server \
  --backend-store-uri "sqlite:///$root/.mlflow/mlflow.db" \
  --artifacts-destination "$root/.mlflow/artifacts" \
  --host 127.0.0.1 --port "${MLFLOW_PORT:-5050}" "$@"
