# codex-supervisor

讓**原本開著的互動 Codex 視窗**在 usage/rate limit 後自動繼續同一 session。
Supervisor 可由 user-level systemd 在登入工作階段背景監控；額度重置後用 Codex 原生的 `queue` 指令送出 `continue`。
不關閉 Codex、不啟動第二個 session writer，也不接手 coding task。

## 安裝

Linux、Python 3.12+，以及支援 `codex queue --thread` 的 Codex CLI。
目前實測版本為 **0.153.4**。使用原有 Codex 登入，不讀取 credential。

```bash
git clone https://github.com/ChingFuHan/codex-supervisor.git
cd codex-supervisor
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/codex-supervisor --help
```

也可用 `pipx install .`，讓 `codex-supervisor` 加入 PATH。
以下範例假設已加入 PATH；使用 venv 時換成 `.venv/bin/codex-supervisor`。

## 保留正在使用的 Codex 視窗

1. 原本的 Codex 視窗保持開啟。在 Codex `/status` 查看目前的 session ID。
2. 另開 terminal，指定**目前視窗**的 UUID：

```bash
codex-supervisor adopt <session-uuid>
```

`adopt` 只監控，不會立即送訊息；最新回合確認為 rate limit 時才安排續跑。
若額度已恢復、Codex 正在等你輸入，而你要立即送一次 `continue`：

```bash
codex-supervisor adopt <session-uuid> --continue-now
```

之後仍會持續監控。可自訂訊息與檢查間隔：

```bash
codex-supervisor adopt <session-uuid> --prompt "繼續完成之前的任務" --interval 5
```

請勿拿過去紀錄的 UUID 代替目前視窗的 UUID。找不到 active writer 時，工具會停止並提示；
它不會幫你關閉任何視窗或重新開啟 Codex。新任務直接在原 terminal 使用 `codex` 啟動，再用 `adopt` 監控。

## 同時監控本機互動 sessions

```bash
codex-supervisor watch
```

每 5 秒掃描未封存的本機 root CLI sessions，只追蹤有 active writer 的 session。
每個 session 的等待互不阻塞；可能同時送出多個到期的續跑訊息。
若只想操作一個工作，使用 `adopt <session-uuid>`。

## 全域背景監控（不指定 session）

安裝 user-level service；這只影響目前使用者的 systemd，不需要 root：

```bash
mkdir -p ~/.config/systemd/user
cp examples/codex-supervisor-watch.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER"
systemctl --user enable --now codex-supervisor-watch.service
systemctl --user status codex-supervisor-watch.service
```

`systemctl --user enable --now` 啟用並立即啟動目前 user 的 service；
`loginctl enable-linger "$USER"` 讓 user systemd 在開機後、尚未登入時也能啟動並維持此 service。
若不需要開機前登入即可執行，必須保留 linger 啟用。可用以下指令驗證：

```bash
loginctl show-user "$USER" -p Linger
systemctl --user is-enabled codex-supervisor-watch.service
systemctl --user is-active codex-supervisor-watch.service
```

此 service 每 5 秒掃描所有仍有 active writer 的互動 Codex sessions。查看 log：

```bash
journalctl --user -u codex-supervisor-watch.service -f
```

範例 service 針對本機的 NVM Codex 路徑 `v22.23.2`；升級 Node 或 Codex 後，請在
`~/.config/systemd/user/codex-supervisor-watch.service` 更新 `CODEX_SUPERVISOR_CODEX_PATH`，
再執行 `systemctl --user daemon-reload` 與 `systemctl --user restart codex-supervisor-watch.service`。
同一 state directory 只允許一個 watcher；重複啟動會明確退出。

停止或重啟 watcher 只停止監控，會保留 rate-limit deadline、queue ID 和去重紀錄。
明確執行 `cancel` 或 Codex 回合被使用者中斷才會取消 job。

## Rate limit 後的行為

1. 唯讀追蹤 rollout 的 lifecycle，以及目前 turn 已完成的 `Reasoning`、`CommandExecution`、`Extension`、`AgentMessage` 項目。
2. 最新完成回合的 error 確認為 rate limit，才安排下一次探測。
3. 支援本地時區的時刻、完整日期、ISO timestamp，以及相對等待時間。
4. 原互動 Codex 保持開啟；Supervisor 保存狀態並等待。
5. 每次確認 rate limit 後固定等待 5 分鐘；到期再確認回合未被手動續跑／中斷取代，執行 `codex queue --thread <uuid> --message continue`。
6. 分別紀錄「已入列」、「新回合開始」、「已有工作項目進度」、「回合完成」。入列成功或新回合開始都不等於任務已經工作。
7. `codex-supervisor status` 會顯示 continuation outcome、turn、最後進度時間、項目數與類型；`scheduled_resume` 是實際 5 分鐘探測時間，`parsed_reset` 只是 Codex 提供的參考時間。
8. 再次遇到 rate limit 就重複；限額重試次數無上限。正常回合完成後不額外送 `continue`，但繼續監控之後的回合。

Codex 提供的 reset 時間不會直接決定互動 session 的重試時間；它只保存供診斷。每次再次 rate limit 都從新的錯誤事件重新等待 5 分鐘，仍受事件去重保護。
批次 `run`／`resume` 模式仍使用原本的 fallback wait 與退避設定。
舊的 `goals_1.sqlite` 中 `usage_limited` 不代表目前額度，**不作為觸發依據**。

