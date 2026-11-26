# Changes

The tool has no version numbers yet. This file lists what changed each week, newest first.

## Week of 2026-11-23

### Added

- The **Today** button, left of the search box, scrolls the timeline to the current day.
- The **Compare with plan** list compares the chart with any earlier plan. The link of the chart
  keeps your choice, as `?compare=YYYY-MM-DD`.
- The date header marks holidays and the days off of team members. An absence of five working days
  or more is one bar.
- Each Jira space has a header row above its epics, with `use_backlog_order`. The table under the
  trend chart has the same rows, and its row for the whole plan is bold.
- For a story that is done since the compared plan, the Moved column shows how much earlier or later
  it finished.
- Clicking an epic in the Story column collapses or expands it.
- New settings: `workdays`, `group_by_space`, and in `[visuals]` `days_off_bar_from`,
  `holiday_icon` and `enable_vacation_banners`.

### Changed

- An open story waits for its Start date in Jira, or for the Start date of its epic.
- With `use_backlog_order`, the epics are sorted by Jira space and then by rank, and the stories of
  an epic follow the Jira rank.
- The timeline has 300 pixels of room after the last bar, so the text next to it can be read.
- `--since` only picks the plan to compare with when the chart opens. The **Show changes since …**
  checkbox is gone.

### Fixed

- The vertical line under the mouse stays under the mouse after you scroll the Day or Week view.
- Fit All marks its own button, not the button of the scale it picked.
- With done stories hidden, the plan of an epic no longer looks later than it is.

## Week of 2026-11-16

### Added

- A search also shows the stories that a filter hides, such as done stories.
- The rows that a search finds have a frame in the search color, set with `search_color`.
- `behind_baseline_blink` makes the outline of a story behind the baseline blink.
- The output of `velocity` has a Stories column.

### Changed

- A run is much faster: the status history comes from Jira in one call, and the stories and icons
  are fetched 4 at a time.
- The whole row under the mouse is darker.
- The info popup shows only over a bar or a title, just under the row.
- The baseline bar has the dark shade of the bar color.
- The search count is under the search box, so the box no longer moves.
- A failed refresh shows as one red line under the button, with a × to close it.
- The points in the output of `velocity` have one decimal.

### Fixed

- The date next to a bar is its last day, not the day after.
- Only a click on a bar opens the story in Jira, not a click on the text next to it.

## Week of 2026-11-09

### Added

- `[[badges]]` mark the epics where a Jira field has a value, such as a "Paid Feature" checkbox. The
  search finds epics by their badges.
- The `exclude` setting leaves stories or whole projects out of the chart.
- The `[visuals]` section, with `past_due_blink`, `past_due_color` and `behind_baseline_color`.
- `gantt.example.toml` has the holidays of 2028.

### Changed

- The `impl` badge counts only the days in the implementation status or a later one.
- A story assigned to someone outside the team stays with that person, on their own row.
- Stories without an epic are at the top of the chart.
- A story behind the baseline has a yellow outline, not a red one.
- The search is faster, because it waits until you stop typing.
- A filter keeps the scale you picked and the dates in view.
- A printed chart keeps its bar colors and row backgrounds.
- The tool warns when it cannot read the list of Jira fields.

### Fixed

- The blinking red outline on work that ends after its due date is back.

## Week of 2026-11-02

### Added

- The `serve` command shows the chart on this computer, with a **Refresh from Jira** button.
- The `implementation_status` setting adds an `impl` badge to stories in implementation.
- Open stories show their age in a badge.

### Changed

- The Points column shows story points as badges, and epic rows have a blue-grey background.
- The Story point estimate field is used when a story has no story points.
- `use_backlog_order` also orders the epic rows.

### Removed

- The Rank column, the Sprint filter, and the `order_epics_by_rank` and `assign_by_epic` settings.
- The blinking red outline on work past its due date was removed by mistake. It came back the next
  week.

## Week of 2026-10-26

### Added

- The first version exports the stories of a project as CSV or as an HTML table, and plans them in
  a Gantt chart.
- The plan takes days off, holidays, `in_team` periods and `auto_assign` into account.
- Dependencies come from `depends_on` and from Jira links, and the chart draws them as arrows.
- `--history` stores each plan, `--since` compares with an earlier one, and a trend chart shows the
  end dates of the epics.
- Done stories are placed on the days they were worked on, from their Jira history.
- `use_backlog_order` plans in Jira backlog order, with started and sprint stories first.
- Due dates show as red diamonds, and work that ends later has a blinking red outline.
- An epic without stories shows from its Start date to its Due date.
- The chart has row colors per status, Jira issue type icons, Points and Sprint columns, a team
  member filter, and Collapse all and Expand all buttons.
- The `velocity` command measures the hours per story point.
- `generate-gantt.sh` uses the Python of `.venv` when there is one.
- The documentation explains velocity and how to share a chart from Google Drive.
