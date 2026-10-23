import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "local-bridge" / "scripts" / "local_bridge.py"
spec = importlib.util.spec_from_file_location("local_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def event(kind, **part):
    return json.dumps({"type": kind, "timestamp": 0, "sessionID": "ses_x", "part": part})


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "src").mkdir()
        (self.source / "src" / "good.py").write_text("print('safe')\n", encoding="utf-8")
        (self.source / "src" / ".env").write_text("SENTINEL_SECRET", encoding="utf-8")
        (self.source / "src" / "opencode.json").write_text("{}", encoding="utf-8")
        (self.source / ".opencode").mkdir()
        (self.source / ".opencode" / "agent.md").write_text("hidden", encoding="utf-8")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("hidden", encoding="utf-8")

    def test_stage_selected_directory_excludes_secrets_configs_and_tracks_changes(self):
        item = bridge.prepare(self.source, ["src"], "codex", self.base / "runs")
        run = Path(item["run_dir"])
        self.assertEqual(item["file_count"], 1)
        self.assertTrue((run / "workspace" / "src" / "good.py").exists())
        self.assertFalse((run / "workspace" / "src" / ".env").exists())
        self.assertFalse((run / "workspace" / "src" / "opencode.json").exists())
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["src/good.py"]["source_sha256"], bridge.hash_file(self.source / "src" / "good.py"))
        (run / "workspace" / "src" / "good.py").write_text("changed", encoding="utf-8")
        (run / "workspace" / "new.txt").write_text("new", encoding="utf-8")
        changes = bridge.inspect(run)["changes"]
        self.assertIn("src/good.py", changes["modified"])
        self.assertIn("new.txt", changes["created"])
        self.assertEqual((self.source / "src" / "good.py").read_text(encoding="utf-8"), "print('safe')\n")

    def test_rejects_traversal_sensitive_and_full_source_paths(self):
        for include in ("..\\outside", "../outside", "C:\\Windows", ".git/config", "src/.env", ".opencode", "."):
            with self.subTest(include=include), self.assertRaises(bridge.BridgeError):
                bridge.prepare(self.source, [include], "cc", self.base / "runs")
        with self.assertRaises(bridge.BridgeError):
            bridge.prepare(self.source, [], "cc", self.base / "runs")

    def test_lock_refuses_concurrent_run(self):
        item = bridge.prepare(self.source, ["src/good.py"], "codex", self.base / "runs")
        run = Path(item["run_dir"])
        (run / ".run.lock").write_text("held", encoding="utf-8")
        task = self.base / "task.txt"
        task.write_text("Do something", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "locked"):
            bridge.run_child(run, task, "routine", "read", [], 2, dict(bridge.DEFAULTS))

    def test_server_down_fails_preflight_without_launching(self):
        item = bridge.prepare(self.source, ["src/good.py"], "codex", self.base / "runs")
        run = Path(item["run_dir"])
        task = self.base / "task.txt"
        task.write_text("Summarise good.py", encoding="utf-8")
        config = dict(bridge.DEFAULTS, endpoint="http://127.0.0.1:9/v1", state_dir=str(self.base / "state"))
        with patch.object(bridge, "resolve_opencode", return_value="opencode.exe"), patch.object(bridge.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(bridge.BridgeError, "not answering"):
                bridge.run_child(run, task, "routine", "read", [], 30, config)
            popen.assert_not_called()
        status = json.loads((run / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "FAILED_PREFLIGHT")


class PermissionTests(unittest.TestCase):
    def test_read_mode_denies_edits_shell_network_and_outside_paths(self):
        perm = bridge.worker_agent("read", 40, [])["permission"]
        self.assertEqual(perm["edit"], "deny")
        self.assertEqual(perm["bash"], {"*": "deny"})
        for key in ("external_directory", "webfetch", "websearch", "task", "skill", "*"):
            self.assertEqual(perm[key], "deny", key)
        self.assertEqual(perm["read"], "allow")

    def test_write_mode_allows_only_exact_commands_after_default_deny(self):
        agent = bridge.worker_agent("write", 80, ["python -m pytest -q tests"])
        self.assertEqual(agent["mode"], "primary")  # `opencode run --agent` ignores subagent-mode agents
        self.assertEqual(agent["permission"]["edit"], "allow")
        bash = agent["permission"]["bash"]
        self.assertEqual(list(bash), ["*", "python -m pytest -q tests"])  # last matching rule wins
        for bad in ("pytest *", "python -c ?", "a\nb", "  "):
            with self.subTest(command=bad), self.assertRaises(bridge.BridgeError):
                bridge.worker_agent("write", 80, [bad])

    def test_child_env_isolates_opencode_config_and_working_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            config = dict(bridge.DEFAULTS, state_dir=tmp)
            workspace = Path(tmp) / "ws"
            with patch.dict(bridge.os.environ, {"OPENCODE_CONFIG": "x", "PWD": "/e/elsewhere"}):
                env = bridge.child_env(config, {"mode": "primary"}, workspace)
            self.assertNotIn("OPENCODE_CONFIG", env)
            self.assertEqual(env["PWD"], str(workspace))
            self.assertTrue(env["XDG_CONFIG_HOME"].startswith(tmp))
            base = json.loads((Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(base["enabled_providers"], ["local"])
            self.assertFalse(base["snapshot"])
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]), {"agent": {"bridge-worker": {"mode": "primary"}}})

    def test_unknown_config_keys_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            path = Path(tmp) / "c.json"
            path.write_text('{"endpoint": "http://x/v1", "typo_key": 1}', encoding="utf-8")
            with self.assertRaisesRegex(bridge.BridgeError, "typo_key"):
                bridge.load_config(path)
            path.write_text('{"endpoint": "http://x/v1"}', encoding="utf-8")
            self.assertEqual(bridge.load_config(path)["endpoint"], "http://x/v1")


class EventTests(unittest.TestCase):
    def test_final_answer_is_text_of_last_text_step(self):
        raw = "\n".join([
            event("step_start"),
            event("text", text="Let me look."),
            event("tool_use", tool="read", state={"status": "completed", "input": {"filePath": "a.py"}, "output": "x"}),
            event("step_finish", reason="tool-calls", tokens={"input": 100, "output": 10, "reasoning": 0, "cache": {"read": 5}}),
            event("step_start"),
            event("text", text="Final answer."),
            event("step_finish", reason="stop", tokens={"input": 20, "output": 30, "reasoning": 4, "cache": {"read": 50}}),
            "not json",
        ])
        digest = bridge.parse_events(raw)
        self.assertEqual(digest["response"], "Final answer.")
        self.assertEqual(digest["steps"], 2)
        self.assertEqual(digest["tokens"], {"input": 120, "output": 40, "reasoning": 4, "cache_read": 55})
        self.assertEqual(bridge.classify(digest, 0, False)[0], "SUCCESS")

    def test_denials_errors_and_empty_answers_are_not_success(self):
        denied = bridge.parse_events("\n".join([
            event("step_start"),
            event("tool_use", tool="bash", state={"status": "error", "input": {"command": "git status"},
                                                   "error": "The user has specified a rule which prevents you from using this specific tool call."}),
            event("text", text="Could not run git."),
        ]))
        self.assertEqual(len(denied["denied_actions"]), 1)
        self.assertEqual(bridge.classify(denied, 0, False)[0], "PARTIAL_DENIED")
        self.assertEqual(bridge.classify(bridge.parse_events(event("step_start")), 0, False)[0], "FAILED_OUTPUT")
        unparsed = bridge.parse_events(event("text", text="<tool_call>\n<function=list>\n<parameter=path>\n.\n</parameter>\n</function>\n</tool_call>"))
        self.assertEqual(bridge.classify(unparsed, 0, False), ("FAILED_OUTPUT", "Worker ended with an unparsed tool call instead of an answer"))
        errored = bridge.parse_events(json.dumps({"type": "error", "error": {"name": "APIError"}}))
        self.assertEqual(bridge.classify(errored, 1, False)[0], "FAILED_CHILD")
        self.assertEqual(bridge.classify(denied, 0, True)[0], "FAILED_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
