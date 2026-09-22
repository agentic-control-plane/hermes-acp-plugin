#!/bin/sh
# Real-sandbox reproduction of "Hermes ran a command, ACP only saw the post-call".
# Throwaway HOME, local stub gateway, no real credentials, no LLM.
# Usage: ./run.sh            (installs hermes-agent from PyPI + this repo's plugin into ./venv)
set -u
S=$(cd "$(dirname "$0")" && pwd)
export HOME="$S/home"
mkdir -p "$HOME"
PORT=18477
LOG="$S/requests.log"
: > "$LOG"

cd "$S" || exit 1
# Python 3.14's venv fails on ensurepip on this machine; prefer 3.13/3.12.
PY=$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)
echo "--- using $PY ($($PY --version 2>&1))"
[ -d venv ] || "$PY" -m venv venv || { echo "venv creation failed"; exit 1; }
. venv/bin/activate
pip install -q --upgrade pip >/dev/null 2>&1
# HERMES_SOURCE=pypi (default, 0.19.x) or git (NousResearch main, 0.21+).
HERMES_SPEC="hermes-agent"
[ "${HERMES_SOURCE:-pypi}" = "git" ] && HERMES_SPEC="git+https://github.com/NousResearch/hermes-agent.git"
echo "--- installing hermes-agent ($HERMES_SPEC) and the plugin from $(dirname "$S")"
pip install -q "$HERMES_SPEC" 2>&1 | tail -2
pip install -q "$(dirname "$S")" 2>&1 | tail -2
python -c "import importlib.metadata as m; print('hermes-agent', m.version('hermes-agent'), '| acp-hermes', m.version('acp-hermes'))"

python "$S/stub_gateway.py" $PORT "$LOG" &
STUB=$!
sleep 1

export ACP_API_BASE="http://127.0.0.1:$PORT"
export ACP_BEARER_TOKEN="sandbox-token"
export ACP_LOCAL_METERING=off
export HERMES_LOG_LEVEL=DEBUG

echo "--- enabling the acp plugin in the sandbox HOME (external plugins are opt-in)"
hermes plugins enable acp 2>&1 | tail -5
echo "--- hermes plugins list (acp row only)"
hermes plugins list 2>&1 | grep -i -A1 "acp" | head -6

echo "--- probe: one terminal call through Hermes's dispatch path"
python "$S/probe.py" 2> "$S/probe.stderr"

echo "--- hook-related lines from debug log (probe.stderr)"
grep -i "pre_tool_call\|post_tool_call\|hook error\|acp_hermes\|name=acp" "$S/probe.stderr" | head -30

echo "--- probe2: registry + direct hook invocation"
python "$S/probe2.py" 2> "$S/probe2.stderr"
grep -i "error\|traceback\|acp_hermes" "$S/probe2.stderr" | head -20

echo "--- probe3: the executor's real entry points (resolve_pre_tool_block + _emit_post_tool_call_hook)"
: > "$LOG"
python "$S/probe3.py" 2> "$S/probe3.stderr"
grep -i "hook error\|traceback\|acp_hermes\|unreachable" "$S/probe3.stderr" | head -20
echo "--- requests from probe3 only"
python - "$LOG" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
print("NONE" if not rows else "")
for d in rows:
    b = d["body"] if isinstance(d["body"], dict) else {}
    print(d["method"], d["path"], "tool=", b.get("tool_name"), "hook=", b.get("hook_event_name"))
EOF

echo "--- requests that reached the stub gateway"
python - "$LOG" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
if not rows:
    print("NONE")
for d in rows:
    b = d["body"] if isinstance(d["body"], dict) else {}
    print(d["method"], d["path"], "client=", d["client"], "tool=", b.get("tool_name"), "hook=", b.get("hook_event_name"))
EOF

kill $STUB 2>/dev/null
echo "--- done. Full debug log: $S/probe.stderr"
