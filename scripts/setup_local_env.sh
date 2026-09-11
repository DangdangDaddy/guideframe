#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"
venv_dir="$project_dir/.venv"

"$python_bin" -m venv "$venv_dir"
if [[ "${1:-}" == "--offline" ]]; then
  "$venv_dir/bin/python" -m pip install --no-index --find-links "$project_dir/wheelhouse" -r "$project_dir/requirements.txt"
else
  "$venv_dir/bin/python" -m pip install --upgrade pip
  "$venv_dir/bin/python" -m pip install --only-binary=:all: -r "$project_dir/requirements.txt"
fi

"$venv_dir/bin/python" -c 'import av, imageio_ffmpeg, numpy, PIL; print("media environment ready")'
