#!/usr/bin/env python3
"""agy-smart-kit v2.1.0 — Capability discovery + two-stage prompt refinement pipeline.

Discovers installed Antigravity CLI (agy) skills, plugins, and MCP servers,
then refines non-trivial prompts through Stage 1 (Gemini) -> Stage 2 (Claude,
with Gemini Neon critic fallback on 429/quota, and local keyword ranker if offline).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "2.1.0"

DEFAULT_CFG = {
    "mode": "auto",
    "hook_group": "auto-capability-and-prompt-pipeline",
    "hook_event": "PreInvocation",
    "hook_output_format": "agy_inject_steps",
    "hook_context_key": "additionalContext",
    "gemini_models": [
        "gemini-4-neon-high",
        "gemini-4-neon-medium",
        "gemini-4-hydrogen-medium",
        "gemini-3.8-flash-medium",
    ],
    "claude_models": [
        "claude-sonnet-5-5-high",
        "claude-opus-5-5-high",
        "claude-sonnet-5-5-medium",
    ],
    "critic_model": "gemini-4-neon-high",
    "stage_timeout_sec": 60,
    "total_budget_sec": 150,
    "max_inject_bytes": 6144,
    "triage_word_limit": 8,
    "catalog_cache_sec": 900,
    "refined_cache_sec": 1800,
    "circuit_breaker_fails": 3,
    "ignore_skills": [
        "auto-capability-and-prompt-pipeline",
        "swarm_monitor",
        "swarm_narrator",
        "swarm_pane",
    ],
    "max_catalog_chars": 8000,
}

SWARM_INTERNAL_SKILLS = {"swarm_monitor", "swarm_narrator", "swarm_pane"}
SELF_SKILL = "auto-capability-and-prompt-pipeline"

STOPWORDS = set(
    (
        "the a an and or to of in on for with is are was were be how what why "
        "kan du jeg og en et det som til for med hvordan hva lag fiks skriv vis meg "
        "please na nå this that den de vi fra skal vil har ikke bare"
    ).split()
)

CODEY_RE = re.compile(
    r"(```|error|fail|traceback|exception|crash|krasj|https?://|/[\w.\-]+/|"
    r"\w+\.\w{1,4}\b|--\w+|^/[a-zA-Z0-9_.:-]+\s+\S+|"
    r"\bbug\b|\bfix\b|\bfeil\b|\blag\b|\bfiks\b|\bkod\b|\bkode\b|\bbygg\b|"
    r"\bskriv\b|\bsjekk\b|\btest\b|\bdeploy\b|\bpubliser\b|\binstaller\b|"
    r"\bavinstaller\b|\bkonfigurer\b|\boppdater\b|\bwrite\b|\bbuild\b|"
    r"\bcreate\b|\bimplement\b|\brefactor\b|\bdebug\b|\baudit\b|\breview\b|"
    r"\binstall\b|\bsetup\b|\bgit\b|\bdocker\b|\bsql\b|\bapi\b|\bscript\b|"
    r"\bskript\b|\brepo\b|\bhook\b|\bskill\b|\bmcp\b|\bplugin\b|\bserver\b)",
    re.IGNORECASE,
)

TRIVIAL_WORDS = (
    "hei",
    "hallo",
    "hello",
    "hi",
    "hey",
    "halla",
    "takk",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "ja",
    "nei",
    "yes",
    "no",
    "yep",
    "nope",
    "fortsett",
    "continue",
    "nice",
    "kult",
    "supert",
    "flott",
    "bra",
    "lgtm",
)

TRIVIAL_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(re.escape(w) for w in TRIVIAL_WORDS) + r")(?:\b|$)",
    re.IGNORECASE,
)

BARE_SLASH_RE = re.compile(r"^/[a-zA-Z0-9_.:-]+\s*$")

PLACEHOLDER_NAMES = {"none", "(none)", "ingen", "n/a", "na", "-", "nothing"}


def get_cfg_dir() -> Path:
    override = os.environ.get("AGY_PIPELINE_CFG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "config"


def get_cache_dir() -> Path:
    override = os.environ.get("AGY_PIPELINE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "agy-pipeline"


def ensure_cache_dir() -> Path:
    cache = get_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cache, 0o700)
    except OSError:
        pass
    return cache


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    cfg["gemini_models"] = list(DEFAULT_CFG["gemini_models"])
    cfg["claude_models"] = list(DEFAULT_CFG["claude_models"])
    cfg["ignore_skills"] = list(DEFAULT_CFG["ignore_skills"])

    path = get_cfg_dir() / "pipeline.json"
    try:
        if path.exists():
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULT_CFG})
    except Exception:
        pass

    if os.environ.get("AGY_PIPELINE_MODE"):
        cfg["mode"] = os.environ["AGY_PIPELINE_MODE"]
    if os.environ.get("AGY_GEMINI_MODEL"):
        m = os.environ["AGY_GEMINI_MODEL"].strip()
        if m:
            cfg["gemini_models"] = [m] + [x for x in cfg["gemini_models"] if x != m]
    if os.environ.get("AGY_CLAUDE_MODEL"):
        m = os.environ["AGY_CLAUDE_MODEL"].strip()
        if m:
            cfg["claude_models"] = [m] + [x for x in cfg["claude_models"] if x != m]
    if os.environ.get("AGY_MAX_BYTES"):
        try:
            cfg["max_inject_bytes"] = max(512, int(os.environ["AGY_MAX_BYTES"]))
        except ValueError:
            pass
    return cfg


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def log_event(event: dict) -> None:
    try:
        cache = ensure_cache_dir()
        log_file = cache / "pipeline.log"
        lock_file = cache / ".lock"
        ev = dict(event)
        ev["ts"] = int(time.time())
        with lock_file.open("a", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                if log_file.exists() and log_file.stat().st_size > 1_000_000:
                    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
                    _atomic_write_text(log_file, "\n".join(lines) + "\n")
                with log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception:
        pass


def is_trivial(prompt: str, cfg: dict) -> bool:
    p = (prompt or "").strip()
    if not p:
        return True
    mode = cfg.get("mode", "auto")
    if mode == "off":
        return True
    if BARE_SLASH_RE.match(p):
        return True
    low = p.lower().strip("!?. ")
    if low in TRIVIAL_WORDS:
        return True
    if mode == "always":
        return False
    if len(low) < 24 and TRIVIAL_PREFIX_RE.match(low) and not CODEY_RE.search(p):
        return True
    if mode == "auto":
        if len(p.split()) <= int(cfg.get("triage_word_limit", 8)) and not CODEY_RE.search(p):
            return True
    return False


def _parse_skill_md(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except Exception:
        return None
    name = path.parent.name
    desc = ""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    fm = m.group(1) if m else ""
    if fm:
        nm = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
        if nm:
            name = nm.group(1).strip().strip("\"'")
        dm = re.search(r"^description:\s*(.+?)(?=\n[a-zA-Z0-9_-]+:|\Z)", fm, re.MULTILINE | re.DOTALL)
        if dm:
            raw_desc = dm.group(1)
            lines = []
            for idx, line in enumerate(raw_desc.splitlines()):
                cleaned = line.strip()
                if idx == 0 and cleaned in (">", ">-", "|", "|-"):
                    continue
                if cleaned:
                    lines.append(cleaned)
            desc = " ".join(lines).strip().strip("\"'")
    if not desc:
        body = text[m.end() :] if m else text
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith("---") and ":" not in line[:12]:
                desc = line
                break
    return name, desc[:220]


def _find_git_root(cwd_path: Path):
    cur = cwd_path.resolve()
    for parent in (cur, *cur.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _workspace_ancestors(cwd: str | None) -> list[Path]:
    try:
        cur = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
    except Exception:
        cur = Path.cwd().resolve()
    git_root = _find_git_root(cur)
    ancestors = [cur]
    if git_root and git_root != cur:
        for p in cur.parents:
            ancestors.append(p)
            if p == git_root:
                break
    return ancestors


def _parse_plugin_list(stdout: str) -> list[dict]:
    plugins = []
    seen = set()
    for raw_line in (stdout or "").splitlines():
        if not raw_line.strip():
            continue
        # Only top-level bullet entries (e.g. "- superpowers (enabled, 7.0.2)")
        stripped = raw_line.lstrip()
        indent = len(raw_line) - len(stripped)
        if indent >= 2:
            continue
        if stripped.startswith(("- ", "* ")):
            body = stripped[2:].strip()
            if not body:
                continue
            name = body.split()[0].rstrip(":")
            if name and name not in seen and name.lower() not in ("installed", "name", "plugin", "no"):
                seen.add(name)
                plugins.append({"kind": "plugin", "name": name, "path": "", "desc": body[:160]})
    return plugins


def _parse_mcp_list(stdout: str) -> set[str]:
    mcps = set()
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("no mcp", "no server", "name", "server", "---", "configured mcp")):
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        if not line:
            continue
        token = line.split()[0].rstrip(":")
        if token and token.lower() not in ("no", "none", "name", "server"):
            mcps.add(token)
    return mcps


def discover(cfg: dict, cwd: str | None = None, include_swarm: bool = False) -> list[dict]:
    ensure_cache_dir()
    cfg_dir = get_cfg_dir()
    home = Path.home()
    entries: list[dict] = []

    roots: list[tuple[Path, str]] = [
        (cfg_dir / "skills", "*/SKILL.md"),
        (home / ".agents" / "skills", "*/SKILL.md"),
        (cfg_dir / "plugins", "*/skills/*/SKILL.md"),
        (home / ".gemini" / "antigravity-cli" / "builtin" / "skills", "*/SKILL.md"),
    ]
    for anc in _workspace_ancestors(cwd):
        roots.append((anc / ".agents" / "skills", "*/SKILL.md"))
        roots.append((anc / ".agent" / "skills", "*/SKILL.md"))
        roots.append((anc / ".gemini" / "skills", "*/SKILL.md"))

    ignore = set(cfg.get("ignore_skills", []))
    if include_swarm:
        ignore -= SWARM_INTERNAL_SKILLS
    ignore.add(SELF_SKILL)

    seen_real = set()
    seen_names = set()
    for root, pattern in roots:
        try:
            if not root.exists():
                continue
            for sf in sorted(root.glob(pattern)):
                real = os.path.realpath(sf)
                if real in seen_real:
                    continue
                seen_real.add(real)
                parsed = _parse_skill_md(sf)
                if not parsed:
                    continue
                name, desc = parsed
                if name in ignore or SELF_SKILL in str(sf) or name in seen_names:
                    continue
                seen_names.add(name)
                entries.append({"kind": "skill", "name": name, "path": str(sf), "desc": desc})
        except Exception:
            continue

    plugins_found: list[dict] = []
    try:
        r = subprocess.run(
            ["agy", "plugin", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "AGY_PIPELINE_ACTIVE": "1"},
        )
        if r.returncode == 0 and r.stdout:
            plugins_found = _parse_plugin_list(r.stdout)
    except Exception:
        pass
    if not plugins_found:
        plugins_dir = cfg_dir / "plugins"
        if plugins_dir.is_dir():
            for p in sorted(plugins_dir.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    plugins_found.append({"kind": "plugin", "name": p.name, "path": "", "desc": f"Installed plugin: {p.name}"})
    entries.extend(plugins_found)

    mcps: set[str] = set()
    mcp_paths = [cfg_dir / "mcp_config.json"]
    if cwd:
        mcp_paths.append(Path(cwd).expanduser().resolve() / ".agents" / "mcp_config.json")
    for mcp_cfg in mcp_paths:
        try:
            if mcp_cfg.is_file() and mcp_cfg.stat().st_size > 2:
                data = json.loads(mcp_cfg.read_text(encoding="utf-8-sig"))
                servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
                if isinstance(servers, dict):
                    mcps.update(k for k in servers.keys() if isinstance(k, str) and k.strip())
        except Exception:
            pass
    try:
        r = subprocess.run(
            ["agy", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "AGY_PIPELINE_ACTIVE": "1"},
        )
        if r.returncode == 0 and r.stdout:
            mcps.update(_parse_mcp_list(r.stdout))
    except Exception:
        pass
    for name in sorted(mcps):
        entries.append({"kind": "mcp", "name": name, "path": "", "desc": "MCP server"})

    return entries


def get_catalog(cfg: dict, cwd: str | None = None, include_swarm: bool = False) -> list[dict]:
    cache = ensure_cache_dir()
    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else str(Path.cwd().resolve())
    key = sha(f"{resolved_cwd}|swarm={int(include_swarm)}")[:12]
    cache_file = cache / f"catalog-{key}.json"
    ttl = int(cfg.get("catalog_cache_sec", 900))
    try:
        if ttl > 0 and cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data["entries"]
    except Exception:
        pass
    entries = discover(cfg, cwd=resolved_cwd, include_swarm=include_swarm)
    try:
        _atomic_write_text(cache_file, json.dumps({"cwd": resolved_cwd, "entries": entries}, ensure_ascii=False))
    except Exception:
        pass
    return entries


def catalog_text(entries: list[dict], cfg: dict) -> str:
    lines = []
    total = 0
    limit = int(cfg.get("max_catalog_chars", 8000))
    for e in entries:
        if e.get("path"):
            line = f"{e['kind']} | {e['name']} | {e['path']} | {(e.get('desc') or '')[:120]}"
        else:
            line = f"{e['kind']} | {e['name']} | {(e.get('desc') or '')[:120]}"
        if total + len(line) > limit:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _is_quota_error(blob: str) -> bool:
    up = (blob or "").upper()
    return any(
        marker in up
        for marker in (
            "RESOURCE_EXHAUSTED",
            "INDIVIDUAL QUOTA REACHED",
            "TOO MANY REQUESTS",
            "CODE 429",
            "HTTP 429",
            "STATUS 429",
            "QUOTA EXCEEDED",
            "RATE LIMIT",
        )
    )


def run_agy(model: str, prompt_text: str, timeout: int):
    env = dict(os.environ)
    env["AGY_PIPELINE_ACTIVE"] = "1"
    timeout_s = max(10, int(timeout))
    cmd = [
        "agy",
        "--model",
        model,
        "--disable-slash-commands",
        "--print-timeout",
        f"{timeout_s}s",
        "-p",
        prompt_text,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 15, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as exc:
        return None, "error:" + str(exc)[:120]

    out = (r.stdout or "").strip()
    err = r.stderr or ""
    blob = out + "\n" + err
    if r.returncode != 0:
        if _is_quota_error(blob):
            return None, "quota"
        return None, "error:" + (err.strip() or out)[:200]
    if not out:
        return None, "empty"
    return out, "ok"


def build_stage1_prompt(raw_prompt: str, cat_text: str, cwd: str | None = None) -> str:
    cwd_line = f"Working directory: {cwd}\n" if cwd else ""
    return (
        "You are a senior prompt engineer and capability router for the Antigravity CLI (agy) agent.\n"
        + cwd_line
        + "Available capabilities (skills, plugins, MCP servers):\n"
        + (cat_text or "(none found)")
        + "\n\n"
        "Rewrite the RAW USER REQUEST into a precise, actionable engineering specification for the executing agent.\n"
        "Rules:\n"
        "- Preserve the user's intent without adding fluff. The executing agent must answer the end user in the user's own language.\n"
        "- Select at most 5 skills that genuinely help; fewer is better. Use exact names and exact paths from the list above.\n"
        "- If no skills or tools apply, leave the section empty or write `- none`.\n"
        "- Output EXACTLY this structure and nothing else:\n\n"
        "### REFINED_PROMPT\n"
        "<goal, context, constraints, acceptance criteria, suggested first step>\n"
        "### MATCHED_SKILLS\n"
        "- <name> :: <full path> :: <one-line reason>\n"
        "### MATCHED_TOOLS\n"
        "- <plugin or mcp name> :: <one-line reason>\n"
        "### RISKS\n"
        "- <max 3 lines>\n\n"
        "RAW USER REQUEST:\n"
        + raw_prompt
    )


def build_stage2_prompt(raw_prompt: str, draft: str) -> str:
    return (
        "You are a strict principal engineer reviewing a DRAFT SPECIFICATION for an executing CLI agent.\n"
        "Improve it by removing ambiguity, missing constraints, wrong or excessive skill/tool matches, missing verification criteria, and over-engineering.\n"
        "Keep EXACTLY the same section structure (### REFINED_PROMPT / ### MATCHED_SKILLS / ### MATCHED_TOOLS / ### RISKS).\n"
        "Preserve exact skill file paths from the draft. Keep at most 5 skills, prefer fewer.\n"
        "The executing agent must answer the end user in the user's own language.\n\n"
        "ORIGINAL USER REQUEST:\n"
        + raw_prompt
        + "\n\nDRAFT SPECIFICATION:\n"
        + draft
    )


def parse_section(text: str, header: str) -> str:
    m = re.search(
        r"###\s*" + re.escape(header) + r"\s*\n(.*?)(?=\n###|\Z)",
        text or "",
        re.DOTALL | re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def parse_matches(text: str, entries: list[dict]) -> list[dict]:
    out = []
    seen_paths = set()
    by_path = {e["path"]: e for e in entries if e.get("path")}
    by_name = {e["name"]: e for e in entries if e.get("kind") == "skill" and e.get("path")}
    section = parse_section(text, "MATCHED_SKILLS")

    for line in section.splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or line.lower() in PLACEHOLDER_NAMES:
            continue
        parts = [p.strip().strip("`") for p in line.split("::")]
        e = None
        reason = ""
        if len(parts) >= 3:
            e = by_path.get(parts[1]) or by_name.get(parts[0])
            reason = parts[2]
        elif len(parts) == 2:
            e = by_path.get(parts[1]) or by_path.get(parts[0]) or by_name.get(parts[0])
            reason = parts[1] if e and parts[1] != e["path"] else ""
        elif len(parts) == 1:
            e = by_path.get(parts[0]) or by_name.get(parts[0])
        if e and e["path"] not in seen_paths:
            seen_paths.add(e["path"])
            out.append({"name": e["name"], "path": e["path"], "reason": reason})

    if not out and section:
        for name, e in by_name.items():
            if re.search(rf"\b{re.escape(name)}\b", section) and e["path"] not in seen_paths:
                seen_paths.add(e["path"])
                out.append({"name": e["name"], "path": e["path"], "reason": ""})

    return out[:5]


def parse_tools(text: str, entries: list[dict] | None = None) -> list[dict]:
    out = []
    seen = set()
    allowed = None
    if entries is not None:
        allowed = {e["name"].lower(): e["name"] for e in entries if e.get("kind") in ("plugin", "mcp")}
    for line in parse_section(text, "MATCHED_TOOLS").splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or line.lower() in PLACEHOLDER_NAMES:
            continue
        parts = [p.strip().strip("`") for p in line.split("::")]
        name = parts[0]
        if not name or name.lower() in PLACEHOLDER_NAMES:
            continue
        if allowed is not None:
            canonical = allowed.get(name.lower())
            if not canonical:
                continue
            name = canonical
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "reason": parts[1] if len(parts) > 1 else ""})
    return out[:5]


def rank_local(prompt: str, entries: list[dict], k: int = 5) -> list[dict]:
    toks = set(
        t
        for t in re.findall(r"[a-z0-9_\-æøåäöü]{3,}", (prompt or "").lower())
        if t not in STOPWORDS
    )
    if not toks:
        return []
    scored = []
    for e in entries:
        if e.get("kind") != "skill" or not e.get("path"):
            continue
        name_toks = set(re.findall(r"[a-z0-9_\-æøåäöü]{3,}", e["name"].lower()))
        desc_toks = set(re.findall(r"[a-z0-9_\-æøåäöü]{3,}", (e.get("desc") or "").lower()))
        name_hits = toks & name_toks
        desc_hits = (toks & desc_toks) - name_hits
        score = len(name_hits) * 3 + len(desc_hits)
        if score > 0:
            hits = sorted(name_hits | desc_hits)[:4]
            scored.append((score, e, hits))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [
        {"name": e["name"], "path": e["path"], "reason": "keyword: " + ", ".join(h)}
        for _, e, h in scored[:k]
    ]


def run_pipeline(raw_prompt: str, cfg: dict, cwd: str | None = None) -> dict:
    t0 = time.time()
    budget = int(cfg.get("total_budget_sec", 150))
    stage_to = int(cfg.get("stage_timeout_sec", 60))
    include_swarm = "swarm" in (raw_prompt or "").lower()
    entries = get_catalog(cfg, cwd=cwd, include_swarm=include_swarm)
    cat_text = catalog_text(entries, cfg)
    notes: list[str] = []

    def time_left() -> int:
        return max(0, budget - int(time.time() - t0))

    s1_text, s1_model = None, None
    for m in cfg["gemini_models"]:
        rem = time_left()
        if rem < 10:
            notes.append("budget-exhausted")
            break
        out, status = run_agy(m, build_stage1_prompt(raw_prompt, cat_text, cwd=cwd), min(stage_to, rem))
        if status == "ok":
            s1_text, s1_model = out, m
            break
        notes.append(f"s1:{m}:{status}")

    if s1_text is None:
        matches = rank_local(raw_prompt, entries)
        ms = int((time.time() - t0) * 1000)
        return {
            "refined": raw_prompt,
            "matches": matches,
            "tools": [],
            "risks": "",
            "models": "local-ranker",
            "notes": notes + ["all-stage1-failed"],
            "ms": ms,
            "skills_total": len(entries),
            "degraded": True,
        }

    s2_text, s2_model = None, None
    claude_blocked = False
    for m in cfg["claude_models"]:
        rem = time_left()
        if rem < 10:
            break
        out, status = run_agy(m, build_stage2_prompt(raw_prompt, s1_text), min(stage_to, rem))
        if status == "ok":
            s2_text, s2_model = out, m
            break
        notes.append(f"s2:{m}:{status}")
        if status == "quota":
            claude_blocked = True
            break

    if s2_text is None:
        critic_chain = []
        cm = cfg.get("critic_model", "gemini-4-neon-high")
        if cm:
            critic_chain.append(cm)
        for gm in cfg["gemini_models"]:
            if gm not in critic_chain:
                critic_chain.append(gm)
        for gm in critic_chain:
            rem = time_left()
            if rem < 10:
                break
            out, status = run_agy(gm, build_stage2_prompt(raw_prompt, s1_text), min(stage_to, rem))
            if status == "ok":
                s2_text, s2_model = out, f"{gm} (critic)"
                break
            notes.append(f"critic:{gm}:{status}")

    if claude_blocked:
        notes.append("claude-quota-exhausted")

    final = s2_text or s1_text
    refined = parse_section(final, "REFINED_PROMPT") or final
    matches = parse_matches(final, entries) or parse_matches(s1_text, entries) or rank_local(raw_prompt, entries)
    tools = parse_tools(final, entries) or parse_tools(s1_text, entries)
    risks = parse_section(final, "RISKS")
    models = s1_model + (f" + {s2_model}" if s2_model else "")
    ms = int((time.time() - t0) * 1000)
    return {
        "refined": refined,
        "matches": matches,
        "tools": tools,
        "risks": risks,
        "models": models,
        "notes": notes,
        "ms": ms,
        "skills_total": len(entries),
        "degraded": False,
    }


def build_injection(result: dict, raw_prompt: str, cfg: dict) -> str:
    head = (
        f"[SMART-PIPELINE v2 | {result['models']} | {result['ms']} ms | "
        f"{result['skills_total']} capabilities]"
    )
    lines = [
        head,
        "",
        "REFINED TASK (authoritative rewrite of the user's request):",
        result["refined"][:2200],
        "",
    ]
    if result.get("matches"):
        lines.append("MATCHED SKILLS - read each SKILL.md file fully BEFORE acting:")
        for m in result["matches"]:
            lines.append("- " + m["path"] + (" - " + m["reason"] if m.get("reason") else ""))
        lines.append("")
    if result.get("tools"):
        lines.append("MATCHED TOOLS:")
        for t in result["tools"]:
            lines.append("- " + t["name"] + (" - " + t["reason"] if t.get("reason") else ""))
        lines.append("")
    if result.get("risks"):
        lines.append("RISKS:")
        lines.append(result["risks"][:400])
        lines.append("")
    lines.append("DIRECTIVES:")
    lines.append("- Answer the user in the user's own language.")
    lines.append("- Do not re-run prepare_task.sh; this block is already the pipeline output.")
    lines.append("- Before declaring done, apply the verification-before-completion checklist.")

    text = "\n".join(lines)
    data = text.encode("utf-8")
    limit = int(cfg.get("max_inject_bytes", 6144))
    if len(data) > limit:
        text = data[: max(128, limit - 40)].decode("utf-8", "ignore") + "\n...[trimmed to fit budget]"
    return text


def format_hook_output(injection: str, cfg: dict) -> dict:
    fmt = cfg.get("hook_output_format", "agy_inject_steps")
    if fmt == "agy_inject_steps":
        return {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": injection,
                    }
                }
            ]
        }
    key = cfg.get("hook_context_key", "additionalContext")
    return {key: injection}


def _json_read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def circuit_ok(cfg: dict, session: str) -> bool:
    c = _json_read(get_cache_dir() / "circuit.json", {})
    if c.get("session") != session:
        return True
    return int(c.get("fails", 0)) < int(cfg.get("circuit_breaker_fails", 3))


def circuit_report(session: str, ok: bool) -> None:
    cache = ensure_cache_dir()
    path = cache / "circuit.json"
    cur = _json_read(path, {})
    if cur.get("session") != session:
        fails = 0 if ok else 1
    else:
        fails = 0 if ok else int(cur.get("fails", 0)) + 1
    try:
        _atomic_write_text(path, json.dumps({"session": session, "fails": fails}))
    except Exception:
        pass


def extract_prompt(payload):
    if isinstance(payload, str):
        return payload, "default", None
    if not isinstance(payload, dict):
        return "", "default", None

    prompt = ""
    for key in ("lastUserInput", "prompt", "message", "user_message", "input", "text", "query"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            prompt = v
            break
        if isinstance(v, dict):
            for k2 in ("text", "content", "prompt"):
                if isinstance(v.get(k2), str) and v[k2].strip():
                    prompt = v[k2]
                    break
            if prompt:
                break

    session = "default"
    for key in ("conversationId", "session_id", "conversation_id", "chat_id", "session", "thread_id"):
        v = payload.get(key)
        if isinstance(v, (str, int)) and str(v).strip():
            session = str(v).strip()
            break

    cwd = None
    if isinstance(payload.get("cwd"), str) and payload["cwd"].strip():
        cwd = payload["cwd"].strip()
    elif isinstance(payload.get("workspacePaths"), list) and payload["workspacePaths"]:
        first = payload["workspacePaths"][0]
        if isinstance(first, str) and first.strip():
            cwd = first.strip()

    return prompt, session, cwd


def hook_entry() -> None:
    cfg = load_cfg()
    session = "default"
    try:
        if os.environ.get("AGY_PIPELINE_ACTIVE") == "1":
            print("{}")
            return

        raw = ""
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = raw

        prompt, session, cwd = extract_prompt(payload)
        if not prompt.strip():
            print("{}")
            return
        if cfg.get("mode", "auto") == "off":
            print("{}")
            return
        if is_trivial(prompt, cfg):
            log_event({"ev": "skip", "why": "trivial", "session": session})
            print("{}")
            return
        if not circuit_ok(cfg, session):
            log_event({"ev": "skip", "why": "circuit-open", "session": session})
            print("{}")
            return

        cache = ensure_cache_dir()
        ddir = cache / "dedup"
        ddir.mkdir(parents=True, exist_ok=True)
        ph = sha(prompt)
        dfile = ddir / (sha(session) + ".txt")
        if dfile.exists() and dfile.read_text(encoding="utf-8", errors="ignore").strip() == ph:
            print("{}")
            return

        # Write dedup marker BEFORE invoking models so tool-loop turns never re-trigger
        _atomic_write_text(dfile, ph)

        rc_dir = cache / "refined"
        rc_dir.mkdir(parents=True, exist_ok=True)
        rc_key = sha(f"{prompt}|cwd={cwd or ''}")
        rc_file = rc_dir / (rc_key + ".json")
        rc_ttl = int(cfg.get("refined_cache_sec", 1800))
        if rc_ttl > 0 and rc_file.exists() and time.time() - rc_file.stat().st_mtime < rc_ttl:
            cached = _json_read(rc_file, None)
            if cached and cached.get("injection"):
                log_event({"ev": "inject", "cached": True, "session": session, "ms": 0})
                print(json.dumps(format_hook_output(cached["injection"], cfg), ensure_ascii=False))
                return

        result = run_pipeline(prompt, cfg, cwd=cwd)
        injection = build_injection(result, prompt, cfg)

        if result.get("degraded"):
            circuit_report(session, ok=False)
        else:
            circuit_report(session, ok=True)
            try:
                _atomic_write_text(rc_file, json.dumps({"injection": injection}, ensure_ascii=False))
            except Exception:
                pass

        log_event(
            {
                "ev": "inject",
                "cached": False,
                "degraded": bool(result.get("degraded")),
                "session": session,
                "models": result["models"],
                "ms": result["ms"],
                "notes": result["notes"],
                "matches": [m["name"] for m in result["matches"]],
            }
        )
        print(json.dumps(format_hook_output(injection, cfg), ensure_ascii=False))
    except Exception as exc:
        circuit_report(session, ok=False)
        log_event({"ev": "error", "session": session, "err": str(exc)[:300]})
        print("{}")


def print_report(result: dict) -> None:
    print("=" * 70)
    print(f"agy smart pipeline v{VERSION}  ({result['ms']} ms, {result['skills_total']} capabilities)")
    print("models: " + result["models"])
    if result.get("notes"):
        print("notes:  " + "; ".join(result["notes"]))
    print("=" * 70)
    print("\nREFINED TASK:\n" + result["refined"])
    if result.get("matches"):
        print("\nMATCHED SKILLS:")
        for m in result["matches"]:
            print("  - " + m["path"] + ("  (" + m["reason"] + ")" if m.get("reason") else ""))
    if result.get("tools"):
        print("\nMATCHED TOOLS:")
        for t in result["tools"]:
            print("  - " + t["name"] + ("  (" + t["reason"] + ")" if t.get("reason") else ""))
    if result.get("risks"):
        print("\nRISKS:\n" + result["risks"])


def show_status() -> None:
    cache = get_cache_dir()
    log_file = cache / "pipeline.log"
    cfg = load_cfg()
    print(f"agy-smart-kit pipeline v{VERSION}")
    print(f"config:     {get_cfg_dir() / 'pipeline.json'} (mode={cfg['mode']}, format={cfg['hook_output_format']})")
    print(f"cache:      {cache}")
    circuit = _json_read(cache / "circuit.json", {})
    if circuit:
        print(f"circuit:    session={circuit.get('session', '-')}, fails={circuit.get('fails', 0)}/{cfg['circuit_breaker_fails']}")
    if not log_file.exists():
        print("log:        no events logged yet")
        return
    events = []
    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    inj = [e for e in events if e.get("ev") == "inject"]
    skips = [e for e in events if e.get("ev") == "skip"]
    errs = [e for e in events if e.get("ev") == "error"]
    degraded = [e for e in inj if e.get("degraded")]
    print(f"injections: {len(inj)} ({sum(1 for e in inj if e.get('cached'))} cached, {len(degraded)} degraded)")
    print(f"skips:      {len(skips)}")
    print(f"errors:     {len(errs)}")
    lat = [e.get("ms", 0) for e in inj if e.get("ms")]
    if lat:
        print(f"latency ms: avg {sum(lat) // len(lat)}, max {max(lat)}")
    print("\nlast events:")
    for e in events[-5:]:
        when = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
        print("  " + when + "  " + json.dumps(e, ensure_ascii=False)[:140])


def clear_cache() -> None:
    cache = get_cache_dir()
    if not cache.exists():
        print("cache already empty")
        return
    removed = 0
    for item in cache.iterdir():
        if item.name in ("pipeline.log", ".lock"):
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
        removed += 1
    print(f"cleared {removed} cache entries in {cache}")


def main() -> None:
    ap = argparse.ArgumentParser(description=f"agy smart pipeline v{VERSION}")
    ap.add_argument("--hook", action="store_true", help="Run as Antigravity PreInvocation hook (reads JSON from stdin)")
    ap.add_argument("--print", dest="print_mode", action="store_true", help="Run full pipeline and print human-readable report")
    ap.add_argument("--status", action="store_true", help="Show pipeline telemetry and configuration status")
    ap.add_argument("--discover", action="store_true", help="List all discovered skills, plugins, and MCP servers")
    ap.add_argument("--clear-cache", action="store_true", help="Clear cached catalog, refined prompts, dedup, and circuit state")
    ap.add_argument("--cwd", default=None, help="Workspace directory to scan for project skills")
    ap.add_argument("--version", action="version", version=f"agy-smart-kit {VERSION}")
    ap.add_argument("--prompt", nargs="*", default=[], help="Prompt text for --print mode")
    ap.add_argument("positional", nargs="*", help="Optional positional prompt text for --print mode")
    args = ap.parse_args()

    if args.clear_cache:
        clear_cache()
        return
    if args.status:
        show_status()
        return

    cfg = load_cfg()
    if args.discover:
        for e in get_catalog(cfg, cwd=args.cwd):
            print(f"{e['kind']} | {e['name']} | {e.get('path') or ''} | {(e.get('desc') or '')[:80]}")
        return
    if args.hook:
        hook_entry()
        return
    if args.print_mode:
        prompt = " ".join(args.prompt or args.positional).strip()
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            print("usage: pipeline.py --print -- <prompt text>", file=sys.stderr)
            sys.exit(1)
        result = run_pipeline(prompt, cfg, cwd=args.cwd)
        print_report(result)
        return

    show_status()
    print("\nTip: run with --discover, --print -- \"<oppgave>\", --clear-cache, or --help")


if __name__ == "__main__":
    main()
