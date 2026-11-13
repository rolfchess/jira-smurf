# Changes

The tool has no version numbers yet. This file lists the changes per week, newest first.

## Week of 2026-11-09

- Add `[[badges]]` to `gantt.toml`. A badge marks the epics where a Jira field has a value, for
  example a "Paid Feature" checkbox. A badge has a label, a color, an optional stripe on the left of
  the epic row and an optional list of `values`.
- Badges work with checkboxes, Yes/No selects, number fields, cascading selects and rich text
  fields. A field set to "No", "False" or 0 gives no badge.
- Show the badges before the name of the epic, so a long name does not hide them.
- Find epics by their badges in the search box.
- Warn when the list of Jira fields cannot be read.
- Count only the days in the implementation status or a later one for the `impl` badge. The days a
  story spent back in an earlier status are left out.
- Keep a story that is assigned in Jira to someone outside the team with that person, on their own
  row.
- Add the `exclude` setting, to leave single stories or whole projects out of the Gantt chart.
- Show the stories without an epic at the top of the Gantt chart, without a bar for the group.
- Keep the bar colors and row backgrounds when you print the Gantt chart.
- Restore the blinking red outline on a story or epic that ends after its due date.
- Show a story that is behind the baseline with a yellow outline, not a red one.
- Add the optional `[visuals]` section to `gantt.toml`, with `past_due_blink` to stop the blinking
  and `past_due_color` to set the color of the outline of a story that ends after its due date.
- Add the holidays of 2028 to `gantt.example.toml`.

## Week of 2026-11-02

- Add the `serve` command. It serves the Gantt chart on this computer, with a **Refresh from Jira**
  button. A spinner shows while the refresh runs.
- Show the story points as badges in the Points column. Give epic rows a blue-grey background.
- Add the `implementation_status` setting. An open story in that status gets an `impl` badge with
  its days in implementation and its estimate. Its bar starts on the day it started.
- Show the age of each open story in a grey badge.
- Use the Story point estimate field when a story has no story points.
- Remove the Rank column from the Gantt chart.
- Remove the Sprint filter and the `order_epics_by_rank` and `assign_by_epic` settings.
- Remove the blinking red outline on a story or epic that ends after its due date. This was not
  on purpose, and it is back in the week of 2026-11-09.
  `use_backlog_order` now also orders the epic rows.

## Week of 2026-10-26

- First version: export the stories of a project as CSV or as an HTML table, and plan stories in a
  Gantt chart.
- Add days off per team member and holidays for the whole team.
- Add the `in_team` setting for team members who join or leave the team.
- Add the `auto_assign` setting, for the whole team and per member.
- Color rows by Jira status with `[status_colors]`.
- Add the Collapse all and Expand all buttons.
- Store a snapshot of each plan with `--history`. Show the changes since an earlier snapshot with
  `--since`, and the trend of the epic end dates.
- Place done stories on the days they were worked on, from their Jira status history.
- Plan in the order of the Jira backlog with `use_backlog_order`.
- Plan started stories and stories in the active sprint before the rest of the backlog.
- Show a red diamond on the due date of a story or epic.
- Show a blinking red outline on a story or epic that ends after its due date.
- Show an epic without stories as a bar from its Start date to its Due date in Jira.
- Use Jira links of the types in `dependency_links` as dependencies, and draw them as arrows.
- Show the Jira issue type icons in the Story column.
- Filter the Gantt chart on team members.
- Add the Points and Sprint columns.
- Add the `velocity` command. A done story without points counts as `default_hours` divided by
  `velocity` points.
- `generate-gantt.sh` uses the Python of `.venv` when there is one.
- Add a tip to share the chart from Google Drive.
- Add the documentation [Velocity: how fast does the team really work?](documentation/velocity.md).
