# Plan history and trends

Use `--history DIR` to store each plan as `DIR/plan-YYYY-MM-DD.json`. A later run on the same day
replaces that day's snapshot.

```bash
python jira_smurf.py gantt -o gantt.html --history plannings
```

## Comparing with an earlier plan

With snapshots from before today, the toolbar has a **Compare with plan** list with every earlier
snapshot, newest first. Pick one to see how the current plan moved since then; pick **none** to
hide the comparison. The chart keeps your choice in its address, as `?compare=2026-11-18` or
`?compare=none`, so a reload, a refresh from Jira, or a link you share shows the same comparison.
A date without a snapshot uses the latest snapshot before it.

Without `compare` in the address, the chart shows no comparison. Add `--since` to compare with
the latest snapshot before today at first, or provide a date to use the latest snapshot on or
before it:

```bash
python jira_smurf.py gantt -o gantt.html --history plannings --since
python jira_smurf.py gantt -o gantt.html --history plannings --since 2026-10-01
```

When no snapshot exists on or before the requested date, the oldest snapshot is used. A warning
identifies any date substitution.

## Reading changes

While a plan is compared:

- a thin striped line under each bar shows its dates in that plan, in the dark shade of the bar
  color. Stories, epics and space rows have one; the tooltip gives the dates and how many working
  days the end moved;
- yellow rows and `NEW` identify new stories;
- tooltips show estimate changes;
- the Moved column shows the change in working days. For a story that was open in that plan and is
  done now, it shows how much earlier or later it finished than planned; its tooltip also compares
  the working days it took with its estimate;
- the line of an epic or space covers the same stories as its bar: with done stories hidden, only
  its open stories;
- yellow outlines identify work that should already have finished (set the color and the blinking
  with `behind_baseline_color` and `behind_baseline_blink` in `[visuals]`);
- a final gray group lists the stories that were open in that plan and are no longer in the current
  plan, with their dates and status in that plan.

The summary below the chart attributes changes in the plan end to new stories, changed estimates,
removed work, and late stories.

With at least two snapshots, the trend chart plots each epic's planned end at every run. A rising
line means the expected end moved later.
