#!/usr/bin/env python3
"""Measure llama-server's system-RAM commit and VRAM under different flags (Qwen3.8-27B on E:).

Each variant: start llama-server, wait for /health, send one ~2K-token request, read the process's private
bytes (commit), working set and dedicated VRAM, then stop it.
Usage: python llama_mem.py [--variant NAME ...]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

BACKENDS = Path.home() / ".lmstudio" / "extensions" / "backends"
RUNTIMES = {
    "vulkan": (BACKENDS / "llama.cpp-win-x86_64-vulkan-avx2-2.37.0", BACKENDS / "vendor" / "win-llama-vulkan-vendor-v2"),
    "rocm": (BACKENDS / "llama.cpp-win-x86_64-amd-rocm-avx2-2.41.0", BACKENDS / "vendor" / "win-llama-rocm-vendor-v6" / "bin"),
}
MODEL = r"E:\LLM\models\unsloth\Qwen3.8-27B-GGUF\Qwen3.8-27B-UD-IQ3_XXS.gguf"
BASE = ["-m", MODEL, "-a", "qwen3.8-27b", "-ngl", "99", "-c", "32768", "-fa", "auto", "--jinja",
        "--chat-template-kwargs", '{"enable_thinking":false}', "--host", "127.0.0.1", "--port", "1234"]
VARIANTS = {
    "baseline": ("vulkan", []),
    "cache-ram-0": ("vulkan", ["--cache-ram", "0"]),
    "np1": ("vulkan", ["-np", "1"]),
    "no-mmap": ("vulkan", ["--no-mmap"]),
    "all-three": ("vulkan", ["--cache-ram", "0", "-np", "1", "--no-mmap"]),
    "rocm-baseline": ("rocm", []),
}
HERE = Path(__file__).resolve().parent
PROMPT = (HERE.parent / "local-bridge" / "scripts" / "local_bridge.py").read_text(encoding="utf-8")[:7000]


def probe(pid: int) -> dict:
    script = (f"$p = Get-Process -Id {pid}; "
              f"$v = (Get-Counter '\\GPU Process Memory(pid_{pid}_*)\\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples | Measure-Object CookedValue -Sum; "
              "$m = Get-CimInstance Win32_PerfRawData_PerfOS_Memory; "
              "[pscustomobject]@{private=[int64]$p.PrivateMemorySize64; ws=[int64]$p.WorkingSet64; vram=[int64]$v.Sum; "
              "committed=[int64]$m.CommittedBytes; avail_mb=[int64]$m.AvailableMBytes} | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    data = json.loads(out.stdout)
    return {"private_gb": round(data["private"] / 2**30, 2), "working_set_gb": round(data["ws"] / 2**30, 2),
            "vram_gb": round(data["vram"] / 2**30, 2), "system_committed_gb": round(data["committed"] / 2**30, 1),
            "avail_ram_gb": round(data["avail_mb"] / 1024, 1)}


def run(name: str) -> dict:
    runtime, extra = VARIANTS[name]
    rt, vendor = RUNTIMES[runtime]
    env = dict(os.environ, PATH=f"{vendor};{rt};{os.environ['PATH']}")
    log = open(HERE / "results" / f"llama-{name}.log", "w", encoding="utf-8")
    proc = subprocess.Popen([str(rt / "llama-server.exe"), *BASE, *extra], env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    row: dict = {"variant": name, "args": " ".join(extra) or "(start-server.ps1 defaults)"}
    try:
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:1234/health", timeout=2) as r:
                    if b"ok" in r.read():
                        break
            except OSError:
                time.sleep(2)
        row["after_load"] = probe(proc.pid)
        body = json.dumps({"model": "qwen3.8-27b", "max_tokens": 200, "temperature": 0,
                           "messages": [{"role": "user", "content": "Summarise this code in 5 bullets:\n\n" + PROMPT}]}).encode()
        req = urllib.request.Request("http://127.0.0.1:1234/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            timings = json.loads(r.read())["timings"]
        row["after_request"] = probe(proc.pid)
        row["prompt_n"], row["prefill_tok_s"], row["gen_tok_s"] = timings["prompt_n"], round(timings["prompt_per_second"]), round(timings["predicted_per_second"], 1)
    except Exception as exc:  # noqa: BLE001 - record and still stop the server
        row["error"] = str(exc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        time.sleep(5)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", action="append", choices=tuple(VARIANTS))
    args = parser.parse_args()
    (HERE / "results").mkdir(exist_ok=True)
    for name in args.variant or list(VARIANTS):
        print(json.dumps(run(name)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
