#!/usr/bin/env python3
"""Comprehensive dependency-free test suite for agy-smart-kit v2.1.0."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "scripts"))

import pipeline  # noqa: E402


class TestTriageAndExtraction(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(pipeline.DEFAULT_CFG)

    def test_trivial_greetings_and_bare_slash(self):
        for msg in ["", "   ", "hei", "Hei!", "ok", "takk", "/help", "/compact", "/hooks   "]:
            with self.subTest(msg=msg):
                self.assertTrue(pipeline.is_trivial(msg, self.cfg))

    def test_slash_command_with_task_is_not_trivial(self):
        self.assertFalse(pipeline.is_trivial("/boost gå over dette og lag en ferdig utgave", self.cfg))
        self.assertFalse(pipeline.is_trivial("/review fix auth bug in login.py", self.cfg))

    def test_prefix_word_boundary_bug_fixed(self):
        # Must NOT confuse 'hi' with 'history', 'no' with 'node', 'ok' with 'okta'
        self.assertFalse(pipeline.is_trivial("history of git commits", self.cfg))
        self.assertFalse(pipeline.is_trivial("node server crash", self.cfg))
        self.assertFalse(pipeline.is_trivial("okta login bug", self.cfg))
        # Multi-word non-codey prompt over limit starting with 'hi...'
        self.assertFalse(
            pipeline.is_trivial(
                "history of architectural decisions across all microservices in this workspace",
                self.cfg,
            )
        )
        # Actual short greeting phrase still skipped
        self.assertTrue(pipeline.is_trivial("hei der kompis", self.cfg))
        self.assertTrue(pipeline.is_trivial("takk skal du ha", self.cfg))

    def test_mode_always_and_off(self):
        always = {**self.cfg, "mode": "always"}
        self.assertFalse(pipeline.is_trivial("forklar kvantefysikk", always))
        self.assertTrue(pipeline.is_trivial("hei", always))
        self.assertTrue(pipeline.is_trivial("/compact", always))

        off = {**self.cfg, "mode": "off"}
        self.assertTrue(pipeline.is_trivial("implement full OAuth2 flow in server.py", off))

    def test_extract_prompt_agy_native_and_fallbacks(self):
        p, s, cwd = pipeline.extract_prompt(
            {
                "lastUserInput": "Lag en CLI i Python",
                "conversationId": "conv-123",
                "cwd": "/tmp/myproj",
            }
        )
        self.assertEqual(p, "Lag en CLI i Python")
        self.assertEqual(s, "conv-123")
        self.assertEqual(cwd, "/tmp/myproj")

        p2, s2, cwd2 = pipeline.extract_prompt(
            {
                "prompt": "Fix bug",
                "session_id": "sess-9",
                "workspacePaths": ["/tmp/ws1", "/tmp/ws2"],
            }
        )
        self.assertEqual((p2, s2, cwd2), ("Fix bug", "sess-9", "/tmp/ws1"))

        p3, s3, cwd3 = pipeline.extract_prompt({"message": {"text": "Nested text"}})
        self.assertEqual((p3, s3, cwd3), ("Nested text", "default", None))

        self.assertEqual(pipeline.extract_prompt("raw string"), ("raw string", "default", None))
        self.assertEqual(pipeline.extract_prompt(None), ("", "default", None))


class TestDiscoveryAndParsing(unittest.TestCase):
    def test_parse_multiline_yaml_skill_md(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "my-skill" / "SKILL.md"
            sp.parent.mkdir()
            sp.write_text(
                "---\n"
                "name: publish-site\n"
                "description: >-\n"
                "  Register a Wheel of Time alias and publish\n"
                "  documentation to GitHub Pages.\n"
                "---\n"
                "# Heading\n",
                encoding="utf-8",
            )
            name, desc = pipeline._parse_skill_md(sp)
            self.assertEqual(name, "publish-site")
            self.assertIn("Wheel of Time alias", desc)
            self.assertIn("GitHub Pages", desc)
            self.assertFalse(desc.startswith(">"))

    def test_parse_plugin_and_mcp_lists(self):
        plugin_stdout = (
            "Installed plugins:\n"
            "- maximum-absolute-mode (enabled, 3.0.0)\n"
            "    Components: 1 rule\n"
            "- superpowers (enabled, 7.0.2)\n"
            "    Source:     antigravity\n"
            "    Components: 15 skills\n"
        )
        plugins = pipeline._parse_plugin_list(plugin_stdout)
        names = [p["name"] for p in plugins]
        self.assertEqual(names, ["maximum-absolute-mode", "superpowers"])
        self.assertNotIn("Components", names)
        self.assertNotIn("Source", names)

        self.assertEqual(pipeline._parse_mcp_list("No MCP servers configured.\n"), set())
        self.assertEqual(
            pipeline._parse_mcp_list("Configured MCP servers:\n- github (enabled)\n- postgres (enabled)\n"),
            {"github", "postgres"},
        )

    def test_workspace_skill_discovery_and_cwd_cache_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_dir = root / "cfg"
            cache_dir = root / "cache"
            proj_a = root / "proj_a"
            sub_a = proj_a / "src" / "pkg"
            proj_b = root / "proj_b"

            (proj_a / ".git").mkdir(parents=True)
            sub_a.mkdir(parents=True)
            (proj_a / ".agents" / "skills" / "alpha-skill").mkdir(parents=True)
            (proj_a / ".agents" / "skills" / "alpha-skill" / "SKILL.md").write_text(
                "---\nname: alpha-skill\ndescription: Alpha workspace skill\n---\n",
                encoding="utf-8",
            )

            (proj_b / ".git").mkdir(parents=True)
            (proj_b / ".agent" / "skills" / "beta-skill").mkdir(parents=True)
            (proj_b / ".agent" / "skills" / "beta-skill" / "SKILL.md").write_text(
                "---\nname: beta-skill\ndescription: Beta workspace skill\n---\n",
                encoding="utf-8",
            )

            env = {
                "AGY_PIPELINE_CFG_DIR": str(cfg_dir),
                "AGY_PIPELINE_CACHE_DIR": str(cache_dir),
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                pipeline.subprocess, "run", side_effect=FileNotFoundError("no agy")
            ):
                cfg = pipeline.load_cfg()
                cat_a = pipeline.get_catalog(cfg, cwd=str(sub_a))
                cat_b = pipeline.get_catalog(cfg, cwd=str(proj_b))

            names_a = {e["name"] for e in cat_a if e["kind"] == "skill"}
            names_b = {e["name"] for e in cat_b if e["kind"] == "skill"}
            self.assertIn("alpha-skill", names_a)
            self.assertNotIn("beta-skill", names_a)
            self.assertIn("beta-skill", names_b)
            self.assertNotIn("alpha-skill", names_b)

    def test_parse_matches_tools_and_norwegian_rank_local(self):
        entries = [
            {
                "kind": "skill",
                "name": "søkemotor-fiks",
                "path": "/skills/sok/SKILL.md",
                "desc": "Håndterer pålogging og søk i norske databaser",
            },
            {
                "kind": "skill",
                "name": "tdd",
                "path": "/skills/tdd/SKILL.md",
                "desc": "Test-driven development workflow",
            },
        ]
        ranked = pipeline.rank_local("Fiks pålogging og søk i databasen", entries)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["name"], "søkemotor-fiks")

        raw = (
            "### REFINED_PROMPT\nDo the task.\n"
            "### MATCHED_SKILLS\n"
            "- tdd :: /skills/tdd/SKILL.md :: Write failing test first\n"
            "- fake-skill :: /does/not/exist/SKILL.md :: Hallucinated\n"
            "### MATCHED_TOOLS\n"
            "- none\n"
            "- superpowers :: Provides TDD skill\n"
            "### RISKS\n- None\n"
        )
        matches = pipeline.parse_matches(raw, entries)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["path"], "/skills/tdd/SKILL.md")

        tools = pipeline.parse_tools(raw)
        self.assertEqual(tools, [{"name": "superpowers", "reason": "Provides TDD skill"}])
        # With entries supplied, non-catalog tools (like 'python3') are filtered out
        raw_with_hallucinated = raw.replace(
            "- superpowers :: Provides TDD skill",
            "- python3 :: standard CLI\n- superpowers :: Provides TDD skill",
        )
        cat_with_plugin = entries + [{"kind": "plugin", "name": "superpowers", "path": "", "desc": "Plugin"}]
        self.assertEqual(
            pipeline.parse_tools(raw_with_hallucinated, cat_with_plugin),
            [{"name": "superpowers", "reason": "Provides TDD skill"}],
        )


class TestPipelineExecutionAndHook(unittest.TestCase):
    def test_run_agy_passes_go_duration_unit_and_detects_quota(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
            res = mock.MagicMock()
            res.returncode = 1
            res.stdout = ""
            res.stderr = "API error code 429: RESOURCE_EXHAUSTED"
            return res

        with mock.patch.object(pipeline.subprocess, "run", side_effect=fake_run):
            out, status = pipeline.run_agy("claude-sonnet-5-5-high", "hello", 45)

        self.assertIsNone(out)
        self.assertEqual(status, "quota")
        idx = captured["cmd"].index("--print-timeout")
        self.assertEqual(captured["cmd"][idx + 1], "45s")
        self.assertEqual(captured["env"]["AGY_PIPELINE_ACTIVE"], "1")

    def test_stage2_falls_back_to_critic_on_claude_quota(self):
        calls = []

        def fake_run_agy(model, prompt_text, timeout):
            calls.append(model)
            if model == "gemini-4-neon-high" and len(calls) == 1:
                return (
                    "### REFINED_PROMPT\nDraft spec\n### MATCHED_SKILLS\n- none\n### MATCHED_TOOLS\n- none\n### RISKS\n- low",
                    "ok",
                )
            if model.startswith("claude-"):
                return None, "quota"
            return (
                "### REFINED_PROMPT\nCritic refined spec\n### MATCHED_SKILLS\n- none\n### MATCHED_TOOLS\n- none\n### RISKS\n- low",
                "ok",
            )

        with tempfile.TemporaryDirectory() as td:
            env = {"AGY_PIPELINE_CFG_DIR": td, "AGY_PIPELINE_CACHE_DIR": td}
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                pipeline, "get_catalog", return_value=[]
            ), mock.patch.object(pipeline, "run_agy", side_effect=fake_run_agy):
                cfg = pipeline.load_cfg()
                res = pipeline.run_pipeline("Build a fast parser in Python", cfg)

        self.assertFalse(res["degraded"])
        self.assertEqual(res["refined"], "Critic refined spec")
        self.assertIn("critic", res["models"])
        self.assertIn("claude-quota-exhausted", res["notes"])
        # Stopped Claude chain after first quota hit
        self.assertEqual(calls, ["gemini-4-neon-high", "claude-sonnet-5-5-high", "gemini-4-neon-high"])

    def test_hook_inject_steps_dedup_cache_and_circuit_breaker(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td) / "cfg"
            cache_dir = Path(td) / "cache"
            cfg_dir.mkdir()
            (cfg_dir / "pipeline.json").write_text(
                json.dumps({"circuit_breaker_fails": 2}), encoding="utf-8"
            )
            env = {
                "AGY_PIPELINE_CFG_DIR": str(cfg_dir),
                "AGY_PIPELINE_CACHE_DIR": str(cache_dir),
            }
            os.environ.pop("AGY_PIPELINE_ACTIVE", None)

            good_result = {
                "refined": "Authoritative spec",
                "matches": [],
                "tools": [],
                "risks": "",
                "models": "gemini-4-neon-high + claude-sonnet-5-5-high",
                "notes": [],
                "ms": 120,
                "skills_total": 5,
                "degraded": False,
            }

            def invoke_hook(payload_dict):
                buf = io.StringIO()
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload_dict))), mock.patch.object(
                    sys, "stdout", buf
                ):
                    pipeline.hook_entry()
                return json.loads(buf.getvalue())

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                pipeline, "run_pipeline", return_value=good_result
            ) as mock_rp:
                # 1. First call returns AGY injectSteps
                out1 = invoke_hook({"lastUserInput": "Build a REST API in Python", "conversationId": "s1"})
                self.assertIn("injectSteps", out1)
                msg = out1["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[SMART-PIPELINE v2", msg)
                self.assertIn("Authoritative spec", msg)
                self.assertEqual(mock_rp.call_count, 1)

                # 2. Identical prompt in same session is deduplicated
                out2 = invoke_hook({"lastUserInput": "Build a REST API in Python", "conversationId": "s1"})
                self.assertEqual(out2, {})
                self.assertEqual(mock_rp.call_count, 1)

                # 3. Same prompt in a NEW session hits refined cache (no extra run_pipeline call)
                out3 = invoke_hook({"lastUserInput": "Build a REST API in Python", "conversationId": "s2"})
                self.assertIn("injectSteps", out3)
                self.assertEqual(mock_rp.call_count, 1)

            # 4. Degraded failures MUST trip circuit breaker and MUST NOT cache
            degraded_result = {**good_result, "models": "local-ranker", "degraded": True}
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                pipeline, "run_pipeline", return_value=degraded_result
            ) as mock_bad:
                o1 = invoke_hook({"lastUserInput": "Fix bug one in app.py", "conversationId": "s-fail"})
                self.assertIn("injectSteps", o1)
                o2 = invoke_hook({"lastUserInput": "Fix bug two in app.py", "conversationId": "s-fail"})
                self.assertIn("injectSteps", o2)
                # 3rd call in same session must be skipped because circuit_breaker_fails == 2
                o3 = invoke_hook({"lastUserInput": "Fix bug three in app.py", "conversationId": "s-fail"})
                self.assertEqual(o3, {})
                self.assertEqual(mock_bad.call_count, 2)

                # Degraded prompt must NOT have been cached for a fresh session
                o4 = invoke_hook({"lastUserInput": "Fix bug one in app.py", "conversationId": "s-fresh"})
                self.assertIn("injectSteps", o4)
                self.assertEqual(mock_bad.call_count, 3)

    def test_additional_context_format_option(self):
        cfg = {**pipeline.DEFAULT_CFG, "hook_output_format": "additional_context", "hook_context_key": "additionalContext"}
        out = pipeline.format_hook_output("hello block", cfg)
        self.assertEqual(out, {"additionalContext": "hello block"})


class TestInstallAndUninstallEndToEnd(unittest.TestCase):
    def test_install_preserves_existing_hooks_and_uninstall_cleans_up(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            cfg_dir = fake_home / ".gemini" / "config"
            cache_dir = fake_home / ".cache" / "agy-pipeline"
            cfg_dir.mkdir(parents=True)

            # Pre-existing unrelated hook and GEMINI.md without trailing newline
            (cfg_dir / "hooks.json").write_text(
                json.dumps({"other-plugin": {"PreToolUse": [{"type": "command", "command": "echo ok"}]}}),
                encoding="utf-8",
            )
            (cfg_dir / "GEMINI.md").write_text("# My existing rules", encoding="utf-8")

            env = {
                **os.environ,
                "HOME": str(fake_home),
                "AGY_PIPELINE_CFG_DIR": str(cfg_dir),
                "AGY_PIPELINE_CACHE_DIR": str(cache_dir),
                "AGY_SKIP_UNITTESTS": "1",
            }

            # Install
            r1 = subprocess.run(["bash", str(KIT_ROOT / "install.sh")], capture_output=True, text=True, env=env)
            self.assertEqual(r1.returncode, 0, f"install failed:\n{r1.stdout}\n{r1.stderr}")
            self.assertIn("INSTALL_OK", r1.stdout)

            # Verify hooks.json has BOTH other-plugin and auto-capability-and-prompt-pipeline
            hooks = json.loads((cfg_dir / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("other-plugin", hooks)
            self.assertIn("auto-capability-and-prompt-pipeline", hooks)
            self.assertIn("PreInvocation", hooks["auto-capability-and-prompt-pipeline"])
            self.assertNotIn("hooks", hooks)

            # Verify GEMINI.md has clean newline separation before marker
            gmd = (cfg_dir / "GEMINI.md").read_text(encoding="utf-8")
            self.assertIn("# My existing rules\n\n<!-- smart-pipeline:v2:start -->", gmd)

            # Re-install must be idempotent (no duplicate hook entries or duplicate GEMINI blocks)
            r2 = subprocess.run(["bash", str(KIT_ROOT / "install.sh")], capture_output=True, text=True, env=env)
            self.assertEqual(r2.returncode, 0, f"re-install failed:\n{r2.stdout}\n{r2.stderr}")
            hooks2 = json.loads((cfg_dir / "hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(len(hooks2["auto-capability-and-prompt-pipeline"]["PreInvocation"]), 1)
            gmd2 = (cfg_dir / "GEMINI.md").read_text(encoding="utf-8")
            self.assertEqual(gmd2.count("smart-pipeline:v2:start"), 1)

            # Uninstall (with --purge)
            r3 = subprocess.run(
                ["bash", str(KIT_ROOT / "uninstall.sh"), "--purge"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(r3.returncode, 0, f"uninstall failed:\n{r3.stdout}\n{r3.stderr}")
            self.assertIn("UNINSTALL_OK", r3.stdout)

            hooks3 = json.loads((cfg_dir / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("other-plugin", hooks3)
            self.assertNotIn("auto-capability-and-prompt-pipeline", hooks3)
            self.assertFalse((cfg_dir / "skills" / "auto-capability-and-prompt-pipeline").exists())
            self.assertFalse((cfg_dir / "pipeline.json").exists())
            gmd3 = (cfg_dir / "GEMINI.md").read_text(encoding="utf-8")
            self.assertEqual(gmd3.strip(), "# My existing rules")


if __name__ == "__main__":
    unittest.main()
