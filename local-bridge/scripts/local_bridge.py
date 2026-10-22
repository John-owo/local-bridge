#!/usr/bin/env python3
"""Explicit, disposable workspace bridge to a local model through OpenCode.

The worker is OpenCode running headless against a local OpenAI-compatible
server (Strata by default). This is context isolation, not an operating-system
security sandbox. The child process still runs with the caller's OS
permissions. Never stage secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SKILL_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("LOCAL_BRIDGE_CONFIG") or SKILL_DIR / "local-bridge.config.json")
DEFAULTS = {
    # OpenAI-compatible base URL of the local inference server.
    "endpoint": "http://127.0.0.1:8080/v1",
    "model_id": "strata",
    "context": 65536,
    "output": 8192,
    # Runs and OpenCode state; keep them off the system drive when possible.
    "runs_root": str(Path.home() / "Documents" / "Codex" / "local-bridge-runs"),
    "state_dir": str(Path.home() / ".local-bridge"),
    "opencode": "",
    "strata": "",
    # `strata launch` arguments used by `server start`.
    "launch_args": [],
}
MAX_FILES = 5000
MAX_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PROMPT_CHARS = 24000  # CreateProcess caps the whole command line at 32767 characters.
EXCLUDED_DIRS = {".git", ".claude", ".codex", ".agents", ".opencode", "node_modules", "venv", ".venv", "dist", "build", "cache", ".cache", "__pycache__"}
EXCLUDED_FILES = {"agents.md", "claude.md", "skill.md", "opencode.json", "opencode.jsonc", ".env", "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json", "settings.json"}
SECRET_PARTS = ("credential", "secret", "private", "token", "apikey", "api_key", "service-account", "service_account", ".pem", ".p12", ".pfx", ".key")
# Tier -> (reasoning effort variant, agent step cap). The local model thinks slowly; keep routine work shallow.
TIERS = {"routine": ("low", 40), "standard": ("medium", 80), "complex": ("high", 120)}
DENIAL_MARKERS = ("prevents you from using this specific tool call", "auto-rejecting", "permission denied", "rejected permission")
WORKER_PREAMBLE = (
    "You are an isolated worker running on a local model. Your workspace is the current directory: a copy of "
    "selected project files. Work only inside it. Do not access credentials, the network, the original project, "
    "or paths outside this workspace. Do not spawn other agents. Use the read, grep, glob and list tools to "
    "explore; shell commands outside the allowed list are denied, so do not retry a denied command in another "
    "form. Finish with a final answer that states what you found or changed, the checks you actually ran, and "
    "anything you are unsure of. Answer in English.\n\n"
)


class BridgeError(Exception):
    pass


def emit(data: dict, code: int = 0) -> int:
    print(json.dumps(data, ensure_ascii=True, sort_keys=True))
    return code


def write_json(path: Path, data: dict) -> None:
    # ASCII escapes keep the files readable by Windows PowerShell 5.1, which assumes the ANSI code page.
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = dict(DEFAULTS)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(f"Cannot parse {path.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise BridgeError(f"{path.name} must be a JSON object")
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            raise BridgeError(f"Unknown keys in {path.name}: {', '.join(sorted(unknown))}")
        config.update(data)
    return config


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def reparse(path: Path) -> bool:
    stat = path.lstat()
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def excluded(path: Path, is_dir: bool) -> bool:
    name = path.name.lower()
    if is_dir and name in EXCLUDED_DIRS:
        return True
    if not is_dir and (name in EXCLUDED_FILES or name.startswith(".env.")):
        return True
    return any(part in name for part in SECRET_PARTS)


def safe_relpath(value: str) -> Path:
    # PureWindowsPath catches drive/UNC forms even when tested on POSIX.
    win = PureWindowsPath(value)
    path = Path(value)
    if value in ("", "."):
        raise BridgeError("Include must name selected files or directories; full-source staging is disabled")
    if win.is_absolute() or win.drive or path.is_absolute() or any(p in ("..", "") for p in win.parts):
        raise BridgeError(f"Unsafe include path: {value}")
    return path


def resolve_opencode(config: dict) -> str:
    # Call the real executable: the npm .cmd shim would route the prompt through cmd.exe quoting.
    if config.get("opencode"):
        candidate = Path(config["opencode"])
        if candidate.is_file():
            return str(candidate)
        raise BridgeError(f"Configured opencode executable not found: {candidate}")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("opencode.exe") or shutil.which("opencode")
    if found and not found.lower().endswith((".cmd", ".bat")):
        return found
    raise BridgeError("opencode executable not found; install opencode-ai or set \"opencode\" in local-bridge.config.json")


def resolve_strata(config: dict) -> str:
    if config.get("strata"):
        candidate = Path(config["strata"])
        if candidate.is_file():
            return str(candidate)
        raise BridgeError(f"Configured strata command not found: {candidate}")
    found = shutil.which("strata.cmd") or shutil.which("strata")
    if found:
        return found
    raise BridgeError("strata command not found; set \"strata\" in local-bridge.config.json")


def http_json(url: str, timeout: float = 5) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def server_health(config: dict) -> dict:
    url = config["endpoint"].rstrip("/") + "/models"
    started = time.time()
    try:
        data = http_json(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)] if isinstance(data, dict) else []
    return {"ok": True, "url": url, "models": models, "latency_ms": round((time.time() - started) * 1000)}


def state_paths(config: dict) -> dict[str, Path]:
    state = Path(config["state_dir"])
    return {"config_home": state / "opencode-config", "data_home": state / "opencode-data"}


def opencode_base_config(config: dict) -> dict:
    """The fixed provider config the worker loads. It replaces, not extends, the user's global OpenCode config."""
    variants = {effort: {"reasoningEffort": effort} for effort in ("none", "low", "medium", "high")}
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "local": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Local model (local-bridge)",
                "options": {"baseURL": config["endpoint"], "apiKey": "local", "timeout": False},
                "models": {config["model_id"]: {"name": "Local worker model", "tool_call": True,
                                                "limit": {"context": int(config["context"]), "output": int(config["output"])},
                                                "variants": variants}},
            }
        },
        "enabled_providers": ["local"],
        "model": f"local/{config['model_id']}",
        "share": "disabled",
        "autoupdate": False,
        # OpenCode's undo snapshots copy the workspace into a shadow git repo on every step: needless disk writes here.
        "snapshot": False,
        "mcp": {},
        "instructions": [],
    }


