# Changes

The tool has no version numbers yet. This file lists the changes per week, newest first.

## Week of 2026-11-23

- Mark the Fit All button while the chart is fitted. Before, Fit All marked the button of the scale
  it picked, such as Quarter, even though the chart was not in that scale.
- Add the `group_by_space` setting. It is on by default, which keeps the header row per Jira space
  with `use_backlog_order`. With `group_by_space = false`, the epics are listed by Jira rank only.
- Update the documentation: weekend hours, unknown statuses, badge colors and stripes, assignees
  without hours left, the mouse line, the search count, the refresh error, and the missing support
  for Jira Server and Data Center.
- Plan an open story that has not started yet no earlier than its Start date in Jira, or, without
  one, the Start date of its epic. Before, the Start date was only used for epics without stories.
  The tooltip of a story or epic shows its Start date from Jira.
- List the stories of an epic in Jira rank order with `use_backlog_order`, with the done stories
  first. Before, they were sorted by start date, so a story that starts earlier, for example
  because auto-assign gave it to someone who is free sooner, came above stories ranked higher.
- List the epics by the name of their Jira space first, then by Jira rank, with `use_backlog_order`.
  Before, the epics of all spaces were mixed in one rank order.
- Show a header row per Jira space above its epics, with `use_backlog_order` and epics in more than
  one space. The row shows the space name and avatar, the total points of its visible stories, and a
  thin line in the timeline over the dates of its epics. Collapse all keeps the space rows open.
- Keep the vertical line under the mouse in the Gantt chart after you scroll the timeline. In the Day
  and Week views, it moved away from the mouse by the distance you had scrolled.
- Leave 300 pixels of room after the last bar of the timeline, in every scale. The text next to the
  last bars, such as the assignee and the end date, was cut off at the right edge in Fit All, and you
  could not scroll to it.
- Collapse or expand an epic when you click it in the Story column. Before, the click selected the
  epic and scrolled the timeline to its bar.

## Week of 2026-11-16

- Show the stories that a search finds, also when a filter hides them, such as done stories. They
  hide again when you clear the search box. The count says how many were hidden.
- Mark the rows that a search finds with a frame in the search color, not with a yellow background,
  and give the search box the same color while it has text. The bars keep their own outlines. Set
  the color with `search_color` in `[visuals]`; it is violet by default.
- Show the number of search matches under the search box, so the box does not move.
- Show a failed refresh from Jira as one red line under the refresh button, with a × to close it.
  Hover over the line to see the whole message. For an error page in HTML, the line shows the title
  of the page, and the hover shows its text without the HTML tags.
- Fetch the stories of the epics, the single stories, the sprint stories and the issue type icons
  4 calls at a time, not one after another.
- Show the last day of a bar next to it in the Gantt chart. It showed the day after, one day later
  than the info popup.
- Open a story in Jira only with a click on its bar, not on the labels next to the bar.
- Fetch the status history of all stories with the bulk changelog of Jira, one call per 1000
  stories. When that fails, fetch the history of each story, 4 at a time.
- Add `behind_baseline_blink` to `[visuals]`, to make the outline of a story that is behind the
  baseline blink. It does not blink by default.
- Show the baseline bar of a story in the dark shade of its bar color, not in gray.
- Darken the whole row under the mouse in the Gantt chart, from the name to the end of the timeline.
  A row with its own color keeps that color.
- Show the info popup of a story only over its bar and over its title in the Story column. The
  popup appears just under the row and follows the mouse along the row.
- Show the points in the output of `velocity` with one decimal, so the table lines up.
- Add a Stories column to the output of `velocity`, with the number of finished stories that have
  story points.

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
- Make the search box of the Gantt chart faster. It searches when the typing pauses, and it
  redraws the chart once per search, not once per match.
- Keep the scale you picked in the Gantt chart, and the dates in view, when you filter on a team
  member or show done stories.
- Add `behind_baseline_color` to `[visuals]`, to set the color of the outline of a story that is
  behind the baseline. The default is yellow.

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
