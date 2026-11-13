# Planning configuration

The `gantt`, `serve`, and `velocity` commands read `gantt.toml` by default. Start with
[`gantt.example.toml`](../gantt.example.toml), which contains a complete example.

## Plan settings

| Setting                  | Purpose                                                                                |
| ------------------------ | -------------------------------------------------------------------------------------- |
| `start_date`             | First planning day in `YYYY-MM-DD` format                                              |
| `default_hours`          | Estimate for a story without points                                                    |
| `velocity`               | Team hours per story point                                                             |
| `include_open_sprints`   | Include every story in the active sprint                                               |
| `include_future_sprints` | Include every story in future sprints                                                  |
| `use_backlog_order`      | Use Jira backlog order instead of configuration order                                  |
| `work_starts_at`         | Status at which work starts, such as `"In Progress"`                                   |
| `implementation_status`  | Optional status used for implementation-time badges                                    |
| `dependency_links`       | Jira link types treated as dependencies; defaults to `["Blocks"]`; use `[]` to disable |
| `auto_assign`            | Assign unowned stories to the earliest available member; `false` uses “Unassigned”     |
| `exclude`                | Project or issue keys omitted from the plan, such as `["ARCH", "APP-123"]`             |
| `holidays`               | Dates on which the whole team is unavailable                                           |

## Team availability

Create one `[team."Display Name"]` table per member. The name must match Jira. Set working hours
with `mon` through `fri`.

```toml
[team."Alice Baker"]
mon = 8
tue = 8
wed = 8
thu = 8
fri = 6
days_off = ["2026-02-16", "2026-07-20 to 2026-08-07"]
in_team = ["to 2026-06-30", "2026-09-01 to"]
auto_assign = false
```

- `days_off` accepts dates or inclusive date ranges.
- `in_team` accepts membership periods with an optional open start or end. Without it, membership
  has no date limit.
- Member-level `auto_assign = false` prevents automatic assignment to that person. It has no effect
  when top-level `auto_assign` is false.

## Statuses, colors, and visuals

Map every relevant Jira status to progress from 0 to 100. A story at 100 is done.

```toml
[statuses]
"To Do" = 0
"In Progress" = 20
"Code Review" = 80
"Done" = 100

[status_colors]
"Blocked" = "#ffcccc"

[visuals]
past_due_blink = false
past_due_color = "#d62728"
```

## Epic badges

Each `[[badges]]` table marks epics whose Jira field has a value.

```toml
[[badges]]
field = "customfield_10410"
label = "Paid"
color = "#d9a520"
stripe = true
values = ["Yes", "Partly"]
```

Only `field` is required. Without `label`, the badge displays the field value. Without `values`, any
value except “No”, “False”, or zero matches. Cascading selects appear as `Parent > Child`; the
parent also matches `values`.

Find a custom field ID in its Jira settings URL (`id=10410` means `customfield_10410`) or through
Jira's `/rest/api/3/field` endpoint.

## Work to include

List whole epics and individual stories by key:

```toml
[epics.APP-100]
[epics.APP-200]

[stories.APP-310]
[stories.APP-320]
depends_on = ["APP-310"]
```

`depends_on` makes a story wait for the listed stories. Jira issue links named in `dependency_links`
work the same way.

Next: [learn how the planner schedules this work](planning.md).