def worker_agent(mode: str, steps: int, allow_commands: list[str]) -> dict:
    bash = {"*": "deny"}
    for command in allow_commands:
        if not command.strip() or any(ch in command for ch in "*?\n\r"):
            raise BridgeError(f"Allowed commands must be exact text without wildcards or newlines: {command!r}")
        bash[command.strip()] = "allow"
    permission = {
        "*": "deny",
        "read": "allow", "glob": "allow", "grep": "allow", "list": "allow", "lsp": "allow", "todowrite": "allow",
        "edit": "allow" if mode == "write" else "deny",
        "bash": bash,
        "external_directory": "deny", "webfetch": "deny", "websearch": "deny", "task": "deny", "skill": "deny",
        "question": "deny", "doom_loop": "deny",
    }
    return {"mode": "primary", "description": "local-bridge isolated worker", "steps": steps, "permission": permission}


def ensure_opencode_home(config: dict) -> dict[str, Path]:
    paths = state_paths(config)
    target = paths["config_home"] / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    paths["data_home"].mkdir(parents=True, exist_ok=True)
    text = json.dumps(opencode_base_config(config), indent=2) + "\n"
    if not target.is_file() or target.read_text(encoding="utf-8") != text:
        target.write_text(text, encoding="utf-8")
    return paths


