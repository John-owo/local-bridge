# local-bridge operations

Reference machine (measured 2026-10-23): Windows 11, RX 9070 XT 16 GB, i5-12600K, 40 GB RAM, models on XPG GAMMIX
S70 Pro (E:), system drive Intel 660p QLC (C:), page files 32-48 GB on C: and E:, OpenCode 1.18.30.

## Profiles and server control

`local-bridge.config.json` holds named backend profiles; `"profile"` is the default, `--profile NAME` or
`LOCAL_BRIDGE_PROFILE` overrides it per command.

| Profile | Model | Server | Endpoint | Status |
|---|---|---|---|---|
| `qwen27b` (default) | Qwen3.8-27B UD-IQ3_XXS (10.9 GB) | llama-server from LM Studio's Vulkan runtime 2.37.0 | `http://127.0.0.1:1234/v1` | installed |
| `flash-next` | Qwen3.8-Flash-Next Coder IQ1_M via Strata | `strata launch` | `http://127.0.0.1:8080/v1` | **removed 2026-10-23 to free E:**; reinstall first (see below) |

| Action | Command |
|---|---|
| Status (and any GPU server running) | `python BRIDGE server status` |
| Start / stop the active profile's server | `python BRIDGE server start` / `python BRIDGE server stop` |
| Use another profile once | `python BRIDGE --profile flash-next server start` |
| Server log (process profiles) | `<state_dir>\server-<profile>.log` |

`server start` launches llama-server detached (it survives the calling shell) and records its pid in
`<state_dir>\server-<profile>.json`; `server stop` kills only that recorded pid after checking the image name.
Only one GPU model server at a time: `server start` refuses while llama-server, Strata, Ollama or LM Studio runs.
Do not run `E:\LLM\start-server.ps1` and the bridge's server together; both use port 1234.

## qwen27b: memory tuning

The old `start-server.ps1` flags let llama-server grow under agent load to 20.2 GB committed / 17.8 GB resident,
leaving 2.7 GB of RAM free. Measured with the same benchmark load (`bench/llama_mem.py`, `load_probe`):

| Flags (on top of start-server.ps1) | Committed | Resident RAM | Free RAM (min) | VRAM | Notes |
|---|---|---|---|---|---|
| none | 20.2 GB | 17.8 GB | 2.7 GB | 11.4 GB | 4 slots share one 32K KV cache ("failed to find free space" under load) |
| `--no-mmap --cache-ram 0` | 19.1 | 7.2 | 13.1 | 11.4 | |
| **`--load-mode none --cache-ram 0 -np 1`** | **16.2** | **5.7** | **14.8** | **10.0** | the profile default |
| ROCm 2.41 runtime, no flags (idle) | 10.7 | 10.2 | 8.3 | 10.8 | generation 22 vs 31 tok/s: not used |

What each flag removes: `--load-mode none` (the non-deprecated form of `--no-mmap`) reads the weights instead of
mapping the file, so the 11 GB model no longer sits in RAM as file cache after it is copied to VRAM;
`--cache-ram 0` disables the host prompt cache (default 8 GiB); `-np 1` drops three idle slots' host buffers. Speed is
unchanged (about 27-31 tok/s generation, 880-940 tok/s prompt reading). With one slot, concurrent dispatches queue.

`--chat-template-kwargs '{"enable_thinking":false}'` logs a deprecation warning (use `--reasoning off`); it is kept
because it is the switch verified to disable this model's thinking. Re-test before changing it.

## Benchmarks (bench/run_bench.py, automatic grading)

| Backend | review | bugfix | testgen | explore | hard | mean | task time |
|---|---|---|---|---|---|---|---|
| 27B, original flags (2 reps, 2026-10-22) | 1.00 | 1.00 | 0.90 | 1.00 | 1.00 | 0.98 | 29-171 s |
| **27B, tuned profile (2 reps)** | 0.67* | 1.00 | 1.00 | 1.00 | 1.00 | 0.93 | 40-497 s |
| Flash Next Coder, 12 GiB RAM tier (2 reps) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 61-443 s |

\* The review miss is the same ambiguous item (`remove()` accepts negative quantities). It appeared after the worker
prompt started asking for concise answers; the prompt now says concise must not mean selective, and three reruns
caught it 2 of 3 times (about 60 s each). Single benchmark runs vary: treat differences under one case as noise.

