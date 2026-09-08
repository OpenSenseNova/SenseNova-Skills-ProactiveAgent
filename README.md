# Proactive Memory Service

本地主动记忆服务：持续整理对话中的项目进展，发现信息复用、结论更新和任务推进的机会，在 Web 工作台提出建议；用户接受后，交回原 Session 执行。

当前默认使用 Web-only 展示：建议、项目进展和日报集中在网页中，Hermes 窗口负责正常对话和获批后的继续执行，不重复展示原生建议框。

```text
turn.completed
→ Organizer 更新 Project / Item / Event
→ Judge 记录提醒或静默原因
→ suggestion.ready
→ 用户同意 / 忽略
→ 可见续跑原 Hermes Session
→ 带 source_suggestion_id 的结果回流并更新状态
```

Core 负责统一事件与状态处理，Connector 负责平台接入与原 Session 续跑。仓库包含 ACP、Codex 和 Hermes 适配代码；当前自动安装流程面向指定 Hermes 基线，不包含 OpenClaw Connector。

面向真实用户时，状态数据默认保存在当前用户 Home 目录下的
`~/.proactive-memory/`。用户不需要预先拥有项目目录；第一次有明确长期目标的对话会自动创建
Project / Item。运行数据不随源码上传或随安装包分发；可以通过 `--data-root`
或 `PROACTIVE_MEMORY_DATA_ROOT` 指定其他数据根目录。

## 已实现

- 六类 V1 事件结构和四个入站 HTTP 接口；
- Project / Item / Event 的幂等 Markdown 存储；
- 原始 Turn、Organizer 归属、Judge 原因、建议响应与续跑结果的集中 `runtime.jsonl` 审计；
- 两阶段渐进式 Organizer、最近 Session 归属消歧和空更新修复；
- 明确的提醒门槛、静默原因，以及新输入到达时丢弃旧建议；
- Hermes `pre_llm_call` / `post_llm_call` Hook；
- classic CLI 建议卡、同意 / 忽略命令、原 Session 可见续跑和结果回流；
- Hermes TUI 主窗口原生建议框与续跑的历史验收；当前安装方案改为 Web-only，不启用重复建议框；
- Hermes ACP 的完整 QA 聚合、能力探测、原 Session 恢复，以及到现有 TUI Bridge 的建议 / 响应接线；
- 同一 HTTP 服务提供 Web 工作台：上方展示建议与决策，下方展示所有 Project / Item 的最新进度，并可展开 Event 与原始 QA；
- Web 工作台提供按本地午夜生成的前一日项目日报：当天首次打开自动弹出，关闭后可从左上角“日报”入口再次查看；
- 已整理为可构建、可安装的 Python 运行包：包含 Core、Web、Connector 和生命周期命令；`skills/proactive-memory/SKILL.md` 保持为独立的 Agent 操作说明，不由运行包复制。

## 测试