def child_env(config: dict, agent: dict, workspace: Path) -> dict:
    paths = ensure_opencode_home(config)
    env = dict(os.environ)
    for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_PERMISSION"):
        env.pop(name, None)
    env.update({
        # OpenCode takes its project directory from an inherited PWD (set by Git Bash) before the real cwd.
        "PWD": str(workspace),
        "XDG_CONFIG_HOME": str(paths["config_home"]),
        "XDG_DATA_HOME": str(paths["data_home"]),
        "OPENCODE_CONFIG_CONTENT": json.dumps({"agent": {"bridge-worker": agent}}),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
    })
    return env


def prepare(source: Path, includes: list[str], owner: str, runs_root: Path) -> dict:
    if not source.exists() or reparse(source):
        raise BridgeError("Source must be an existing ordinary directory, not a link or reparse point")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise BridgeError("Source must be a directory")
    if not includes:
        raise BridgeError("At least one --include is required")
    selected: dict[str, Path] = {}
    for item in includes:
        rel = safe_relpath(item)
        candidate = source / rel
        # Validate every component before resolving: links and reparse points are excluded.
        current = source
        for part in rel.parts:
            current = current / part
            if not current.exists() or reparse(current) or excluded(current, current.is_dir()):
                raise BridgeError(f"Included path is absent, linked, or excluded: {item}")
        if candidate.is_file():
            selected[candidate.relative_to(source).as_posix()] = candidate
        elif candidate.is_dir():
            for root, dirs, files in os.walk(candidate, followlinks=False):
                base = Path(root)
                dirs[:] = [d for d in dirs if not reparse(base / d) and not excluded(base / d, True)]
                for name in files:
                    path = base / name
                    if not reparse(path) and path.is_file() and not excluded(path, False):
                        selected[path.relative_to(source).as_posix()] = path
    if not selected:
        raise BridgeError("No eligible files selected")
    if len(selected) > MAX_FILES:
        raise BridgeError("Selected files exceed 5000-file limit")
    total = 0
    for path in selected.values():
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise BridgeError(f"Selected file exceeds 5 MiB limit: {path.name}")
        total += size
    if total > MAX_BYTES:
        raise BridgeError("Selected files exceed 100 MiB limit")
    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = runs_root / str(uuid.uuid4())
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    manifest = {"schema": 1, "owner": owner, "source": str(source), "context_isolation_only": True, "files": {}}
    try:
        for rel, origin in sorted(selected.items()):
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # Recheck just before opening; do not follow a swapped link.
            if reparse(origin) or not origin.is_file():
                raise BridgeError(f"Source changed during staging: {rel}")
            source_hash = hash_file(origin)
            shutil.copyfile(origin, target, follow_symlinks=False)
            copied_hash = hash_file(target)
            if source_hash != copied_hash:
                raise BridgeError(f"Source changed during staging: {rel}")
            manifest["files"][rel] = {"source_sha256": source_hash, "copied_sha256": copied_hash, "bytes": target.stat().st_size}
        write_json(run_dir / "manifest.json", manifest)
        write_json(run_dir / "status.json", {"state": "PREPARED", "owner": owner})
    except Exception:
        shutil.rmtree(run_dir)
        raise
    return {"run_dir": str(run_dir), "workspace": str(workspace), "file_count": len(selected), "bytes": total, "owner": owner}


def validate_run(run_dir: Path) -> tuple[Path, dict]:
    run_dir = run_dir.resolve(strict=True)
    manifest_path = run_dir / "manifest.json"
    workspace = run_dir / "workspace"
    if not manifest_path.is_file() or not workspace.is_dir():
        raise BridgeError("Run directory is missing manifest or workspace")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise BridgeError("Invalid run manifest")
    return workspace, manifest


def terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10, shell=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def snapshot(workspace: Path) -> dict[str, str]:
    hashes = {}
    for root, dirs, files in os.walk(workspace, followlinks=False):
        base = Path(root)
        dirs[:] = [d for d in dirs if not reparse(base / d)]
        for name in files:
            path = base / name
            if path.is_file() and not reparse(path):
                hashes[path.relative_to(workspace).as_posix()] = hash_file(path)
    return hashes