Choice: 27B is the default because tasks finish 2-3x sooner than on Flash Next at similar quality, it is stable
(mature llama.cpp) and its VRAM-resident model barely touches the SSD. Switch to Flash Next if the 27B keeps failing
real tasks.

Bridge fixes found by these runs: the worker prompt no longer names OpenCode's undeclared `list` tool; answers keep
every step's text (a model wrote its answer, made one more tool call and closed with "findings stand as above");
runs that hit the step cap (one 27B review re-read the same file 74 times) end as `FAILED_STEP_LIMIT` instead of
`SUCCESS`; an unparsed tool call as the answer is `FAILED_OUTPUT`.

## SSD wear

Wear comes from writes; reads cost only heat and speed.

| What | qwen27b | flash-next (12 GiB tier) |
|---|---|---|
| Model files | 10.9 GB, already on E: | about 63 GB download (+ conversion) per install |
| Reads while generating | almost none (model in VRAM) | about 33 GB per busy minute from E: |
| Page-file writes | none expected: 14.8 GB RAM stays free | about 1 GB per 45 min of continuous load |
| Bridge runs + worker sessions | KB-MB per task on E: | same |

Even eight hours a day of continuous delegation stays under 1% a year of the S70 Pro's 740 TBW.

**The largest write load on this PC is not the model.** C: shows a constant 20-24 MB/s write stream (about 1.2-1.4
GB per minute) that predates the model work, survives reboots and is not attributed to any user process: about
1.7-2 TB per day of uptime against the 660p's 200 TBW. Find it with Resource Monitor (Disk tab, sort by Write B/sec).
To keep paging off the C: QLC drive, make the C: page file small (4-8 GB) and keep the large one on E:; with two
equal page files Windows spreads the writes across both.

## Flash Next (Strata) notes, for a reinstall

Removed 2026-10-23 (63.6 GB freed on E:). To reinstall: Strata installer with `STRATA_DATA_DIR=E:\Strata`, user env
`STRATA_ENGINE_BACKEND=hip` (a leftover `nvidia-smi.exe` makes it pick CUDA) and `STRATA_COMPONENTS_DIR`, then
`strata launch --family coder --model iq1_m --data-dir E:\Strata-data ...` (about 64 GB download), then
`python BRIDGE --profile flash-next server start`.

- Its speed depends on the RAM tier of experts. The tier is capped by the Windows commit limit (it was 2.4 GiB with
  the old 12 GB page file: 6 tok/s) and by physical RAM (the engine keeps about 4 GiB free).
- Budget sweep (`bench/sweep_budget.py`) with page files enlarged: 4 GiB 12.9 tok/s, 8 GiB 18.2, 12 GiB 30.6 (the
  profile default), 16/20 GiB clamp to about 13 GiB, 31-35 tok/s. Use 8 next to heavy apps: no paging.
- It holds about 11.4 GB VRAM and about 22 GB committed / 15 GB resident RAM while serving.

## Files and folders

| Path | Contents |
|---|---|
| `E:\LLM\models\unsloth\Qwen3.8-27B-GGUF\` | 27B GGUF files (IQ3_XXS used; IQ4_XS 13.3 GB unused) |
| `E:\LLM\local-bridge-runs\` | One folder per delegated task; safe to prune old ones |
| `E:\LLM\local-bridge-state\` | Worker OpenCode config and sessions, server pid/log files |
| `~\.lmstudio\extensions\backends\` | llama.cpp runtimes used by the qwen27b profile (managed by LM Studio) |

## Maintenance

- LM Studio updates can add or remove runtime folders; if `server start` reports the executable missing, point the
  profile's `exe` and `path_prepend` at a current Vulkan runtime and re-run one benchmark case.
- OpenCode: stay on 1.18.x. OpenCode 2 renames permission keys (`bash` -> `shell`, `permission` -> `permissions`),
  which this helper does not emit yet. Re-run the unit tests and one `bench` case after any upgrade.
- Prune runs: delete folders under `E:\LLM\local-bridge-runs\` older than you need for evidence.
- Re-run `python bench\run_bench.py --label <name>` after changing model, quantization or server flags.