项目没有第三方 Python 运行时依赖：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
node --experimental-strip-types --test tests/test_web_bridge.mts
```

以上为 POSIX Shell 示例。PowerShell 先执行 `$env:PYTHONPATH = "src"`，再运行
`python -m unittest discover -s tests -v`。Node 测试需要支持 TypeScript 类型擦除的 Node.js
版本，并允许在本机临时端口启动测试服务。

自动测试使用隔离数据与模拟 Harness，不调用真实模型。测试通过不等于真实业务链路、
Windows 接入或 PyPI 发布已经完成。

## 从源码构建

当前仓库为 `0.1.2` 开发候选版；本次源码上传不发布新的 PyPI / TestPyPI 版本，也不包含 CI/CD。
需要 Python 3.11+。以下在已选定的 Python 环境中执行；macOS 可将 `python` 换成 `python3`：

```text
git clone https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent.git
cd SenseNova-Skills-ProactiveAgent
python -m pip install build
python -m build
```

构建产物位于 `dist/`，不提交到 Git。将所生成 wheel 的绝对路径用于下节的 `PMS_PACKAGE`。
只做源码开发与测试时，也可以在独立虚拟环境中执行 `python -m pip install -e .`。

## 安装运行包与 Hermes Connector

当前代码为 `0.1.2` 未发布候选版，新增可回滚的 Web-only 桥接安装与按 Session 的实际能力检查。
TestPyPI `0.1.1` 是旧安装验收包，不包含本次改进；不要用它执行新版接入流程。

完整说明以薄 Skill 的 [安装流程](skills/proactive-memory/references/install/overview.md)和
[Hermes 接入](skills/proactive-memory/references/connectors/hermes.md)为准。
按 [macOS](skills/proactive-memory/references/install/macos.md)或
[Windows / WSL](skills/proactive-memory/references/install/windows.md)说明，确认 Python 3.11+、pipx，
设置 `PMS_PYTHON`、候选 wheel 的 `PMS_PACKAGE`，以及当前 Hermes 实际源码目录 `PMS_HERMES_ROOT`。

```text
pipx install --python "$PMS_PYTHON" "$PMS_PACKAGE"
proactive-memory-service --version
proactive-memory-service setup --harness hermes --web-only --hermes-root "$PMS_HERMES_ROOT" --dry-run
proactive-memory-service setup --harness hermes --web-only --hermes-root "$PMS_HERMES_ROOT"
```

安装器支持 Hermes `b03c94dbed5ee72e97eace2376e02092cc854f6a` 的已核对 TUI 入口基线，
需要 Node.js 22+ 和现有构建依赖。它会检查指纹、备份源码与构建产物、接入 Web-only 桥接、
构建并启用观测插件；遇到未知修改时停止，构建/启用失败时回滚。不自动下载或升级 Hermes。
已有 ACP/TUI 定制环境保持原链路，本安装器不自动迁移；Windows 完整接入尚未验收。

`pipx` 不安装 Skill、不配置 Harness、不启动服务。`setup` 不复制 Skill、不发送消息；
只安装观测资源须显式加 `--observer-only`，classic CLI 可用 `--no-tui-plugin`，都不等于完整窗口接入。
仓库脚本 `scripts/install_hermes_connector.py` 是为源码开发保留的兼容入口；
面向用户统一使用上述已安装的 `setup` 命令。

安装后可检查文件，启动目标窗口并完成真实对话及 Web 接受后，再检查在线能力：

```text
proactive-memory-service doctor --harness hermes --hermes-root "$PMS_HERMES_ROOT" --json
proactive-memory-service doctor --harness hermes --hermes-root "$PMS_HERMES_ROOT" --url http://127.0.0.1:8080 --session-id "$PMS_SESSION_ID" --json
```

`PMS_SESSION_ID` 必须是该窗口的真实 ID。文件安装、桥接在线、完整 QA 到达与获批原 Session 回流
分别报告；没有真实证据时为 `unverified`，必需项未验证时退出码为 1。`doctor` 不调用模型或发送测试输入。

卸载时明确使用原源码目录，恢复未被再次修改的 TUI 文件并保留用户数据：

```text
proactive-memory-service uninstall --harness hermes --hermes-root "$PMS_HERMES_ROOT" --dry-run
proactive-memory-service uninstall --harness hermes --hermes-root "$PMS_HERMES_ROOT"
```

旧观测清理仍可能留下本插件的 `plugins.enabled` / `plugins.entries` 配置，详见接入说明；
不能将这些残留称作完整卸载。源码备份与 `.proactive-memory/` 数据不自动删除。

## 启动

安装包启动（使用 Hermes 当前默认 Provider）：

```bash
proactive-memory-service serve --web-only
```

如需显式指定本机 Hermes、Provider、模型或数据位置，使用 `--hermes`、
`--hermes-provider`、`--hermes-model` 和 `--data-root`；参数应来自当前用户已经核对的环境。
完整选项可执行 `proactive-memory-service serve --help` 查看。

服务默认监听 `127.0.0.1:8080`。启动后可直接打开 Web 工作台：

```text
http://127.0.0.1:8080/
```

页面上方显示建议与决策，下方显示所有 Project / Item 的最新状态；建议可以直接在网页中接受或忽略。ACP 的原 Session 续跑仍由原 Connector 负责。

服务运行到本地午夜后会生成前一天的项目日报，并写入 `runtime.jsonl`。用户当天第一次打开
Web 工作台时会看到可关闭的日报弹窗；之后可点击左上角“日报”重新打开。服务重启时会补做
最近一天尚未生成的日报。

如果使用 classic CLI，建议出现时服务输出仍会打印两条命令，例如：

```bash
proactive-memory-service respond <suggestion-id> approve
proactive-memory-service respond <suggestion-id> ignore
```

## 仓库与发布结构

```text
src/proactive_memory_service/          → 核心服务与 Web 前端
src/proactive_memory_connectors/       → ACP、Codex、Hermes 适配与安装资源
skills/proactive-memory/               → 独立 Skill 与安装接入说明
tests/                                → 自动测试与隔离样例
scripts/                              → 安装、迁移及测试依赖脚本
pyproject.toml / MANIFEST.in           → Python 安装包构建配置
README.md / README.pypi.md             → 仓库和安装包的入口说明
```

安装包只带运行所需的代码、Web 和 Connector 资源，不带 Skill、测试或开发脚本。
所有 `docs/`、本地运行数据、截图录像、无关实验与构建产物都不加入 Git 提交。
`scripts/run_hermes_acp_demo.py` 暂作为既有回归测试的依赖保留；其中固定场景仅用于测试，
不参与默认服务的 Organizer / Judge 判断。

## 最小验收（Web-only）

完成上述安装后，分别启动服务和接入的 Hermes。已有窗口应在安全时机重启，加载新构建与插件：

```text
proactive-memory-service serve --web-only
```

```text
hermes --tui --accept-hooks
```

在 <http://127.0.0.1:8080/> 查看状态变化与建议。按
[真实对话验收](skills/proactive-memory/references/connectors/hermes.md#真实对话验收)
确认完整 QA、Project / Item / Event 更新、Web 接受/忽略、可见的原 Session 执行与来源 ID 回流。
仅构建成功或 Web 可打开不等于完整验收；实际业务结果仍须检查。

历史 classic CLI、原生红框与 ACP/TUI 实现保留兼容，不代表本次候选安装包已在所有新用户
环境通过。本次自动安装不启用终端建议框，也不替换已有 ACP 的执行路径。

## 数据

```text
~/.proactive-memory/
├── runtime.jsonl
└── projects/
    └── <project-id>/
        ├── project.md
        └── items/<item-id>/
            ├── item.md
            └── events.md
