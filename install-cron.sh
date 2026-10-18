#!/usr/bin/env bash
# Install two daily cron jobs for introspect (22:15 feedback, 22:30 reflect).
# Idempotent: re-running replaces the previous introspect entries.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HERE/state"

FEEDBACK="15 22 * * * \"$HERE/run.sh\" >> \"$HERE/state/cron.log\" 2>&1"
REFLECT="30 22 * * * \"$HERE/reflect.sh\" >> \"$HERE/state/cron.log\" 2>&1"

# keep the existing crontab minus any previous introspect lines
EXISTING="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf '%s\n' "$EXISTING" | grep -vF "$HERE/run.sh" | grep -vF "$HERE/reflect.sh" || true)"

{ printf '%s\n' "$FILTERED"; printf '%s\n' "$FEEDBACK" "$REFLECT"; } | crontab -

echo "Installed cron jobs:"
crontab -l | grep -F "$HERE" || true
echo
echo "提示：cron 的 PATH 很精简。若 python3 不在 /usr/bin，"
echo "请把 run.sh / reflect.sh 里的 python3 改成绝对路径（command -v python3 查看）。"
