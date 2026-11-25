---
name: auto-capability-and-prompt-pipeline
description: Use when starting any non-trivial task (build, fix, debug, refactor, plan, research) where installed skills, plugins, or MCP servers could help. The PreInvocation hook normally runs this pipeline automatically before every non-trivial user message; use this skill manually when the hook is disabled, when AGY_PIPELINE_MODE=off, or when the pipeline was skipped.
---

# Auto Capability & Prompt Pipeline v2.1

## What it does
1. Discovers every installed capability: global skills (`~/.gemini/config/skills`, `~/.agents/skills`), plugin skills, builtin skills, workspace skills walking up to the git root, plus `agy plugin list` and MCP servers.
2. Rewrites the raw user request into a structured technical specification with Gemini (Stage 1).
3. Sharpens the spec with Claude (Stage 2). If Claude returns HTTP 429/quota, it stops immediately and runs a Gemini Neon critic pass instead, so refinement stays two-stage.
4. If all models fail or quota is exhausted, degrades gracefully to a local Unicode-aware keyword ranker and trips the per-session circuit breaker after consecutive failures.
5. Injects a compact `[SMART-PIPELINE v2]` block via Antigravity CLI's `PreInvocation` `injectSteps` protocol (refined task, exact paths of matched skills, matched tools, risks, and directives).

## Automatic vs manual
- **Automatic**: `~/.gemini/config/hooks.json` runs `scripts/pre_invocation_hook.py` on `PreInvocation`. Trivial messages (`hei`, `ok`) and bare slash commands (`/compact`, `/help`) are skipped locally with zero model calls. Slash commands with real tasks (e.g. `/boost ...`) still run normally. Identical repeated messages are deduplicated per session.
- **Manual fallback**: run `~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/prepare_task.sh "<the user's request>"` and treat the report as the task specification.
- If a `[SMART-PIPELINE v2]` block is already present in the conversation, do **not** run `prepare_task.sh` again.

## How to consume the injected block
1. Treat `REFINED TASK` as the authoritative version of the user's request.
2. Read every file listed under `MATCHED SKILLS` in full before acting — paths are validated against the discovered catalog.
3. Prefer listed plugins/MCP tools over ad-hoc solutions.
4. Answer the user in the user's own language.
5. Before declaring done, apply `verification-before-completion`.

## Red flags — stop and run the pipeline
| Thought | Reality |
|---|---|
| "This task is trivial, skip it" | Local triage already ran; if no block is present and the task touches code, run it |
| "I remember the skills, no scan needed" | Skills and plugins change; the workspace-scoped catalog cache is the source of truth |
| "Claude quota is out, skip Stage 2" | The pipeline automatically falls back to the Gemini Neon critic chain |
| "Running it twice is safer" | Duplicate runs waste quota; dedup exists — trust the existing block |

## Files & commands
- `scripts/pipeline.py` — engine (discovery, two-stage refinement, local ranker, cache, circuit breaker, JSONL telemetry)
- `scripts/pre_invocation_hook.py` — fail-open hook entrypoint (never breaks `agy`)
- `scripts/prepare_task.sh` — manual wrapper (handles quotes, flags, and multiline prompts safely)
- `~/.gemini/config/pipeline.json` — models, timeouts, budgets, hook format, mode
- Status: `python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pipeline.py --status`
- Discover: `python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pipeline.py --discover`
- Clear cache: `python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pipeline.py --clear-cache`
