# Todo Skill

用于个人 Todo 应用的 Codex 技能。通过 ToolBox 同步服务操作任务，让 Flutter Todo 与 ToolBox 在下次同步时收到相同变更。

支持任务增删改查、分组与移动、截止时间和提醒、垃圾箱恢复，以及重复任务的完成、跳过、撤销和历史补记。脚本仅依赖 Python 3.10+ 标准库。

## 安装

将本仓库的 `SKILL.md`、`agents/`、`references/` 和 `scripts/` 保存到个人技能目录 `~/.codex/skills/todo/`。已安装时保留原目录，先检查差异再更新。

```text
~/.codex/skills/todo/
├── SKILL.md
├── agents/openai.yaml
├── references/operations.md
└── scripts/todo.py
```

在 Codex 中调用：

```text
$todo 查看今天的待办
$todo 添加明天18点提交报告，并提醒我
$todo 把“提交报告”标记完成
```

## 连接与命令行

需要已经部署的 [ToolBox 同步服务](https://github.com/skylight-0/ToolBox/tree/main/sync-service)。本机默认只读加载 ToolBox 保存的同步地址和密钥，也可以使用 `TODO_SYNC_CONFIG` 指向私有 JSON 配置，或同时设置 `TODO_SYNC_URL` 和 `TODO_SYNC_TOKEN`。配置文件和密钥留在本机。

```powershell
python scripts/todo.py doctor
python scripts/todo.py list --status active --date today
python scripts/todo.py add "提交报告" --due "2026-11-06T18:00:00+08:00" --reminder on
python scripts/todo.py done TASK_ID
```

默认按 `+08:00` 解释无时区的时间；仅日期表示当地 `23:59`。更多连接方式、筛选、重复任务和重试参数见 [操作说明](references/operations.md)。

脚本通过已有 HTTP 同步协议提交字段变更，保留其他设备的描述、优先级和记录。操作发送前会保存固定 ID，响应丢失后使用 `pending` 和 `retry` 查询并重试，避免重复新增或重复完成。脚本不领取系统提醒。

## 验证

17 项测试使用独立临时数据库和本机临时 HTTP 服务，覆盖字段合并、删除恢复、重复任务、重试去重、配置只读、进程锁及 600 多条任务的快照读取。测试需要先构建 ToolBox 后端，再指定服务可执行文件；不会连接个人线上清单。

```powershell
# 在 ToolBox 仓库中构建一次
cargo build --manifest-path E:/MyProjects/toolbox/ToolBox/sync-service/Cargo.toml --target-dir E:/MyProjects/toolbox/ToolBox/target/todo-sync-service --locked

# 在本仓库中运行
$env:TODO_SYNC_SERVICE_BINARY = "E:/MyProjects/toolbox/ToolBox/target/todo-sync-service/debug/todo-sync-service.exe"
python -m unittest discover -s tests -v
```

其他系统使用对应的服务可执行文件路径。测试会启动、停止该二进制的独立实例，数据库由测试临时创建。
