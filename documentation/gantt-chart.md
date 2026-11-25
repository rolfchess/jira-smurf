# Using the Gantt chart

The chart supports zooming, searching, and filtering by team member. Select several members—or
“Unassigned”—to combine their work. Done stories are hidden initially; use the toolbar to show them.
The Story-column buttons collapse or expand every epic; the space rows stay open. Click an epic or
a space in the Story column to collapse or expand only that one.

The Day, Week, Month and Quarter buttons set the scale of the chart. A filter then keeps that scale
and the dates in view. Scroll to see stories outside them. Until you pick a scale, a filter fits the
chart to the stories that are shown. After Fit All, it does the same. In every scale, the timeline
has 300 pixels of room after the last bar, so you can read the text next to it, and Fit All keeps
that room in view.

The row under the mouse is darker across the whole chart, and a thin vertical line follows the
mouse over the timeline, so you can read off the date. A red line marks the current day. Point at
a bar, or at a title in the Story column, to see the dates and changes of that story under its row.
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
- With `use_backlog_order` and `group_by_space`, the default, and epics in more than one Jira space,
  each space has a header row with its name and avatar above its epics. The row shows the total
  points of its visible stories, and a thin grey line in the timeline from the first start to the
  last end of its epics.
- Weekends and holidays are shaded in day view.
- Colors remain when printing or saving as PDF.

The Sprint column shows a story's latest sprint number. The Status column shows its current Jira
status. Search also matches epic badges. A search also shows the stories it finds that a filter
hides, such as done stories, until you clear the search box. The rows it finds have a frame in the
color of the search box, violet by default, and the number of matches shows under the search box.

## Refreshing in the browser

```bash
python jira_smurf.py serve --history plannings
```

`serve` opens `http://localhost:8000/` and adds a **Refresh from Jira** button. Each refresh fetches
Jira data again and rereads `gantt.toml`. With `--history`, it also stores the refreshed plan and
chart. When a refresh fails, one red line under the button shows the error; hover over it to read
the whole message, and close it with the ×.

The server binds only to `127.0.0.1`, because the chart contains Jira data. A chart opened directly
from a file has no refresh button.

See [plan history and trends](history.md) or [share a chart through Google Drive](sharing.md).
