#!/usr/bin/env bash
set -euo pipefail

CFG_DIR="${AGY_PIPELINE_CFG_DIR:-$HOME/.gemini/config}"
SKILL_DIR="$CFG_DIR/skills/auto-capability-and-prompt-pipeline"
CACHE_DIR="${AGY_PIPELINE_CACHE_DIR:-$HOME/.cache/agy-pipeline}"
TS="$(date +%Y%m%d-%H%M%S)"
PURGE=0

if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

echo "== agy-smart-kit v2.1.0 uninstaller =="

if [ -f "$CFG_DIR/hooks.json" ]; then
  cp "$CFG_DIR/hooks.json" "$CFG_DIR/hooks.json.bak-$TS"
  python3 - "$CFG_DIR" <<'PYEOF'
import json
import sys
from pathlib import Path

cfg_dir = Path(sys.argv[1])
hj = cfg_dir / "hooks.json"
group = "auto-capability-and-prompt-pipeline"
pj = cfg_dir / "pipeline.json"
if pj.exists():
    try:
        pdata = json.loads(pj.read_text(encoding="utf-8-sig"))
        if isinstance(pdata, dict) and pdata.get("hook_group"):
            group = pdata["hook_group"]
    except Exception:
        pass

try:
    data = json.loads(hj.read_text(encoding="utf-8-sig"))
except Exception:
    data = {}

if isinstance(data, dict):
    removed = False
    if group in data:
        del data[group]
        removed = True
    # Also clean any stray entries referencing pre_invocation_hook.py inside other groups
    for gname in list(data.keys()):
        gval = data[gname]
        if isinstance(gval, dict):
            for ev in list(gval.keys()):
                if isinstance(gval[ev], list):
                    before = len(gval[ev])
                    gval[ev] = [
                        item for item in gval[ev]
                        if "pre_invocation_hook.py" not in json.dumps(item)
                    ]
                    if len(gval[ev]) != before:
                        removed = True
                    if not gval[ev]:
                        del gval[ev]
            if not gval:
                del data[gname]
    hj.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("hooks.json: fjernet smart-pipeline-hook" if removed else "hooks.json: ingen oppføring å fjerne")
PYEOF
fi

GEMINI_MD="$CFG_DIR/GEMINI.md"
if [ -f "$GEMINI_MD" ]; then
  cp "$GEMINI_MD" "$GEMINI_MD.bak-$TS"
  python3 - "$GEMINI_MD" <<'PYEOF'
import re
import sys
from pathlib import Path

target = Path(sys.argv[1])
content = target.read_text(encoding="utf-8")
pattern = re.compile(r"\n?<!--\s*smart-pipeline:v2:start\s*-->.*?<!--\s*smart-pipeline:v2:end\s*-->\n?", re.DOTALL)
updated, n = pattern.subn("\n", content)
if n:
    updated = re.sub(r"\n{3,}", "\n\n", updated).strip()
    target.write_text((updated + "\n") if updated else "", encoding="utf-8")
    print("GEMINI.md: fjernet smart-pipeline-blokk")
else:
    print("GEMINI.md: ingen smart-pipeline-blokk funnet")
PYEOF
fi

rm -rf "$SKILL_DIR" "$CACHE_DIR"
echo "Fjernet skill-mappe og cache."

if [ "$PURGE" -eq 1 ] && [ -f "$CFG_DIR/pipeline.json" ]; then
  rm -f "$CFG_DIR/pipeline.json"
  echo "Fjernet $CFG_DIR/pipeline.json (--purge)."
elif [ -f "$CFG_DIR/pipeline.json" ]; then
  echo "Beholdt $CFG_DIR/pipeline.json (kjør med --purge for å fjerne den også)."
fi

echo "UNINSTALL_OK"
