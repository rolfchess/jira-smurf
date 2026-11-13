# Using the Gantt chart

The chart supports zooming, searching, and filtering by team member. Select several members—or
“Unassigned”—to combine their work. Done stories are hidden initially; use the toolbar to show them.
The Story-column buttons collapse or expand every epic.

Click a bar to open its Jira issue. Issue icons are embedded in the page so they work without a Jira
login; an emoji is used when an icon cannot be downloaded.

## What the chart shows

- Story-point badges are green, or orange when implementation has exceeded the estimate.
- Open stories show their age in days.
- With `implementation_status`, an `impl 3/2d` badge means three working days spent against a
  two-day estimate. Hover for dates and the difference.
- Due dates appear as red diamonds. Unfinished work ending after its due date gets the configured
  overdue outline.
- Epic rows show the total points of their visible stories.
- Weekends and holidays are shaded in day view.
- Colors remain when printing or saving as PDF.

The Sprint column shows a story's latest sprint number. The Status column shows its current Jira
status. Search also matches epic badges.

## Refreshing in the browser

```bash
python jira_smurf.py serve --history plannings
```

`serve` opens `http://localhost:8000/` and adds a **Refresh from Jira** button. Each refresh fetches
Jira data again and rereads `gantt.toml`. With `--history`, it also stores the refreshed plan and
chart.

The server binds only to `127.0.0.1`, because the chart contains Jira data. A chart opened directly
from a file has no refresh button.

See [plan history and trends](history.md) or [share a chart through Google Drive](sharing.md).
