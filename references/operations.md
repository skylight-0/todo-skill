# 连接与命令

以下 `todo.py` 指本技能目录下的 `scripts/todo.py`。脚本只依赖 Python 3.10+ 标准库，命令输出 JSON；全局参数放在子命令之前。完整参数以 `--help` 和各子命令的 `--help` 为准。

## 连接

配置来源按以下顺序选择，每次使用一整组地址与密钥，避免混用不同服务的凭据：

1. `--config PATH` 或 `TODO_SYNC_CONFIG` 指向 JSON：`{"serverUrl":"https://your-service","token":"个人密钥"}`。
2. 同时设置 `TODO_SYNC_URL` 和 `TODO_SYNC_TOKEN`。也兼容后端进程环境 `SYNC_TOKEN`，地址取 `TODO_SYNC_URL`，未设置时使用 `http://127.0.0.1:8787`。
3. 本机 ToolBox 的 `%APPDATA%/com.zhao.toolbox/clipboard.sqlite` 中 `app_settings.todo_sync_config_v1`，需要已启用同步。可以用 `--toolbox-db PATH` 或 `TODO_TOOLBOX_DB` 指定数据库；始终只读。

`--url` 仅覆盖已选配置的地址，适合显式连接同一密钥对应的测试地址。正常使用原保存地址。服务根路径后由脚本追加 `/api/v1/todo/sync`；不要把该接口路径填进服务地址。HTTP 重定向会被拒绝，遇到反向代理跳转时改用最终根地址。

```powershell
python todo.py doctor
python todo.py --config C:/private/todo-sync.json doctor
python todo.py --timezone +08:00 list --date today
```

没有配置时，先让用户在 ToolBox「待办 → 云端同步」或 Flutter「更多操作 → 云端同步」接入正在使用的后端，也可提供上面的环境变量或私有 JSON 配置。Flutter 仅本地保存、尚未同步的数据不会出现在云端。不要通过创建空数据库替代用户现有数据。

如果用户要求在本机启动已有后端，在 ToolBox 根目录使用 `cargo run --manifest-path sync-service/Cargo.toml`；启动参数和备份流程查阅该项目的 `sync-service/README.md`。服务需要 `SYNC_TOKEN`、`SYNC_BIND` 和 `SYNC_DATABASE`，沿用原数据空间的密钥及数据库路径。单纯管理任务不需要重新构建或重启服务。

## 查询

```powershell
python todo.py list
python todo.py list --status active --date today
python todo.py list --date overdue
python todo.py list --date 2026-11-06 --group "工作"
python todo.py list --query "报告" --status all
python todo.py list --trash --status all
python todo.py show TASK_ID
python todo.py show TASK_ID --trash
python todo.py groups
python todo.py history TASK_ID
```

`list` 默认返回全部未删除任务；`--status` 可选 `all|active|completed`。`--date` 可选 `all|today|tomorrow|week|overdue|unscheduled|YYYY-MM-DD`，`week` 与客户端一样表示从今天零点起的未来七天。`--group root` 表示独立任务。`--query` 对标题与描述做不区分大小写的包含匹配。

结果中的 `fields` 是原始服务端字段，`dueLocal` 是本次配置时区下的日期时间，`displayCompleted` 用于与客户端一致的筛选。`history` 包含 `occurrences`、`recurrenceHistory`、`currentOccurrenceId`、`advancedFrom` 和当前规则，不展开无限期计划。

## 任务

```powershell
python todo.py add "提交报告" --description "附上本月数据" --priority high --due "2026-11-06T18:00:00+08:00" --reminder on --group "工作"
python todo.py update TASK_ID --text "提交最终报告" --priority urgent
python todo.py update TASK_ID --due 2026-11-07 --reminder off
python todo.py update TASK_ID --group root
python todo.py update TASK_ID --due none
python todo.py done TASK_ID
python todo.py reopen TASK_ID
python todo.py delete TASK_ID
python todo.py restore TASK_ID
python todo.py restore TASK_ID --group "工作"
```

优先级为 `low|normal|high|urgent`。日期接受 ISO 8601、Unix 毫秒或仅日期；无偏移的时间按 `--timezone` 解释，仅日期为 `23:59`。默认不打开提醒，要求提醒时显式加 `--reminder on` 并设置截止时间。移除截止时间会关闭提醒。

