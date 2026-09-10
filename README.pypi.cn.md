# Proactive Agent

[English](README.pypi.md) · 简体中文

Proactive Agent 从你与 AI 的多轮对话中持续整理项目进展，并提出有用的下一步建议，由你决定接受或忽略。

## 它能帮你做什么

- **持续整理项目进展。** 从已接入的对话中整理目标、进展、阻塞和下一步。展开工作台中的项目，可以看到下面的事项与进度条，并随新记录的进展更新。
- **复用之前的工作。** 提醒复用之前分享过的相关模板、报告或结论。例如，沿用旧演示文稿的结构，补入最新数据，减少重复整理。
- **更新相关材料。** 将新发现与已有材料联系起来，提醒同步更新。例如，新分析补充或改变了原来的判断，之前写好的周报也能据此调整。
- **用日报快速了解进展。** 自动汇总各项目的进展和下一步，在当天第一次打开工作台时展示。日报可以关闭，也可以从入口重新查看，无需专门发消息索要。

建议以简短卡片展示在工作台中。你可以接受，让助手在仍保持连接的原对话中继续处理；也可以忽略，不执行建议中的动作。

[查看 Demo 截图](https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent/blob/main/README.cn.md)。

## 如何开始

改名后的 `sn-proactive-agent` 当前是本地 `0.1.3` 候选版，尚未发布到 GitHub Release 或 PyPI。

让 Agent 按照 [安装 Skill](https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent/blob/main/skills/sn-proactive-agent/SKILL.md) 帮你安装服务并接入助手，也可以自行参考 [安装指南](https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent/blob/main/skills/sn-proactive-agent/references/install/overview.md)。

完成接入后，启动服务并保持运行：

```sh
sn-proactive-agent serve --web-only
```

如果接入的是 Hermes，在另一个终端打开它：

```sh
hermes --tui --accept-hooks
```

打开 [工作台](http://127.0.0.1:8080/)，查看建议、项目进展和日报。

---

记录保存在本地；AI 处理时，相关对话和项目内容可能发送至你配置的模型服务。
