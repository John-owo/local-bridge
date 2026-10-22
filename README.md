# local-bridge

讓 Claude Code 與 Codex 把工作派給本機 GPU 上的模型（Strata + Qwen3.8-Flash-Next，經由 OpenCode 執行）的共用技能。
設計沿用 [agy-bridge](https://github.com/John-owo/agy-bridge)：同樣的副本工作流程，只是工人從 Antigravity CLI 換成本機模型。

## 功能

- 程式碼閱讀、Repository 探索、Code Review、測試生成與小型實作交給本機模型，不消耗雲端 token。
- 每次任務使用獨立的檔案副本；CC 與 Codex 可以同時派工。
- 預設唯讀；寫入模式只能改副本，shell 指令預設全部拒絕，只開放逐字列出的指令。
- 工人看不到原始專案、網路、憑證與使用者自己的 OpenCode 設定；拒絕的動作會記錄在結果中。
- 保存結果、工具呼叫、token 用量與修改清單，由主代理檢查後整合。不會自動合併、推送或部署。

## 需求

- Windows、Python 3.10+、[OpenCode](https://opencode.ai) 1.18.x（`npm i -g opencode-ai`）。
- 本機 OpenAI 相容推論伺服器。預設為 [Strata](https://github.com/Niko1221/Strata) 在 `http://127.0.0.1:8080/v1`。

## 安裝（Windows）

先檢查安裝程式，再於 repository 目錄執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-LocalBridge.ps1 -DataRoot E:\LLM -Strata E:\Strata\terminal\current\bin\strata.cmd
```

安裝器將技能放入 `~/.agents/skills/local-bridge/`，並在 `~/.claude/skills/local-bridge/` 建立指向同一份技能的目錄連結，
再寫入 `local-bridge.config.json`（端點、資料夾、`strata launch` 參數）。已有同名技能時會停止，避免覆寫。
不修改使用者的 OpenCode、Codex 或 Claude Code 設定。

## 使用

Claude Code：

```text
/local-bridge 幫這個專案的 src/parser 補單元測試
```

Codex：

```text
$local-bridge 整理這個 repository 的架構
```

技能也可在符合條件時自動選用。完整操作規則見 [SKILL.md](local-bridge/SKILL.md)，
維運、SSD 與效能資料見 [operations.md](local-bridge/references/operations.md)。

## 測試

單元測試只使用 Python 標準函式庫，不呼叫模型：

```powershell
python -m unittest discover -s tests -v
```

端到端品質與速度測試（需要伺服器在線；`explore` 題目需要把 agy-bridge 複製到 `bench/fixtures/explore_q/agy-bridge`）：

```powershell
python bench\run_bench.py --label strata-coder --repeat 2
```

## 執行範圍

獨立副本是避免工作互相覆蓋與限制模型視野的機制，並非 OS 沙盒：被允許的指令仍以使用者權限執行。
因此不要開放可執行任意程式碼的指令（例如 `python -c`）。
