#!/usr/bin/env python3
"""Sample per-drive disk traffic and paging with Windows performance counters (no admin needed).

Usage: python io_sample.py --seconds 600 --out io.csv
Prints totals in GB per drive and pagefile activity when done (or on Ctrl+C).
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys

COUNTERS = [
    r"\PhysicalDisk(*)\Disk Read Bytes/sec",
    r"\PhysicalDisk(*)\Disk Write Bytes/sec",
    r"\Memory\Pages Output/sec",     # pages written to the pagefile
    r"\Memory\Pages Input/sec",      # hard faults read back (pagefile or mapped files)
    r"\Memory\Available MBytes",
    r"\Memory\Committed Bytes",
    r"\Paging File(_Total)\% Usage",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    cmd = ["typeperf", *COUNTERS, "-si", "1", "-sc", str(args.seconds), "-f", "CSV", "-o", args.out, "-y"]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except KeyboardInterrupt:
        pass
    with open(args.out, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    header, data = rows[0], [r for r in rows[1:] if len(r) == len(rows[0])]
    totals: dict[str, float] = {}
    peaks: dict[str, float] = {}
    mins: dict[str, float] = {}
    for i, name in enumerate(header[1:], start=1):
        values = []
        for r in data:
            try:
                values.append(float(r[i]))
            except ValueError:
                pass
        if not values:
            continue
        short = name.split("\\", 3)[-1]
        totals[short] = sum(values)  # 1 s samples: rate sum == total
        peaks[short] = max(values)
        mins[short] = min(values)
    gb = 1024 ** 3
    summary = {"seconds": len(data), "drives": {}, "paging": {}}
    for key, total in totals.items():
        if "Bytes/sec" in key and "_Total" not in key:
            drive = key.split(")")[0].split("(")[1]
            kind = "read_gb" if "Read" in key else "write_gb"
            summary["drives"].setdefault(drive, {})[kind] = round(total / gb, 3)
            summary["drives"][drive][kind.replace("_gb", "_peak_mb_s")] = round(peaks[key] / 1024 ** 2, 1)
    summary["paging"] = {
        "pages_output_total": int(totals.get("Memory\\Pages Output/sec", 0)),
        "pagefile_write_gb_est": round(totals.get("Memory\\Pages Output/sec", 0) * 4096 / gb, 3),
        "pages_input_total": int(totals.get("Memory\\Pages Input/sec", 0)),
        "available_mb_min": int(mins.get("Memory\\Available MBytes", 0)),
        "committed_gb_peak": round(peaks.get("Memory\\Committed Bytes", 0) / gb, 1),
        "pagefile_usage_pct_peak": round(peaks.get("Paging File(_Total)\\% Usage", 0), 1),
    }
    json.dump(summary, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
