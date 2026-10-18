#!/usr/bin/env bash
# Daily habit-feedback pipeline (stats + digest + llm).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONIOENCODING=utf-8
python3 "$HERE/habit_feedback.py" run
