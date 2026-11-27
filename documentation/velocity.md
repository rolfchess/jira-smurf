# Measuring velocity

In `jira-smurf`, velocity means **team hours per story point**. A velocity of 7 makes a five-point
story a 35-hour estimate. Lower values therefore mean faster delivery.

Use the historical value instead of guessing:

```bash
python jira_smurf.py velocity
python jira_smurf.py velocity --weeks 4
```

By default, the command measures the last eight complete Monday-to-Sunday weeks. For each week it
divides the available team hours from `gantt.toml` by the points completed by team members. For
example, with `--weeks 2`:

```text
Week         Hours  Stories  Points  Hours/point  No points
2026-W46       120        3    15.0          8.0          1
2026-W47       120        3    10.0         12.0          0
Total          240        6    25.0          9.6          1

Velocity over the last 2 weeks: 9.6 hours per story point. gantt.toml uses 8.0.
1 finished story had no story points. Each counts as 5.0 points: default_hours 40 divided by velocity 8.
```

Hours are the available team hours of the week. Stories is the number of finished stories with
points, and No points the number without. Points adds up both: in week 46, three stories with 10
points and one story without points, which counts as 5.

Use the total—9.6 in this example—as `velocity` in `gantt.toml`. A longer representative period is
usually more stable than one unusually fast week.

Stories without points count as `default_hours ÷ velocity` points. For example, 40 default hours and
a velocity of 8 count as five points.

The command counts the finished issues of the types in `issue_types`, which is only stories by
default. With `issue_types = ["Story", "Bug"]`, finished bugs count too, and a bug without points
counts as `default_hours ÷ velocity` points.

## Keep the measurement useful

Availability means hours assigned to the project, not uninterrupted development time. Meetings,
support, bugs, and normal interruptions belong in velocity. Do not reduce availability to account
for them; change it only when a person's actual schedule changes.

For reliable results:

- keep team membership dates and days off current;
- estimate every story and keep stories small;
- assign work to the person doing it;
- mark work Done only when it is released and no work remains;
- do not change points after work starts; and
- create a new story or bug instead of reopening completed work.

The command applies today's `gantt.toml` to the whole measurement period. Use `in_team` to record
join and leave dates. Recalculate velocity after correcting availability.

Velocity is specific to a team and project. It is a planning input, not a performance score or a
useful comparison between teams.

See [planning configuration](configuration.md) for the related settings.
