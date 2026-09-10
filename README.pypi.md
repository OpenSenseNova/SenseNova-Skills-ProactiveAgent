# Proactive Agent

English · [Chinese](https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent/blob/main/README.pypi.cn.md)

Proactive Agent keeps project progress up to date across your AI conversations and suggests useful next steps that you can accept or ignore.

## Where it helps

- **Keep project progress in view.** Organize goals, progress, blockers, and next steps from connected conversations. Expand a project in the dashboard to see its tasks and progress bars, which update as new progress is recorded.
- **Reuse earlier work.** Suggest relevant templates, reports, or conclusions you have already shared. For example, reuse a previous presentation's structure with the latest figures instead of starting over.
- **Keep related work up to date.** Connect new findings with earlier work and suggest updates, such as revising a weekly report after a new analysis adds to or changes its conclusions.
- **Catch up with a daily recap.** Automatically bring project progress and next steps together on your first dashboard visit each day. Close the recap or reopen it from the daily-report button, without asking in chat.

Suggestions appear as short cards in the dashboard. Accept one to have the assistant continue the action in the original connected conversation, or ignore it without running the proposed action.

[See the demo screenshots](https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent#where-it-helps).

## Getting started

The renamed `sn-proactive-agent` package is a local `0.1.3` candidate, not yet released on GitHub or PyPI.

Use the [setup Skill](https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent/blob/main/skills/sn-proactive-agent/SKILL.md) with your agent to install the service and connect your assistant, or follow the [installation guide](https://github.com/OpenSenseNova/SenseNova-Skills-ProactiveAgent/blob/main/skills/sn-proactive-agent/references/install/overview.md).

After setup, start the service and leave it running:

```sh
sn-proactive-agent serve --web-only
```

For a connected Hermes installation, open it in another terminal:

```sh
hermes --tui --accept-hooks
```

Open [the dashboard](http://127.0.0.1:8080/) to view suggestions, project progress, and your daily recap.

---

Records are stored locally; relevant conversation and project content may be sent to your configured model service for AI processing.