def inspect(run_dir: Path) -> dict:
    workspace, manifest = validate_run(run_dir)
    baseline = {name: info["copied_sha256"] for name, info in manifest["files"].items()}
    current = snapshot(workspace)
    changes = {
        "created": {k: v for k, v in current.items() if k not in baseline},
        "modified": {k: v for k, v in current.items() if k in baseline and v != baseline[k]},
        "deleted": {k: baseline[k] for k in baseline if k not in current},
    }
    status_path = run_dir / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {"state": "UNKNOWN"}
    return {"run_dir": str(run_dir.resolve()), "workspace": str(workspace), "status": status, "changes": changes, "source_hashes": {k: v["source_sha256"] for k, v in manifest["files"].items()}}


def parse_events(raw: str) -> dict:
    """Digest `opencode run --format json` output: final answer, tool calls, denials, token use."""
    texts, tools, denials, errors = [], [], [], []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    steps = 0
    last_text_step = -1
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "step_start":
            steps += 1
        elif kind == "text" and part.get("text", "").strip():
            if last_text_step != steps:
                texts = []  # Keep only the text of the latest step that produced any.
                last_text_step = steps
            texts.append(part["text"].strip())
        elif kind == "tool_use":
            state = part.get("state") or {}
            entry = {"tool": part.get("tool"), "status": state.get("status"), "input": json.dumps(state.get("input"), ensure_ascii=False)[:300]}
            tools.append(entry)
            message = str(state.get("error") or "")
            if state.get("status") == "error" and any(marker in message.lower() for marker in DENIAL_MARKERS):
                denials.append(entry)
        elif kind == "step_finish":
            used = part.get("tokens") or {}
            for key in ("input", "output", "reasoning"):
                tokens[key] += int(used.get(key) or 0)
            tokens["cache_read"] += int((used.get("cache") or {}).get("read") or 0)
        elif kind == "error":
            errors.append(json.dumps(event.get("error"), ensure_ascii=False)[:500])
    return {"response": "\n\n".join(texts), "tool_calls": tools, "denied_actions": denials, "errors": errors, "steps": steps, "tokens": tokens}


def classify(digest: dict, returncode: int | None, timed_out: bool) -> tuple[str, str]:
    if timed_out:
        return "FAILED_TIMEOUT", "Watchdog timeout"
    if digest["errors"]:
        return "FAILED_CHILD", "OpenCode reported a session error"
    if returncode:
        return "FAILED_PROCESS", f"opencode exited {returncode}"
    if not digest["response"].strip():
        return "FAILED_OUTPUT", "Worker produced no final answer"
    if digest["denied_actions"]:
        return "PARTIAL_DENIED", f"{len(digest['denied_actions'])} tool call(s) were denied; verify the answer did not depend on them"
    return "SUCCESS", ""


