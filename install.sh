#!/usr/bin/env bash
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG_DIR="${AGY_PIPELINE_CFG_DIR:-$HOME/.gemini/config}"
SKILL_DIR="$CFG_DIR/skills/auto-capability-and-prompt-pipeline"
CACHE_DIR="${AGY_PIPELINE_CACHE_DIR:-$HOME/.cache/agy-pipeline}"
TS="$(date +%Y%m%d-%H%M%S)"

echo "== agy-smart-kit v2.1.0 installer =="

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 mangler i PATH. Installer python3 og prøv igjen." >&2
  exit 1
fi

if ! command -v agy >/dev/null 2>&1; then
  echo "ADVARSEL: 'agy' ble ikke funnet i PATH. Installasjonen fullføres, men hooken krever Antigravity CLI for å kjøre."
fi

mkdir -p "$CFG_DIR" "$SKILL_DIR/scripts" "$CACHE_DIR"
chmod 700 "$CACHE_DIR"

cp "$KIT_DIR/SKILL.md" "$SKILL_DIR/SKILL.md"
cp "$KIT_DIR/scripts/pipeline.py" "$SKILL_DIR/scripts/pipeline.py"
cp "$KIT_DIR/scripts/pre_invocation_hook.py" "$SKILL_DIR/scripts/pre_invocation_hook.py"
cp "$KIT_DIR/scripts/prepare_task.sh" "$SKILL_DIR/scripts/prepare_task.sh"
chmod +x "$SKILL_DIR/scripts/pipeline.py" "$SKILL_DIR/scripts/pre_invocation_hook.py" "$SKILL_DIR/scripts/prepare_task.sh"

if [ -f "$CFG_DIR/pipeline.json" ]; then
  cp "$CFG_DIR/pipeline.json" "$CFG_DIR/pipeline.json.bak-$TS"
  echo "pipeline.json finnes allerede — beholdt dine innstillinger (backup: pipeline.json.bak-$TS)"
else
  cp "$KIT_DIR/config/pipeline.json" "$CFG_DIR/pipeline.json"
  echo "pipeline.json: opprettet standardkonfigurasjon"
fi

if [ -f "$CFG_DIR/hooks.json" ]; then
  cp "$CFG_DIR/hooks.json" "$CFG_DIR/hooks.json.bak-$TS"
fi

python3 - "$CFG_DIR" "$SKILL_DIR/scripts/pre_invocation_hook.py" <<'PYEOF'
import json
import shlex
import sys
from pathlib import Path

cfg_dir = Path(sys.argv[1])
hook_script = sys.argv[2]

group = "auto-capability-and-prompt-pipeline"
event = "PreInvocation"
pj = cfg_dir / "pipeline.json"
if pj.exists():
    try:
        pdata = json.loads(pj.read_text(encoding="utf-8-sig"))
        if isinstance(pdata, dict):
            group = pdata.get("hook_group") or group
            event = pdata.get("hook_event") or event
    except Exception:
        pass

hj = cfg_dir / "hooks.json"
cfg = {}
if hj.exists():
    try:
        loaded = json.loads(hj.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            cfg = loaded
    except Exception:
        cfg = {}

cmd = f"python3 {shlex.quote(hook_script)}"
entry = {
    "type": "command",
    "command": cmd,
    "timeout": 180,
}

group_obj = cfg.get(group)
if not isinstance(group_obj, dict):
    group_obj = {}
    cfg[group] = group_obj

event_list = group_obj.get(event)
if not isinstance(event_list, list):
    event_list = []
    group_obj[event] = event_list

replaced = False
for idx, existing in enumerate(event_list):
    if isinstance(existing, dict) and "pre_invocation_hook.py" in str(existing.get("command", "")):
        event_list[idx] = entry
        replaced = True
        break

if not replaced:
    event_list.append(entry)

hj.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
action = "oppdaterte" if replaced else "la til"
print(f"hooks.json: {action} {group}.{event}")
PYEOF

GEMINI_MD="$CFG_DIR/GEMINI.md"
if [ -f "$GEMINI_MD" ]; then
  cp "$GEMINI_MD" "$GEMINI_MD.bak-$TS"
else
  touch "$GEMINI_MD"
fi

python3 - "$GEMINI_MD" "$KIT_DIR/GEMINI.snippet.md" <<'PYEOF'
import re
import sys
from pathlib import Path

target = Path(sys.argv[1])
snippet = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
content = target.read_text(encoding="utf-8")

pattern = re.compile(r"<!--\s*smart-pipeline:v2:start\s*-->.*?<!--\s*smart-pipeline:v2:end\s*-->", re.DOTALL)
if pattern.search(content):
    updated = pattern.sub(snippet, content, count=1)
    print("GEMINI.md: oppdaterte eksisterende smart-pipeline-blokk")
else:
    sep = "" if not content else ("\n\n" if not content.endswith("\n") else "\n")
    updated = content + sep + snippet + "\n"
    print("GEMINI.md: la til smart-pipeline-blokk")

target.write_text(updated, encoding="utf-8")
PYEOF

rm -f "$CACHE_DIR"/catalog*.json

echo "== statiske og funksjonelle sjekker =="
AGY_VERIFY_INSTALLED=1 AGY_PIPELINE_CFG_DIR="$CFG_DIR" AGY_PIPELINE_CACHE_DIR="$CACHE_DIR" bash "$KIT_DIR/verify.sh"
echo "INSTALL_OK — start agy på nytt for å aktivere hooken"
