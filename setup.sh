#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but was not found in PATH." >&2
  exit 1
fi

if [ ! -f "$REQUIREMENTS_FILE" ]; then
  echo "requirements.txt was not found at $REQUIREMENTS_FILE." >&2
  exit 1
fi

echo "Creating virtual environment at $VENV_DIR"
python3 -m venv "$VENV_DIR"

echo "Upgrading pip tooling"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

echo "Installing requirements"
"$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS_FILE"

echo "Setup complete. Activate with: source .venv/bin/activate"