新任务默认放在所在分组或独立任务区最前；可用 `--position NUMBER` 显式设置排序。任务引用支持完整 ID、唯一 ID 前缀或完全匹配标题；分组同理。优先使用查询得到的完整 ID，标题重复时不会猜测。恢复沿用保留的字段；原分组不存在时服务端将任务恢复到独立任务区。

## 重复任务与记录

```powershell
python todo.py add "浇花" --due "2026-11-06T09:00:00+08:00" --repeat day --interval 3 --basis completion --reminder on
python todo.py add "月报" --due "2026-11-30T18:00:00+08:00" --repeat month --interval 1 --basis schedule --count 12
python todo.py update TASK_ID --repeat week --interval 1 --due "2026-11-09T09:00:00+08:00" --end 2027-01-31
python todo.py update TASK_ID --repeat none
python todo.py done TASK_ID
python todo.py skip TASK_ID
python todo.py reopen TASK_ID
python todo.py history TASK_ID
python todo.py record TASK_ID --status completed --scheduled "2026-11-06T09:00:00+08:00" --rule-id RULE_ID --completed-at "2026-11-06T10:00:00+08:00"
python todo.py record TASK_ID --status open --occurrence-id OCCURRENCE_ID --replace
```

`--repeat` 支持 `hour|day|week|month|none`。`--basis schedule` 固定计划，`completion` 从完成时间计算下一次；默认固定计划。`--interval` 为 1–999，`--count` 为 1–9999 次完成，`--end` 为结束日期或时刻。修改规则需重新指定完整意图，未指定的选项恢复默认（间隔 1、固定计划、无结束限制），而不是继承旧规则的局部设置。

重复操作前检查 `/healthz` 的 `recurrence-v1` 能力；缺失时先更新部署。普通一次性任务不要求此能力。

`done` 对已经显示完成的当前周期返回无变更，避免反复执行提前完成下一次。要明确操作未来或历史那次，使用 `record` 并选择 occurrence。`reopen` 撤销界面当前显示的完成记录；更早的历史通过 `record --status open --replace` 指定。

`record` 优先使用 `history` 返回的 `--occurrence-id`，已有记录自动带回原规则和计划时间。补记未保存的固定计划时使用 `--scheduled` 和 `--rule-id`；规则可来自当前或归档，服务端验证是否匹配原计划。完成后间隔只允许当前、已记录或归档时尚未完成的那次；归档待完成的 ID 和时间见 `recurrenceHistory` 的 `pendingId`、`pendingAt`。首次完成默认 first-wins，修改已保存记录时加 `--replace`。完成时刻不能晚于当前时刻。

## 分组

```powershell
python todo.py group-add "工作" --color "#3366CC"
python todo.py group-update GROUP_ID --name "工作事项" --collapsed on
python todo.py group-delete GROUP_ID
python todo.py groups --trash
python todo.py group-restore GROUP_ID
```

颜色接受 `#RRGGBB`、`#AARRGGBB` 或无符号 32 位整数。删除分组保留任务，恢复分组不会自动把任务移回去。

## 未确认操作

```powershell
python todo.py pending
python todo.py retry
```

本机状态默认存放在 `%LOCALAPPDATA%/Codex/todo-skill/`（其他系统为 `~/.local/state/codex/todo-skill/`），按服务地址隔离；可用 `--state-dir PATH` 或 `TODO_SKILL_STATE_DIR` 指定。状态只保存客户端 ID 和未确认操作，不保存密钥。脚本对同一服务加进程锁，写入前原子保存操作；网络错误、未知提交结果保留 pending，明确的客户端请求错误会清除未提交的请求。

查询不会重放 pending；下一次写操作会提示先 `retry`。重试复用客户端 ID、实体 ID 和操作 ID，所以服务端已经提交的请求不会新增重复任务或重复推进周期。如果服务的 `serverId` 改变，重试停止，需人工核对是否更换或恢复了数据库，不能盲目跨库重放。

API 已有字段级合并及软删除，但没有比较 revision 后再写的条件更新。脚本读取最新快照、只提交指定字段；并发修改同一个字段仍由服务端接受顺序决定。后端会在同步时推进到期的固定计划，因此即使只查询也可能触发正常的周期推进。