```

- `project.md`：Project 封面和 Item 索引；
- `item.md`：Item 最新状态；
- `events.md`：完整 QA 和绝对状态更新；
- `runtime.jsonl`：服务运行日志，供程序恢复、去重和审计；日报生成、查看和关闭分别记录为
  `daily_report.generated`、`daily_report.viewed`、`daily_report.dismissed`。

`--data-root` 指定后，以上结构会落在指定目录下。测试和演示应使用独立的数据目录，
避免与真实用户数据混在一起。

如果已有旧版本的 `runtime.md`，先停止写入该目录的服务并备份数据，将 `PMS_DATA_ROOT`
设置为待迁移目录，再在源码仓库执行：

```text
python scripts/migrate_runtime_journal.py --root "$PMS_DATA_ROOT"
```

默认保留旧日志；核对迁移后的 `runtime.jsonl` 后再决定是否清理旧文件。

## HTTP 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/` | Web 工作台页面 |
| `GET` | `/api/dashboard` | 返回建议、决策和所有 Project / Item 的展示 JSON |
| `GET` | `/api/daily-report` | 返回前一日项目日报及首次打开状态 |
| `POST` | `/api/daily-report/view` | 记录用户已查看日报 |
| `POST` | `/api/daily-report/dismiss` | 记录用户关闭日报 |
| `GET` | `/api/projects/<project-id>/items/<item-id>/events` | 按需读取某个 Item 的 Event 历史和原始 QA |
| `GET` | `/health` | 健康检查 |
| `POST` | `/v1/events/turn.started` | 接收新一轮输入信号 |
| `POST` | `/v1/events/turn.completed` | 接收完整 QA |
| `POST` | `/v1/events/suggestion.responded` | 接收同意 / 忽略 |
| `POST` | `/v1/events/session.resume.failed` | 接收续跑失败结果 |
| `GET` | `/v1/bridge/events` | TUI 按 Session 长轮询建议 / 续跑事件 |
| `POST` | `/v1/bridge/source/claim` | TUI 观测组件按动作原文和 Turn ID 领取来源建议 ID |
| `POST` | `/v1/bridge/heartbeat` | 目标窗口持有短时租约并完成往返探测 |
| `POST` | `/v1/bridge/resume/claim` | 原窗口最多一次领取 Core 已授权的续跑动作 |
| `GET` | `/v1/bridge/diagnostics` | 按实例与 Session 返回在线、QA、续跑回执，不返回原始 QA |

## 使用说明

- [Skill 入口](skills/proactive-memory/SKILL.md)
- [安装流程](skills/proactive-memory/references/install/overview.md)
- [macOS 安装](skills/proactive-memory/references/install/macos.md)
- [Windows / WSL 安装](skills/proactive-memory/references/install/windows.md)
- [Hermes 接入与验收](skills/proactive-memory/references/connectors/hermes.md)