環境變數：

| 變數 | 預設 | 用途 |
| --- | --- | --- |
| `CODEX_SUPERVISOR_FALLBACK_WAIT_MINUTES` | `30` | 無 reset 時間的初始等待 |
| `CODEX_SUPERVISOR_MAX_WAIT_SECONDS` | `14400` | 無 reset 時間的退避上限 |
| `CODEX_SUPERVISOR_STATE_DIR` | XDG state 下的 `codex-supervisor` | Supervisor 狀態目錄 |
| `CODEX_SUPERVISOR_CODEX_PATH` | PATH 中的 `codex` | Codex executable |
| `CODEX_HOME` | `~/.codex` | 與被監控 Codex 相同的 home |

## Status、logs 與取消

```bash
codex-supervisor jobs
codex-supervisor status
codex-supervisor cancel sv-interactive-<session-uuid>
```

狀態預設在 `~/.local/state/codex-supervisor/jobs/<job-id>.json`；
互動監控的事件 log 在 `~/.local/state/codex-supervisor/logs/<job-id>.log`，並同步顯示於監控 terminal。
遵守 `XDG_STATE_HOME`。Log 包含 session、事件、時間、reset、queue ID 與分類，不複製使用者的完整對話。

在 **Supervisor terminal** 按 Ctrl+C，或停止 user service，會停止監控但保留待處理 job；`cancel` 指令才會停止後續自動送訊息，**不向 Codex 或 shell 發訊號**。
Codex 回合中的使用者中斷也會取消該 session 的監控。重新 `adopt` 可重新啟用。

已提交到 Codex queue 的訊息不會被撤回，仍可能執行；取消只阻止後續提交。
若送出後 120 秒沒有新 lifecycle event，狀態顯示 `failed`／`QUEUE_UNCONFIRMED`，不會反覆重送。
先檢查原視窗的佇列、approval 或輸入狀態。不要靠殺程序來排除問題。

## 保留的非互動模式

批次任務仍可使用既有的 `run`／`resume`，這條路徑會啟動 `exec` 子程序：

```bash
codex-supervisor run -- codex exec --json "implement the feature plan"
codex-supervisor resume <job-id>
```

這與保留既有 TUI 的 `adopt`／`watch` 不同。不要對已有互動 writer 的 session 同時跑 `exec resume`。
批次模式保留有限次數的 transient/unknown failure 重試；互動模式只自動接續確認的 rate limit，
其他錯誤留在原 TUI 供使用者處理。

## 限制

- 原 Codex 必須保持開啟並持有 writer。未啟用 user linger 時，user service 依賴登入的 user systemd 工作階段；啟用 linger 後，可在未登入時由 user systemd 啟動並維持執行。
- 本機 Linux 專用：唯讀使用 Codex 的 `state_5.sqlite`、rollout JSONL、`thread-writer-locks` 與 `/proc/locks`。
  這些內部格式可能改變；不修改 Codex database 或鎖檔。Remote／ephemeral sessions 不在此模式的支援範圍。
- queue 已在真實 TUI 驗證；不保證跨版本相容。沒有 `queue` 指令時明確報錯，不偷偷切回 `exec resume`。
- 送出與新回合之間若發生 crash、timeout 或使用者操作，採取不重複送出的保守處理。必要時由使用者檢查 queue。
- 多個 Supervisor 使用相同 state 目錄時，以檔案鎖和持久化提交紀錄防止重複提交。不要用不同 state 目錄同時管理同一 session。
- 原 Codex 的 approval、sandbox、模型及工作目錄由它自己保留；Supervisor 不代答 approval。
- `scheduler.py` 是保留的 legacy systemd/sleep backend；互動監控使用單一常駐 user service，不建立 timer。

## 測試

```bash
python3 -m pytest -q -m "not slow and not smoke"

# 真實互動測試：僅使用臨時目錄，不故意耗盡額度
CODEX_SUPERVISOR_RUN_SMOKE=1 python3 -m pytest -q -s tests/test_interactive_smoke.py

# 保留的批次／legacy scheduler 測試
python3 -m pytest -q tests/test_smoke.py
python3 -m pytest -q tests/test_systemd_integration.py
```

互動 smoke test 注入的是**獨立 fixture** 的假限額事件；實際透過 Supervisor 送出 `continue`，
驗證原 Codex PID 仍存活、同一 session 出現第二個完成回合，以及指定回答。

## Uninstall / cleanup

先停用 user service；原 Codex 不受影響：

```bash
systemctl --user disable --now codex-supervisor-watch.service
rm ~/.config/systemd/user/codex-supervisor-watch.service
systemctl --user daemon-reload
```

若也不再需要該 user 的 user services 在未登入時執行，可另外停用 linger：

```bash
loginctl disable-linger "$USER"
```

若使用 pipx：`pipx uninstall codex-supervisor`。若使用 venv：`.venv/bin/pip uninstall codex-supervisor`。
確認不再需要紀錄後，可刪除自己設定的 Supervisor state 目錄；不需移除任何 Codex 檔案。
Legacy scheduler 的清理方式見 [architecture](docs/architecture.md)。
