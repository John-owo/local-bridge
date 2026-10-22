# local-bridge operations

Measured on the reference machine (2026-10-23): Windows 11, RX 9070 XT 16 GB, i5-12600K, 40 GB RAM,
models on XPG GAMMIX S70 Pro (E:), system drive Intel 660p QLC (C:), Strata 0.2.2 / engine 0.1.47 (HIP,
gfx1201), OpenCode 1.18.30.

## Server

| Action | Command |
|---|---|
| Status (and other GPU servers) | `python BRIDGE server status` |
| Start with the configured `launch_args` | `python BRIDGE server start` |
| Stop | `python BRIDGE server stop` (or `strata launch --stop`) |
| Live view | `strata monitor --url http://127.0.0.1:8080` |
| Engine log | `%LOCALAPPDATA%\Strata\launch\<preset>.engine.log` |

Only one GPU model server at a time. The old Qwen3.8-27B `llama-server` (E:\LLM\start-server.ps1, port 1234)
and Strata both want most of the 16 GB card. `server start` refuses while llama-server, Ollama or LM Studio runs.

This PC has a leftover `nvidia-smi.exe`, which makes Strata pick a CUDA engine. The user environment variable
`STRATA_ENGINE_BACKEND=hip` forces the AMD engine; `STRATA_COMPONENTS_DIR=E:\Strata\components` keeps the
engine on E:.

## Memory: the limit that decides speed

Strata keeps the model's experts in three tiers: VRAM (about 2,900 hot experts, 5.6 GiB), page-locked RAM,
and the model file on the SSD. Anything not in the first two is read from the SSD on every token, unbuffered.

The RAM tier is capped by the **Windows commit limit** (RAM + page file), not by free RAM. With a 12 GB
system-managed page file the limit is 51.7 GiB; desktop apps and drivers already commit 30-35 GiB, so Strata got
2.4 GiB of RAM for experts even though 25 GiB of RAM was physically free. Result: decode about 10-12 tok/s instead
of the 30-44 tok/s reported for this card. The full in-RAM arena (23.4 GiB) also failed to start: commit ran out
and the GPU kernel launch failed.

Ways to raise the RAM tier, in order of preference:

1. Close heavy background apps while the local model works (each GB of commit freed is a GB of experts).
2. Raise the commit limit with a larger page file, preferably on E: (S70 Pro). A page file only sets the limit;
   Windows writes to it only when physical RAM runs out. Needs administrator rights and a reboot (user's decision).
3. Then raise `--resident-budget-gib` in `launch_args` (currently 12; the engine clamps it to what commit allows
   and logs `FileExpertSource: RAM budget ...`).

## Benchmarks (bench/run_bench.py, automatic grading)

| Backend | review | bugfix | testgen | explore | hard | mean | typical task time |
|---|---|---|---|---|---|---|---|
| Qwen3.8-27B UD-IQ3_XXS, llama.cpp Vulkan (2 reps) | 1.00 | 1.00 | 0.90 | 1.00 | 1.00 | 0.98 | 30-170 s |
| Strata Coder IQ1_M, 2.4 GiB RAM tier (1 rep) | 0.67 | 1.00 | 1.00 | 1.00 | 1.00 | 0.93 | 140-415 s |

Per task, Strata in the commit-limited mode was 3-8x slower than the 27B dense model (hard: 325 s vs 64-77 s).
Quality was comparable; the one miss was a review that did not flag negative quantities.

## SSD wear

Wear comes from writes. Reads (even terabytes) do not meaningfully wear NAND.

| Source | Drive | Amount |
|---|---|---|
| Model download + one-time conversion (Coder IQ1_M, draft layer, pack) | E: | about 63 GB once; again only for a new model or size |
| Expert reads while generating (low-RAM mode) | E: | about 2 GB/s, roughly 120 GB per busy minute: **reads, no wear** |
| Runtime writes by Strata (logs, settings) | C:/E: | KB-MB |
| Bridge runs (snapshot, events, result) | E: | about 10 KB-5 MB per task |
| OpenCode worker sessions (SQLite) | E: | MB per day |
| Session/slot save files (`--slot-save-path`) | - | off; would be about 1.2 GB per long conversation |
| Page file when commit/RAM run out | C: | 1.8 GB during one failed full-RAM start; avoid by not over-committing |

Endurance: S70 Pro 1 TB is rated 740 TBW; the 660p 1 TB (C:) about 200 TBW. The model workload adds essentially
no writes after the download. The main risk is page-file traffic on C: under memory pressure.

Separately, the C: drive showed a constant background write load of about 20-24 MB/s (1.2 GB/min) that was
present before Strata started and is not attributed to any user process. Identify it with Resource Monitor
(Disk tab, sort by Write B/sec); at that rate it is about 1.7 TB/day, which matters for a 200 TBW drive.

## Files and folders

| Path | Contents |
|---|---|
| `E:\Strata\` | Strata terminal, engine components, install records |
| `E:\Strata-data\` | Model shards (55 GB), MTP draft layer (6.5 GB), converted pack (1.4 GB) |
| `E:\LLM\local-bridge-runs\` | One folder per delegated task; safe to prune old ones |
| `E:\LLM\local-bridge-state\` | Worker OpenCode config and session database |
| `%APPDATA%\Strata\` , `%LOCALAPPDATA%\Strata\launch` | Strata settings, presets, launch logs (small) |

## Maintenance

- Update Strata: `strata update --check`, then `strata update` (keeps the old engine until a test start passes).
- OpenCode: stay on 1.18.x. OpenCode 2 renames permission keys (`bash` -> `shell`, `permission` -> `permissions`),
  which this helper does not emit yet. Re-run the unit tests and one `bench` case after any upgrade.
- Prune runs: delete folders under `E:\LLM\local-bridge-runs\` older than you need for evidence.
- Re-run `python bench\run_bench.py --label <name>` after changing model, quantization or launch arguments.
