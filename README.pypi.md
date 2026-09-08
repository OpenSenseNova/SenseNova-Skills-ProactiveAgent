# Proactive Memory Service

本地主动记忆服务：整理项目进展，在值得推进时提出建议；用户在 Web 接受后，交回原 Session 执行。

## 当前版本

`0.1.2` 是未发布候选版，包含 Core、Web、Hermes 观测组件、指定版本的 Web-only 续跑桥与安装检查。
TestPyPI `0.1.1` 不包含这些新版安装与探测能力，不能替代本候选包。
Skill 说明独立分发，安装运行包不会安装 Skill。

## 安装前提

需要 Python 3.11+、pipx、已可用的 Hermes，以及 Node.js 22+ 和 Hermes 构建依赖。
当前安装基线为 Hermes `b03c94dbed5ee72e97eace2376e02092cc854f6a` 的 TUI 入口与构建脚本；
未知版本或本地修改会阻止安装，不自动升级/重装 Hermes。Windows 完整接入尚未验收。

先在当前 Shell 设置变量：`PMS_PYTHON` 为已检查的 Python 解释器，`PMS_PACKAGE` 为来源已核实的
候选 wheel 绝对路径，`PMS_HERMES_ROOT` 为当前 Hermes 实际使用且允许修改的源码目录（内含 `ui-tui/`）。
POSIX Shell 使用 `NAME="value"`；PowerShell 使用 `$NAME = "value"`。
没有候选文件时先取得已确认的包，不猜测远程下载地址。

```text
pipx install --python "$PMS_PYTHON" "$PMS_PACKAGE"
proactive-memory-service --version
proactive-memory-service doctor --json
```

命令不存在时执行 `pipx ensurepath` 后重新打开终端；不要拼接开发者源码路径冒充已安装命令。

## 配置与启动

配置前确认源码目录与配置实例的对应关系。默认配置目录是用户 Home 下 `.hermes/`；
自定义实例用一致的 `HERMES_HOME`，或在安装、检查、卸载命令上显式传 `--hermes-home`。

```text
proactive-memory-service setup --harness hermes --web-only --hermes-root "$PMS_HERMES_ROOT" --dry-run
proactive-memory-service setup --harness hermes --web-only --hermes-root "$PMS_HERMES_ROOT"
proactive-memory-service serve --web-only
```

安装会备份并修改所选源码、构建 TUI、安装和启用观测插件。构建或启用失败时恢复本次改动；
`--dry-run` 只做静态预检查，不构建、不启用。仅复制观测资源使用 `--observer-only`，不等于完整接入。
服务默认监听 `127.0.0.1:8080`，不要直接暴露到公网。

然后在另一终端打开接入的 Hermes（已有窗口需安全重启）：

```text
hermes --tui --accept-hooks
```

工作台：<http://127.0.0.1:8080/>。建议只在 Web 展示，获批动作通过原窗口继续，忽略则不执行。
自定义服务地址时，在启动 Hermes 的终端设置 `PROACTIVE_MEMORY_SERVICE_URL` 为实际地址。

## 验证与卸载

先取得实际窗口的 Session ID，设置 `PMS_SESSION_ID`。使用获准的模型与输入完成对话，
并在 Web 接受建议、观察原 Session 执行和结果回流后检查：

```text
proactive-memory-service doctor --harness hermes --hermes-root "$PMS_HERMES_ROOT" --url http://127.0.0.1:8080 --session-id "$PMS_SESSION_ID" --json
```

检查分别报告文件安装、窗口在线、完整 QA 到达和获批动作带来源 ID 回流。
`doctor` 不调用模型、不发送输入；未观察到相应能力时为 `unverified`，必需项未验证则退出码为 1。
基础检查或构建通过不等于真实业务验收，状态更新与执行结果仍需核对。

卸载前安全停止服务与相关窗口，使用安装时的同一源码目录和配置实例：

```text
proactive-memory-service uninstall --harness hermes --hermes-root "$PMS_HERMES_ROOT" --dry-run
proactive-memory-service uninstall --harness hermes --hermes-root "$PMS_HERMES_ROOT"
pipx uninstall proactive-memory-service
```

源码或构建产物在安装后再次被修改时，自动恢复会停止，不覆盖用户修改。
旧观测清理仍可能保留本插件的 `plugins.enabled` / `plugins.entries` 配置，须检查并报告残留，
不要当作完整卸载。数据默认保存在 `~/.proactive-memory/`，卸载保留数据及源码备份。
升级也应先恢复旧桥，再安装新包、重新配置与验收。
