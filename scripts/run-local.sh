#!/usr/bin/env bash
# Run the emulator detached so it survives the launching shell exiting.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PID_FILE="${SIGELLY_PID_FILE:-/tmp/sigelly_emu.pid}"
LOG_FILE="${SIGELLY_LOG_FILE:-/tmp/sigelly_emu.log}"
HOST="${SIGELLY_HOST:-0.0.0.0}"
PORT="${HTTP_PORT:-8080}"

if [[ -f "$PID_FILE" ]]; then
  old="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "already running pid=$old (stop with: kill $old)"
    exit 0
  fi
fi

: >"$LOG_FILE"
export SIGELLY_ROOT="$ROOT" SIGELLY_PID_FILE="$PID_FILE" SIGELLY_LOG_FILE="$LOG_FILE" SIGELLY_HOST="$HOST" HTTP_PORT="$PORT"
# New process group — required when launched from IDE agent shells that reap children.
"$ROOT/.venv/bin/python" - <<'PY'
import os
import subprocess
from pathlib import Path

root = Path(os.environ["SIGELLY_ROOT"])
pid_file = Path(os.environ["SIGELLY_PID_FILE"])
log_file = Path(os.environ["SIGELLY_LOG_FILE"])
host = os.environ["SIGELLY_HOST"]
port = os.environ["HTTP_PORT"]

proc = subprocess.Popen(
    [
        str(root / ".venv/bin/uvicorn"),
        "app.main:app",
        "--host",
        host,
        "--port",
        port,
        "--no-server-header",
    ],
    cwd=str(root),
    stdin=subprocess.DEVNULL,
    stdout=log_file.open("a"),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
pid_file.write_text(str(proc.pid))
print(f"started pid={proc.pid}  http://127.0.0.1:{port}/debug  log={log_file}")
PY
