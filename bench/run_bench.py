#!/usr/bin/env python3
"""End-to-end quality and speed benchmark for local-bridge workers.

Each case goes through the real bridge CLI (prepare -> run), then is graded
automatically: hidden tests, mutant killing, or required facts in the answer.
Usage: python run_bench.py --label NAME [--repeat N] [--case ID ...]
The target backend is whatever local-bridge.config.json (or LOCAL_BRIDGE_CONFIG) points at.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / "local-bridge" / "scripts" / "local_bridge.py"
FIX = HERE / "fixtures"
RESULTS = HERE / "results"


def bridge(*args: str) -> dict:
    out = subprocess.run([sys.executable, str(BRIDGE), *args], capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": out.stdout[-500:] + out.stderr[-500:]}


def dispatch(source: Path, includes: list[str], task: str, tier: str, mode: str = "read", allow: tuple[str, ...] | list[str] = (), timeout: int = 900) -> tuple[dict, dict, Path]:
    prep = bridge("prepare", "--source", str(source), *sum((["--include", i] for i in includes), []), "--owner", "cc")
    if not prep.get("ok"):
        raise RuntimeError(prep)
    run_dir = Path(prep["run_dir"])
    task_file = run_dir / "task.txt"
    task_file.write_text(task, encoding="utf-8")
    args = ["run", "--run-dir", str(run_dir), "--task-file", str(task_file), "--tier", tier, "--mode", mode, "--timeout", str(timeout)]
    for command in allow:
        args += ["--allow-command", command]
    summary = bridge(*args)
    result_path = run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    return summary, result, run_dir


def facts(text: str | None, required: dict[str, str]) -> dict[str, bool]:
    return {name: bool(re.search(pattern, text or "", re.I | re.S)) for name, pattern in required.items()}


def case_review(tier: str) -> dict:
    task = ("Review inventory.py for correctness bugs only (no style). For each bug give the method, a concrete failing "
            "scenario, and a minimal fix as a unified diff.")
    summary, result, _ = dispatch(FIX / "review", ["inventory.py"], task, tier)
    found = facts(result.get("response"), {
        "plus_assign": r"=\s?\+|=\+|overwrit|reassign|replac\w* the total|only the last",
        "negative_qty": r"negative",
        "mutable_default": r"mutable default|default=\[\]|default\s*=\s*\[\]|shared (?:list|default)",
    })
    return {"score": sum(found.values()) / len(found), "detail": found, **pick(summary, result)}


def case_bugfix(tier: str) -> dict:
    task = ("interval.py has a function merge_intervals with a docstring describing its contract. It has bugs. Fix them "
            "in place so the function honours the docstring for unsorted input, touching intervals, contained intervals "
            "and empty input, and does not mutate its argument. No shell commands are available, so reason carefully.")
    summary, result, run_dir = dispatch(FIX / "bugfix", ["interval.py"], task, tier, mode="write")
    return hidden_tests(FIX / "bugfix" / "test_interval_hidden.py", run_dir, summary, result)


def case_hard(tier: str) -> dict:
    task = ("lru.py implements LRUCache; its class docstring is the contract. The implementation has several bugs. "
            "Fix every deviation from the contract in place, keeping the public API. No shell commands are available, "
            "so trace each rule of the contract against the code carefully. List each bug you fixed.")
    summary, result, run_dir = dispatch(FIX / "hard", ["lru.py"], task, tier, mode="write")
    return hidden_tests(FIX / "hard" / "test_lru_hidden.py", run_dir, summary, result)


def hidden_tests(test: Path, run_dir: Path, summary: dict, result: dict) -> dict:
    done = subprocess.run([sys.executable, str(test), str(run_dir / "workspace")], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    ran = re.search(r"Ran (\d+) test", done.stderr)
    failed = re.search(r"failures=(\d+)", done.stderr)
    errors = re.search(r"errors=(\d+)", done.stderr)
    total = int(ran.group(1)) if ran else 1
    bad = (int(failed.group(1)) if failed else 0) + (int(errors.group(1)) if errors else 0) if ran else total
    return {"score": (total - bad) / total, "detail": {"hidden_tests": total, "failed": bad}, **pick(summary, result)}


def case_testgen(tier: str) -> dict:
    task = ("Write a thorough unittest file test_slugify.py for slugify.py that pins down every rule in its docstring, "
            "including the length cap. Then run the allowed test command and report the actual output. Do not modify slugify.py.")
    summary, result, run_dir = dispatch(FIX / "testgen", ["slugify.py"], task, tier, mode="write",
                                        allow=["python -m unittest test_slugify -v"])
    test_file = run_dir / "workspace" / "test_slugify.py"
    detail = {"test_file": test_file.is_file(), "passes_on_original": False, "mutants_killed": 0}
    if test_file.is_file():
        detail["passes_on_original"] = run_tests_against(test_file, FIX / "testgen" / "slugify.py")
        spec = importlib.util.spec_from_file_location("mutants", FIX / "testgen" / "slugify_mutants.py")
        mutants = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mutants)
        for mutant in mutants.MUTANTS:
            if not run_tests_against(test_file, FIX / "testgen" / "slugify_mutants.py", mutant.__name__):
                detail["mutants_killed"] += 1
    score = (detail["mutants_killed"] / 5) if detail["passes_on_original"] else 0.0
    return {"score": score, "detail": detail, **pick(summary, result)}


def run_tests_against(test_file: Path, module_file: Path, alias: str | None = None) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copy(test_file, tmp / "test_slugify.py")
        if alias:
            source = module_file.read_text(encoding="utf-8") + f"\n\nslugify = {alias}\n"
            (tmp / "slugify.py").write_text(source, encoding="utf-8")
        else:
            shutil.copy(module_file, tmp / "slugify.py")
        done = subprocess.run([sys.executable, "-m", "unittest", "test_slugify"], cwd=tmp, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)
        return done.returncode == 0


def case_explore(tier: str) -> dict:
    task = ("Explore this repository and answer precisely, citing file and function names:\n"
            "1. What is the maximum task prompt length in characters, and which constant holds it?\n"
            "2. Which function picks the model for a tier, and which model family/effort does the 'complex' tier try first?\n"
            "3. How does the helper kill a timed-out child process tree on Windows (exact command)?\n"
            "4. Name three file names that are always excluded from staging.\n"
            "5. What state does a run end in if the child process exceeds its timeout?")
    summary, result, _ = dispatch(FIX / "explore_q" / "agy-bridge", ["agy-bridge", "tests", "README.md"], task, tier)
    found = facts(result.get("response"), {
        "prompt_limit": r"12[,\s]?000.{0,200}MAX_PROMPT_CHARS|MAX_PROMPT_CHARS.{0,200}12[,\s]?000",
        "choose_model": r"choose_model",
        "complex_first": r"argon.{0,40}high|high.{0,40}argon",
        "taskkill": r"taskkill.{0,40}/T.{0,10}/F|taskkill.{0,40}/F.{0,10}/T",
        "excluded_files": r"(?:\.env|agents\.md|claude\.md|credentials|id_rsa|secrets\.json|settings\.json)(?:.|\n){0,200}(?:\.env|agents\.md|claude\.md|credentials|id_rsa|secrets\.json|settings\.json)",
        "timeout_state": r"FAILED_TIMEOUT",
    })
    return {"score": sum(found.values()) / len(found), "detail": found, **pick(summary, result)}


def pick(summary: dict, result: dict) -> dict:
    return {"state": summary.get("status") or summary.get("error"), "elapsed": summary.get("elapsed_seconds"), "steps": summary.get("steps"),
            "tokens": summary.get("tokens"), "denied": summary.get("denied"),
            "tool_errors": sum(1 for t in result.get("tool_calls", []) if t.get("status") == "error")}


CASES = {"review": case_review, "bugfix": case_bugfix, "testgen": case_testgen, "explore": case_explore, "hard": case_hard}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--tier", default="standard")
    parser.add_argument("--case", action="append", choices=tuple(CASES))
    args = parser.parse_args()
    if not (FIX / "explore_q" / "agy-bridge").is_dir():
        print("missing fixtures/explore_q/agy-bridge (clone John-owo/agy-bridge there)", file=sys.stderr)
        return 2
    RESULTS.mkdir(exist_ok=True)
    rows = []
    for name in args.case or list(CASES):
        for i in range(args.repeat):
            started = time.time()
            try:
                row = CASES[name](args.tier)
            except Exception as exc:  # noqa: BLE001 - a crashed case scores zero
                row = {"score": 0.0, "state": f"BENCH_ERROR {exc}"}
            row.update({"case": name, "rep": i + 1, "wall": round(time.time() - started, 1)})
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    out = RESULTS / f"{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"label": args.label, "tier": args.tier, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    mean = sum(r["score"] for r in rows) / len(rows)
    print(json.dumps({"label": args.label, "mean_score": round(mean, 3), "results": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
