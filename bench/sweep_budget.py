#!/usr/bin/env python3
"""Sweep Strata's --resident-budget-gib and measure speed, memory and disk traffic per setting.

Each budget: start Strata (Coder IQ1_M, 32K, mapped experts), read the RAM tier the engine actually got from its
log, warm up, time decode and a long-prompt read, sample memory/paging/disk counters, stop.
SSD wear comes from writes only: page-file traffic (pagefile_out_mb) and E:/C: writes are the numbers to watch.
Usage: python sweep_budget.py --budgets 2 8 12 16 20 [--label NAME]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

STRATA = r"E:\Strata\terminal\current\bin\strata.cmd"
BASE_ARGS = ["launch", "--family", "coder", "--model", "iq1_m", "--data-dir", r"E:\Strata-data", "--context", "32k",
             "--vision", "off", "--access", "local", "--port", "8080", "--set", "mmap-experts"]
ENGINE_LOG = Path(os.environ["LOCALAPPDATA"]) / "Strata" / "launch" / "iq1_m-32k.engine.log"
URL = "http://127.0.0.1:8080/v1/chat/completions"
HERE = Path(__file__).resolve().parent
LONG_SOURCE = HERE.parent / "local-bridge" / "scripts" / "local_bridge.py"
ENV = dict(os.environ, STRATA_ENGINE_BACKEND="hip", STRATA_COMPONENTS_DIR=r"E:\Strata\components")


def chat(content: str, max_tokens: int) -> dict:
    body = json.dumps({"model": "strata", "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens,
                       "reasoning_effort": "low", "temperature": 0}).encode()
    request = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.loads(response.read())["timings"]


def counters() -> dict:
    """Cumulative raw counters (no admin needed)."""
    script = (
        "$m = Get-CimInstance Win32_PerfRawData_PerfOS_Memory; "
        "$d = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk | Where-Object Name -in '0 C:','2 E:'; "
        "$p = Get-Process strata -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{pages_out=[int64]$m.PagesOutputPersec; avail_mb=[int64]$m.AvailableMBytes; "
        "committed=[int64]$m.CommittedBytes; limit=[int64]$m.CommitLimit; "
        "c_write=[int64]($d | Where-Object Name -eq '0 C:').DiskWriteBytesPersec; "
        "e_read=[int64]($d | Where-Object Name -eq '2 E:').DiskReadBytesPersec; "
        "e_write=[int64]($d | Where-Object Name -eq '2 E:').DiskWriteBytesPersec; "
        "strata_private=[int64]$p.PrivateMemorySize64; strata_ws=[int64]$p.WorkingSet64} | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return json.loads(out.stdout)


def engine_facts() -> dict:
    text = ENGINE_LOG.read_text(encoding="utf-8", errors="replace") if ENGINE_LOG.is_file() else ""
    facts = {}
    for key, pattern in {
        "ram_tier_gib": r"resident RAM mode: ([\d.]+) GiB of experts in RAM",
        "budget_clamped_to_gib": r"exceeds available commit .*?: ([\d.]+) GiB",
        "gpu_slots": r"expert cache (\d+) slots",
        "lend_kept": r"FileExpertSource: (\d+) of the prompt path's (\d+) lendable slots",
        "vram_free_mib": r"(\d+) MiB of VRAM free with everything loaded",
    }.items():
        m = re.search(pattern, text)
        if m:
            facts[key] = m.group(1) if len(m.groups()) == 1 else f"{m.group(1)}/{m.group(2)}"
    return facts


def run_budget(budget: float) -> dict:
    row: dict = {"budget_gib": budget}
    started = time.time()
    launch = subprocess.run([STRATA, *BASE_ARGS, "--set", f"resident-budget-gib={budget}", "--yes", "--no-tui"], env=ENV,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=900)
    row["load_seconds"] = round(time.time() - started, 1)
    if launch.returncode != 0:
        row["error"] = (launch.stdout + launch.stderr)[-600:]
        row.update(engine_facts())
        return row
    row.update(engine_facts())
    try:
        chat("Say hello.", 16)  # warm-up
        before, before_time = counters(), time.time()
        decode = [chat(f"Variant {i}. Explain how a hash map resolves collisions, then give a short Python example.", 400) for i in range(3)]
        source = LONG_SOURCE.read_text(encoding="utf-8")
        prefill = chat("Summarise what this module does in five bullet points.\n\n" + source, 64)
        after, after_time = counters(), time.time()
        row.update({
            "decode_tok_s": round(sum(t["predicted_per_second"] for t in decode) / len(decode), 1),
            "draft_accept": round(sum(t.get("draft_n_accepted", 0) for t in decode) / max(1, sum(t.get("draft_n", 0) for t in decode)), 2),
            "prompt_tokens": prefill["prompt_n"], "prefill_tok_s": prefill["prompt_per_second"],
            "e_read_gb": round((after["e_read"] - before["e_read"]) / 2**30, 1),
            "c_write_gb": round((after["c_write"] - before["c_write"]) / 2**30, 2),
            "e_write_gb": round((after["e_write"] - before["e_write"]) / 2**30, 2),
            "test_seconds": round(after_time - before_time, 1),
            "pagefile_out_mb": round((after["pages_out"] - before["pages_out"]) * 4096 / 2**20, 1),
            "avail_ram_gb": round(after["avail_mb"] / 1024, 1),
            "commit_gb": f"{after['committed'] / 2**30:.1f}/{after['limit'] / 2**30:.1f}",
            "strata_private_gb": round(after["strata_private"] / 2**30, 1),
            "strata_ws_gb": round(after["strata_ws"] / 2**30, 1),
        })
    except Exception as exc:  # noqa: BLE001 - record the failure and still stop the server
        row["error"] = str(exc)
    finally:
        subprocess.run([STRATA, "launch", "--stop", "--preset", "IQ1_M 32K"], env=ENV, capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
        time.sleep(10)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budgets", type=float, nargs="+", required=True)
    parser.add_argument("--label", default="sweep")
    args = parser.parse_args()
    out = HERE / "results" / f"{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out.parent.mkdir(exist_ok=True)
    for budget in args.budgets:
        row = run_budget(budget)
        print(json.dumps(row), flush=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    print(json.dumps({"results": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
