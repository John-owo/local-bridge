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

Strata keeps the model's experts in three tiers: VRAM (about 3,000 hot experts, 5.6 GiB), page-locked RAM,
and the model file on the SSD. Anything not in the first two is read from the SSD on every token, unbuffered.

The RAM tier is capped twice:

1. **Windows commit limit** (RAM + page files). With the original 12 GB auto page file (limit 51.7 GiB) the
   desktop's 30-35 GiB of commit left Strata 2.4 GiB, whatever `--resident-budget-gib` said. Fixed 2026-10-23 by
   page files on E: and C: (32-48 GB each): limit 103.7 GiB.
2. **Physical RAM.** The engine keeps about 4 GiB of RAM free: a budget of 16 gave 12.7 GiB, 20 gave 13.0 GiB.
   The desktop itself uses about 20 GB, so 12 GiB is the practical ceiling on this 40 GB machine.

Budget sweep after the page-file change (`bench/sweep_budget.py`, one short decode + 8K-prompt test each):

| `--resident-budget-gib` (actual) | Decode tok/s | 8K prompt tok/s | E: reads | Page-file writes | RAM left |
|---|---|---|---|---|---|
| 12 before the page-file change (2.44) | 6.3 | 531 | 380 GB | 0 | - |
| 4 (4) | 12.9 | 479 | 246 GB | 0 | 12.2 GB |
| 8 (8) | 18.2 | 466 | 120 GB | 0 | 7.1 GB |
| **12 (12)** | **30.6** | 528 | 52 GB | 236 MB | 3.4 GB |
| 16 (12.7) | 31.2 | 494 | 44 GB | 43 MB | 2.5 GB |
| 20 (13.0) | 34.9 | 552 | 38 GB | 0 | 3.1 GB |

**Chosen: 12** (the `launch_args` default). It reaches the card's published speed, cuts SSD reads about 7x, and
the page-file writes it causes are small (see SSD wear). Use 8 when you run heavy apps (games, video editing)
next to the model: zero paging, about 60% of the speed.

## Benchmarks (bench/run_bench.py, automatic grading)

| Backend | review | bugfix | testgen | explore | hard | mean | task time |
|---|---|---|---|---|---|---|---|
| Qwen3.8-27B UD-IQ3_XXS, llama.cpp Vulkan (2 reps) | 1.00 | 1.00 | 0.90 | 1.00 | 1.00 | 0.98 | 29-171 s |
| Strata Coder IQ1_M, 2.4 GiB RAM tier (1 rep) | 0.67 | 1.00 | 1.00 | 1.00 | 1.00 | 0.93 | 142-415 s |
| **Strata Coder IQ1_M, 12 GiB RAM tier (2 reps)** | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | **1.00** | 61-443 s |

With the 12 GiB tier Strata scored full marks; end-to-end output ran at about 20-23 tok/s. Tasks still take
about 2-3x the 27B's time, mostly because Coder writes 2-3x more tokens. The worker prompt now asks for concise,
evidence-first answers: review output dropped from about 2,700 to 1,270 tokens (76 s), quality unchanged.

The first 12 GiB run exposed a real tool-calling failure: the prompt named a `list` tool that OpenCode 1.18 does
not declare (read mode declares `read`, `grep`, `glob`, `todowrite`), so the model emitted an undeclared call
that came back as text. The prompt was fixed and the helper now fails runs whose answer is an unparsed tool call.

## SSD wear

Wear comes from writes. Reads (even terabytes) do not meaningfully wear NAND; they cost heat and speed.

Sustained measurement: 45 minutes of continuous benchmark load at the 12 GiB tier (`bench/io_sample.py`).

| What | Drive | Measured | Wear impact |
|---|---|---|---|
| Model download + one-time conversion | E: | about 63 GB once | once per model; 0.01% of 740 TBW |
| Expert reads while generating | E: | 1,488 GB in 45 min (was 3,953 GB in 40 min at 2.4 GiB) | none (reads) |
| All E: writes (bridge runs, worker sessions, page file share) | E: | 0.65 GB in 45 min | negligible |
| Page-file writes (C: + E: combined) | C:/E: | 1.06 GB in 45 min; bursts of about 0.4-0.5 GB when the model loads or a large job starts | small, see below |
| Session/slot save files (`--slot-save-path`) | - | off | would be about 1.2 GB per long conversation |

Worst case, eight hours a day of non-stop delegated work at the 12 GiB tier: about 11 GB/day of page-file writes
plus about 7 GB/day of other E: writes, roughly 4-7 TB/year. That is under 1% per year of the S70 Pro's 740 TBW,
or about 2-3% per year if all paging landed on the C: 660p (200 TBW). Real use (minutes of delegation, not hours)
is far lower, and with the server stopped there is no paging from the model at all.

To keep paging off the C: QLC drive, set the C: page file small (4-8 GB) and keep the large one on E:; Windows
writes to whichever page file is less busy, so two equal page files share the writes.

**The largest write load on this PC is not the model.** C: shows a constant 20-24 MB/s write stream (61.6 GB in
these 45 minutes; about 1.2-1.4 GB/min) that was present before Strata ran, survived a reboot, and is not
attributed to any user process. At that rate it is about 1.7-2 TB/day of uptime, which would exhaust the 660p's
200 TBW in months. Identify it with Resource Monitor (Disk tab, sort by Write B/sec, check the file names).

## Resource use while serving (Coder IQ1_M, 32K context, 12 GiB RAM tier)

| Resource | Use |
|---|---|
| VRAM | 11.4 GB for strata.exe (14.3 GB with the desktop); about 3,000 experts cached |
| RAM | about 22 GB committed, 15 GB working set (12 GiB of it page-locked experts); about 3 GB left free |
| CPU | 9 expert-pool workers, one per physical core |
| Load time | about 40 s; first start after download about 2 min |

Stop the server when you are done (`python BRIDGE server stop`): it holds about 15 GB of RAM and most of the GPU.

## Alternative backend: Qwen3.8-27B on llama-server

The bridge talks to any OpenAI-compatible server. To use the dense 27B already on E: (about 2-3x shorter task times,
slightly lower benchmark score), stop Strata, run `E:\LLM\start-server.ps1`, and point the config at it:

```json
{"endpoint": "http://127.0.0.1:1234/v1", "model_id": "qwen3.8-27b", "context": 32768, "output": 4096}
```

Keep the other keys. `server start/stop` only drive Strata; manage llama-server yourself. Note that this
llama-server (Vulkan) commits about 22 GB of system RAM besides its 11 GB of VRAM, so it also strains the commit
limit; it grew the C: page file to 16.5 GB during testing.

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
