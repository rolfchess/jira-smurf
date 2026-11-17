# Measuring velocity

In `jira-smurf`, velocity means **team hours per story point**. A velocity of 7 makes a five-point
story a 35-hour estimate. Lower values therefore mean faster delivery.

Use the historical value instead of guessing:

```bash
python jira_smurf.py velocity
python jira_smurf.py velocity --weeks 4
```

By default, the command measures the last eight complete Monday-to-Sunday weeks. For each week it
divides the available team hours from `gantt.toml` by the points completed by team members.

| Week      | Available hours | Points done | Hours per point |
| --------- | --------------: | ----------: | --------------: |
| 1         |             120 |          15 |               8 |
| 2         |             120 |          10 |              12 |
| **Total** |         **240** |      **25** |         **9.6** |

Use the total—9.6 in this example—as `velocity` in `gantt.toml`. A longer representative period is
usually more stable than one unusually fast week.

Stories without points count as `default_hours ÷ velocity` points. For example, 40 default hours and
a velocity of 8 count as five points.

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
