# Plan history and trends

Use `--history DIR` to store each plan as `DIR/plan-YYYY-MM-DD.json`. A later run on the same day
replaces that day's snapshot.

```bash
python jira_smurf.py gantt -o gantt.html --history plannings
```

Add `--since` to compare with the latest snapshot before today, or provide a date to use the latest
snapshot on or before it:

```bash
python jira_smurf.py gantt -o gantt.html --history plannings --since
python jira_smurf.py gantt -o gantt.html --history plannings --since 2026-10-01
```

When no snapshot exists on or before the requested date, the oldest snapshot is used. A warning
identifies any date substitution.

## Reading changes

The chart's **Show changes since …** checkbox controls comparison marks:

- yellow rows and `NEW` identify new stories;
- tooltips show estimate changes;
- thin gray bars show baseline dates;
- the Moved column shows the change in working days;
- yellow outlines identify work that should already have finished (set the color with
  `behind_baseline_color` in `[visuals]`);
- a final gray group lists open stories removed from the current plan.

The summary below the chart attributes changes in the plan end to new stories, changed estimates,
removed work, and late stories.

With at least two snapshots, the trend chart plots each epic's planned end at every run. A rising
line means the expected end moved later.
