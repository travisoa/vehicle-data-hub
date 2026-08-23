#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT_DIR"

echo "[bootstrap] using python: $PYTHON_BIN"
"$PYTHON_BIN" -m venv .venv

echo "[bootstrap] upgrading pip"
.venv/bin/python -m pip install --upgrade pip

echo "[bootstrap] installing base requirements"
.venv/bin/python -m pip install -r requirements.txt

if [[ "${INSTALL_PLAYWRIGHT:-0}" == "1" ]]; then
  echo "[bootstrap] installing browser requirements + Playwright Chromium"
  .venv/bin/python -m pip install -r requirements-browser.txt
  .venv/bin/python -m playwright install chromium
else
  echo "[bootstrap] skip browser install (set INSTALL_PLAYWRIGHT=1 to enable)"
fi

if [[ "${INSTALL_DEV:-0}" == "1" ]]; then
  echo "[bootstrap] installing dev requirements (pytest/ruff)"
  .venv/bin/python -m pip install -r requirements-dev.txt
else
  echo "[bootstrap] skip dev install (set INSTALL_DEV=1 to enable)"
fi

echo "[bootstrap] done"
echo "[bootstrap] activate with: source .venv/bin/activate"
