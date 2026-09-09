# Proactive Memory Service

English · [简体中文](README.cn.md)

Proactive Memory Service keeps project progress up to date across your AI conversations and suggests useful next steps that you can accept or ignore.

## Where it helps

From connected conversations, the service organizes each project's goals, latest progress, blockers, and next steps. The dashboard brings projects together in one place; expand a project to see its tasks and progress bars. As new progress is recorded, task statuses and their progress bars update too.

Useful follow-ups appear as short suggestion cards above your projects. Accept a suggestion to have the assistant continue the action in the original connected conversation, or ignore it without running the proposed action.

*Demo screenshots use sample data; the interface shown is in Chinese.*

### Reuse earlier work

Materials from previous work may still be useful for a new task. The service can suggest relevant templates, reports, or conclusions you have already shared. For example, when preparing a quarterly presentation, it can suggest reusing the previous quarter's slide structure and adding the latest figures, reducing the work of starting over.

![A suggestion to reuse earlier presentation materials](media/readme/reuse-previous-work.png)

### Keep related work up to date

New findings may add to or change an earlier conclusion. The service can connect them with related work and suggest an update. For example, a new retention analysis may be useful not only for the current question, but also for revising a weekly report written earlier.

![A suggestion to update the weekly report with new findings](media/readme/update-existing-work.png)

### Catch up with a daily recap

When several projects are moving at once, the service brings their key progress and next steps into a short daily recap. It opens on your first dashboard visit of the day, with no chat request needed. Dismiss it when you're done, or reopen it from the daily-report button.

![A daily recap of project progress and next steps](media/readme/daily-project-recap.png)

## Getting started

Use the [setup Skill](skills/proactive-memory/SKILL.md) with your agent to install the service and connect your assistant, or follow the [installation guide](skills/proactive-memory/references/install/overview.md).

After setup, start the service and leave it running:

```sh
proactive-memory-service serve --web-only
```

For a connected Hermes installation, open it in another terminal:

```sh
hermes --tui --accept-hooks
```

Open [the dashboard](http://127.0.0.1:8080/) to view suggestions, project progress, and your daily recap.

---

Records are stored locally; relevant conversation and project content may be sent to your configured model service for AI processing.
