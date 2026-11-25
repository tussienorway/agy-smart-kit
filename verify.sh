#!/usr/bin/env bash
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG_DIR="${AGY_PIPELINE_CFG_DIR:-$HOME/.gemini/config}"
CACHE_DIR="${AGY_PIPELINE_CACHE_DIR:-$HOME/.cache/agy-pipeline}"
SKILL_DIR="$CFG_DIR/skills/auto-capability-and-prompt-pipeline"

if [ "${AGY_VERIFY_INSTALLED:-0}" = "1" ] || ([ -f "$SKILL_DIR/scripts/pipeline.py" ] && grep -q 'VERSION = "2\.' "$SKILL_DIR/scripts/pipeline.py" 2>/dev/null); then
  SCRIPT_DIR="$SKILL_DIR/scripts"
  CHECK_HOOKS=1
else
  echo "INFO: Verifiserer skript direkte fra $KIT_DIR"
  SCRIPT_DIR="$KIT_DIR/scripts"
  CHECK_HOOKS=0
fi

echo "[1/6] Python-syntaks"
python3 -m py_compile "$SCRIPT_DIR/pipeline.py" "$SCRIPT_DIR/pre_invocation_hook.py"

echo "[2/6] Bash-syntaks"
bash -n "$SCRIPT_DIR/prepare_task.sh" "$KIT_DIR/install.sh" "$KIT_DIR/uninstall.sh" "$KIT_DIR/verify.sh"

echo "[3/6] JSON-validering"
python3 -m json.tool "$KIT_DIR/config/pipeline.json" >/dev/null
python3 -m json.tool "$KIT_DIR/config/hooks.example.json" >/dev/null
if [ "$CHECK_HOOKS" -eq 1 ] && [ -f "$CFG_DIR/hooks.json" ]; then
  python3 -m json.tool "$CFG_DIR/hooks.json" >/dev/null
fi

echo "[4/6] Triviell melding og bare slash-kommando hoppes over (AGY-payload)"
OUT1="$(echo '{"lastUserInput":"hei","conversationId":"verify"}' | AGY_PIPELINE_CFG_DIR="$CFG_DIR" AGY_PIPELINE_CACHE_DIR="$CACHE_DIR" python3 "$SCRIPT_DIR/pre_invocation_hook.py")"
[ "$OUT1" = "{}" ] || { echo "FAIL: forventet {}, fikk: $OUT1"; exit 1; }
OUT_SLASH="$(echo '{"lastUserInput":"/help","conversationId":"verify"}' | AGY_PIPELINE_CFG_DIR="$CFG_DIR" AGY_PIPELINE_CACHE_DIR="$CACHE_DIR" python3 "$SCRIPT_DIR/pre_invocation_hook.py")"
[ "$OUT_SLASH" = "{}" ] || { echo "FAIL: bare slash-kommando ble ikke hoppet over: $OUT_SLASH"; exit 1; }

echo "[5/6] Rekursjonsvakt (AGY_PIPELINE_ACTIVE=1)"
OUT2="$(echo '{"lastUserInput":"lag en komplett python-app med tester","conversationId":"verify"}' | AGY_PIPELINE_ACTIVE=1 AGY_PIPELINE_CFG_DIR="$CFG_DIR" AGY_PIPELINE_CACHE_DIR="$CACHE_DIR" python3 "$SCRIPT_DIR/pre_invocation_hook.py")"
[ "$OUT2" = "{}" ] || { echo "FAIL: rekursjonsvakt feilet: $OUT2"; exit 1; }

echo "[6/6] Enhetstester"
if [ "${AGY_SKIP_UNITTESTS:-0}" = "1" ]; then
  echo "SKIP: AGY_SKIP_UNITTESTS=1"
elif [ -f "$KIT_DIR/tests/test_pipeline.py" ]; then
  python3 -m unittest discover -s "$KIT_DIR/tests" -p "test_*.py" -v
else
  echo "SKIP: tests/test_pipeline.py ikke funnet"
fi

if [ "${1:-}" = "--live" ]; then
  echo "[live] Full ende-til-ende-kjøring via agy (bruker litt modellkvote):"
  AGY_PIPELINE_CFG_DIR="$CFG_DIR" AGY_PIPELINE_CACHE_DIR="$CACHE_DIR" "$SCRIPT_DIR/prepare_task.sh" "Lag et lite Python-script som leser en CSV-fil og skriver ut kolonnenavnene"
fi

echo "VERIFY_OK"
