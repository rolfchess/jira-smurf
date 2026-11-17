# How scheduling works

The planner turns story points into hours, orders the work, and assigns it to available people.

## Estimates and order

Remaining hours are the estimate (`points × velocity`) multiplied by the status's remaining
percentage. Stories without points use `default_hours`. Epic estimates are ignored; an epic spans
its stories.

Work is grouped before it is ordered:

1. done stories;
2. started stories;
3. stories in the active sprint;
4. all remaining stories.

Within those groups, the default order is configured epics, individual stories, then sprint stories.
With `use_backlog_order = true`, Jira rank determines story and epic order.

A dependency must finish before the dependent story starts. Dependencies can come from `depends_on`
or from Jira link types listed in `dependency_links`. Links to work outside the plan are ignored.

## Assignment

- A story with a Jira assignee stays with that person.
- An assignee outside `[team]` gets a separate row with eight hours per weekday and only global
  holidays off.
- An unassigned story goes to the eligible member who can finish it first.
- With `auto_assign = false`, unassigned work is scheduled sequentially under “Unassigned.”
- A member who leaves before completing a story is skipped when another member can finish it.
- A done story keeps its Jira assignee and consumes no future capacity.

Automatically assigned stories receive an `AUTO` badge in the chart.

## Dates and status history

Open work starts today at the earliest. When `implementation_status` is configured, time already
spent at that status or later is shown in the past and deducted from the remaining estimate. Periods
after a story moves back to an earlier status do not count.

Done work is placed from Jira history. It starts when it first reaches `work_starts_at` or a later
status, and ends on its last transition to 100% progress. Without `work_starts_at`, the first status
above 0% is used. If the history is incomplete, the story is placed at the plan's start.

## Epics and Jira project types

An epic runs from its first story to its last. An empty epic is shown from its Jira Start date to
Due date without reserving team capacity; it is omitted when both dates are absent.

The tool supports both Jira project models: a team-managed story links to its epic through `parent`,
while a company-managed story commonly uses Epic Link (`customfield_10014`). Stories without an epic
appear in “(No Epic)” at the top of the chart.

Next: [use the generated chart](gantt-chart.md).