def run_child(run_dir: Path, task_file: Path, tier: str, mode: str, allow_commands: list[str], timeout: int, config: dict) -> dict:
    workspace, manifest = validate_run(run_dir)
    lock = run_dir / ".run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise BridgeError("This run is already locked") from exc
    os.close(fd)
    try:
        status_path = run_dir / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("state") != "PREPARED":
            raise BridgeError("Run has already been used; prepare a fresh run")
        effort, steps = TIERS[tier]
        try:
            opencode = resolve_opencode(config)
            health = server_health(config)
            if not health["ok"]:
                raise BridgeError(f"Local model server is not answering at {health['url']}: {health['error']}. Run `server start` first.")
            task_file = task_file.resolve(strict=True)
            if workspace == task_file or workspace in task_file.parents:
                raise BridgeError("Task file must be outside the staged workspace")
            task = task_file.read_text(encoding="utf-8")
            mode_note = ("Mode: write. You may edit files inside the workspace.\n" if mode == "write"
                         else "Mode: read-only. File edits are denied; report proposed changes as unified diffs in your answer.\n")
            commands = ("Allowed shell commands (run verbatim from the workspace root, no cd, no chaining): "
                        + "; ".join(allow_commands) + "\n") if allow_commands else "No shell commands are allowed.\n"
            prompt = WORKER_PREAMBLE + mode_note + commands + "\n" + task
            if not task.strip() or len(prompt) > MAX_PROMPT_CHARS:
                raise BridgeError(f"Task prompt must contain text and fit within {MAX_PROMPT_CHARS} characters including the wrapper")
            agent = worker_agent(mode, steps, allow_commands)
            env = child_env(config, agent, workspace)
            (run_dir / "task-input.txt").write_text(task, encoding="utf-8")
        except (BridgeError, OSError, ValueError, KeyError) as exc:
            failure = {"state": "FAILED_PREFLIGHT", "reason": str(exc), "response": None, "changes": inspect(run_dir)["changes"]}
            write_json(run_dir / "result.json", failure)
            write_json(status_path, {"state": "FAILED_PREFLIGHT", "reason": str(exc), "finished_at": time.time()})
            raise
        selected = {"model": f"local/{config['model_id']}", "endpoint": config["endpoint"], "variant": effort, "steps": steps,
                    "tier": tier, "mode": mode, "allow_commands": allow_commands, "server_models": health.get("models")}
        write_json(run_dir / "selection.json", {"selected": selected, "agent": agent})
        write_json(status_path, {"state": "RUNNING", "selected": selected, "started_at": time.time(), "pid": None})
        args = [opencode, "run", "--pure", "--format", "json", "--dir", str(workspace), "--agent", "bridge-worker", "--model", selected["model"],
                "--variant", effort, "--title", f"local-bridge {run_dir.name[:8]}", prompt]
        stdout_path, stderr_path = run_dir / "stdout.jsonl", run_dir / "stderr.txt"
        started = time.time()
        timed_out = False
        process = None
        try:
            with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
                # stdin must be closed: `opencode run` appends piped stdin to the prompt and waits for EOF.
                process = subprocess.Popen(args, cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False,
                                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0, start_new_session=os.name != "nt")
                write_json(status_path, {"state": "RUNNING", "selected": selected, "started_at": started, "pid": process.pid})
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_tree(process)
            digest = parse_events(stdout_path.read_text(encoding="utf-8", errors="replace"))
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            if "auto-rejecting" in stderr and not digest["denied_actions"]:
                digest["denied_actions"].append({"tool": "permission", "status": "error", "input": "see stderr.txt"})
            state, reason = classify(digest, process.returncode, timed_out)
        except Exception as exc:  # noqa: BLE001 - record any launch failure as a run result
            digest = parse_events("")
            state, reason = "FAILED_PROCESS", str(exc)
            stderr_path.write_text(reason, encoding="utf-8")
            stdout_path.touch(exist_ok=True)
        elapsed = round(time.time() - started, 2)
        tokens = digest["tokens"]
        result = {"state": state, "reason": reason, "selected": selected, "exit_code": process.returncode if process else None,
                  "elapsed_seconds": elapsed, "response": digest["response"] or None, "steps": digest["steps"], "tokens": tokens,
                  "output_tokens_per_second": round(tokens["output"] / elapsed, 1) if elapsed else None,
                  "tool_calls": digest["tool_calls"], "denied_actions": digest["denied_actions"], "errors": digest["errors"],
                  "changes": inspect(run_dir)["changes"]}
        write_json(run_dir / "result.json", result)
        write_json(status_path, {"state": state, "reason": reason, "selected": selected, "finished_at": time.time()})
        return {"run_dir": str(run_dir), "result_path": str(run_dir / "result.json"), "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
                "status": state, "reason": reason, "elapsed_seconds": elapsed, "steps": digest["steps"], "tokens": tokens,
                "denied": len(digest["denied_actions"]), "changes": {k: sorted(v) for k, v in result["changes"].items()}}
    finally:
        lock.unlink(missing_ok=True)


def doctor(config: dict) -> dict:
    report = {"config_path": str(CONFIG_PATH), "config_file_present": CONFIG_PATH.is_file(), "endpoint": config["endpoint"], "runs_root": config["runs_root"]}
    ok = True
    try:
        exe = resolve_opencode(config)
        version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        report["opencode"] = {"path": exe, "version": version.stdout.strip()}
    except (BridgeError, OSError, subprocess.TimeoutExpired) as exc:
        report["opencode"] = {"error": str(exc)}
        ok = False
    try:
        report["strata"] = {"path": resolve_strata(config)}
    except BridgeError as exc:
        report["strata"] = {"error": str(exc)}
    report["server"] = server_health(config)
    ok = ok and report["server"]["ok"]
    try:
        Path(config["runs_root"]).mkdir(parents=True, exist_ok=True)
        report["runs_root_writable"] = os.access(config["runs_root"], os.W_OK)
    except OSError as exc:
        report["runs_root_writable"] = False
        report["runs_root_error"] = str(exc)
    ok = ok and report["runs_root_writable"]
    report["ok"] = ok
    return report


def gpu_competitors() -> list[str]:
    """Other local inference servers that would compete for VRAM."""
    if os.name != "nt":
        return []
    names = ("llama-server.exe", "ollama.exe", "ollama_llama_server.exe", "LM Studio.exe", "lms.exe")
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = set()
    for line in out.splitlines():
        image = line.split(",", 1)[0].strip('"')
        if image.lower() in {n.lower() for n in names}:
            found.add(image)
    return sorted(found)


def server(action: str, config: dict, wait: int, force: bool) -> dict:
    health = server_health(config)
    if action == "status":
        return {"server": health, "gpu_competitors": gpu_competitors()}
    strata = resolve_strata(config)
    if action == "stop":
        done = subprocess.run([strata, "launch", "--stop"], capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        return {"stopped": done.returncode == 0, "output": (done.stdout + done.stderr)[-2000:], "server": server_health(config)}
    if health["ok"]:
        return {"already_running": True, "server": health}
    competitors = gpu_competitors()
    if competitors and not force:
        raise BridgeError(f"Other inference servers hold the GPU: {', '.join(competitors)}. Stop them or pass --force.")
    if not config["launch_args"]:
        raise BridgeError("No launch_args in local-bridge.config.json; cannot start the server")
    args = [strata, "launch", *config["launch_args"], "--no-tui", "--yes"]
    log = Path(config["state_dir"]) / "strata-launch.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as out:
        done = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=max(wait, 60))
    deadline = time.time() + wait
    while time.time() < deadline:
        health = server_health(config)
        if health["ok"]:
            return {"started": True, "server": health, "log": str(log)}
        time.sleep(3)
    return {"started": False, "exit_code": done.returncode, "server": health, "log": str(log)}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    p = sub.add_parser("server")
    p.add_argument("action", choices=("status", "start", "stop"))
    p.add_argument("--wait", type=int, default=420)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--include", action="append", default=[])
    p.add_argument("--owner", choices=("codex", "cc"), required=True)
    p = sub.add_parser("run")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--task-file", type=Path, required=True)
    p.add_argument("--tier", choices=tuple(TIERS), required=True)
    p.add_argument("--mode", choices=("read", "write"), default="read")
    p.add_argument("--allow-command", action="append", default=[])
    p.add_argument("--timeout", type=int, default=900)
    p = sub.add_parser("inspect")
    p.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if args.command == "doctor":
            report = doctor(config)
            return emit(report, 0 if report["ok"] else 1)
        if args.command == "server":
            return emit({"ok": True, **server(args.action, config, args.wait, args.force)})
        if args.command == "prepare":
            return emit({"ok": True, **prepare(args.source, args.include, args.owner, Path(config["runs_root"]))})
        if args.command == "inspect":
            return emit({"ok": True, **inspect(args.run_dir)})
        if not 1 <= args.timeout <= 7200:
            raise BridgeError("--timeout must be 1-7200 seconds")
        result = run_child(args.run_dir, args.task_file, args.tier, args.mode, args.allow_command, args.timeout, config)
        good = result["status"] in ("SUCCESS", "PARTIAL_DENIED")
        return emit({"ok": good, **result}, 0 if good else 1)
    except (BridgeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return emit({"ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
