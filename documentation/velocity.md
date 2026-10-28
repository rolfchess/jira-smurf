# Velocity: how fast does the team really work?

Every planning starts with the same question: when will it be done? To answer that, you need to
know how much work there is, and how fast the team works. The first part is in Jira. The second part
is the velocity. If the velocity is wrong, every date in the planning is wrong, no matter how
pretty the Gantt chart looks.

## What is velocity?

Story points say how big a story is. They do not say how long it takes. Velocity is the number that
turns points into time.

Many teams count velocity as points per sprint. This tool counts it the other way around: **hours
per story point**. With a velocity of 7, a story of 5 points takes 5 × 7 = 35 hours of team time.

The planner uses this number for every story. So if the velocity is 7 but the team really needs 9
hours per point, every story gets too few hours in the plan, and the end date is too early.

## How this tool measures it

Run this:

```bash
python jira_smurf.py velocity
```

The tool looks at the last 8 full weeks. For each week it does a simple sum:

- **Hours:** the hours of everyone in `[team]` in `gantt.toml`, minus days off and holidays.
- **Points:** the story points of the stories that moved to a done status that week, such as Done
  or Closed, if they are assigned to someone in the team.
- **Velocity:** hours ÷ points.

Here is an example. Three people are available 8 hours a day, 5 days a week. That is 3 × 8 × 5 = 120
hours.

| Week   | Hours | Points done | Hours per point |
| ------ | ----- | ----------- | --------------- |
| Week 1 | 120   | 15          | 120 ÷ 15 = 8    |
| Week 2 | 120   | 10          | 120 ÷ 10 = 12   |
| Total  | 240   | 25          | 240 ÷ 25 = 9.6  |

One week says 8, the next says 12. Neither is wrong, but neither is the whole truth. The total over
all weeks is the number to use. Put it in `gantt.toml` as `velocity`.

Note that the hours include everything the team does: meetings, support, bugs, coffee. That is on
purpose. The plan has to make room for that work too.

### Hours are availability

This tool ignores all hours written in Jira, so you do not have to write them. In our experience,
writing hours in Jira does not make the planning more predictable. The hours in `[team]` are the
availability: how many hours each person is available for the project on each day of the week. The tool assumes that people are there on
those days. Sick days, a dentist visit or an evening of extra work make the real hours different.
That error is accepted, because it ends up in the velocity.

It is tempting to make room for meetings by lowering the hours. For example, you could set 6 hours
on Monday and Friday instead of 8. Don't do that. The velocity already includes meetings and all other work,
because it is measured over all the hours. Lower hours only hide part of that work in a guess. And
that guess never changes, while the velocity follows the real situation week by week.

So keep the two apart:

- **Hours:** how many hours each person is available for the project. Change them only when the
  availability changes, for example for a person who works 4 days a week.
- **Velocity:** how many of those hours one story point takes, with meetings and everything else
  included.

The tool measures the past weeks with the hours in `gantt.toml` as they are now. So when you fix the
hours, run `python jira_smurf.py velocity` again and use the new velocity.

## Why velocity jumps around

The sum is simple, but it can only be as good as the data in Jira. These are the usual causes of a
velocity that jumps around:

- **A story without points.** The team spent hours on it, but it adds 0 points. The velocity goes
  up.
- **A story that is moved to Done too late.** The tool uses the last day the story moved to a done
  status. If the team closes a whole sprint of stories on Friday afternoon, all those points land in
  one week.
- **A big story.** A story of 13 points that took three weeks counts all 13 points in its last
  week. The two weeks before it look slow, the last week looks fast.
- **A wrong team.** If someone joins, leaves or goes on holiday and `gantt.toml` does not know, the
  hours are wrong.

## Do's

- **Give every story points before work starts.** Even a rough guess is better than nothing.
- **Move a story to Done on the day it is done.** Do not wait until the end of the sprint or until
  after the demo. The date of that move is the only completion date the tool knows.
- **Keep stories small.** Small stories spread the points evenly over the weeks.
- **Assign the story to the person who did the work.** Stories of people outside `[team]` do not
  count.
- **Keep `gantt.toml` up to date.** Add days off and holidays, and change `[team]` when the team
  changes.
- **Look at the total, not at one week.** One week is noise. Eight weeks is a trend.

## Don'ts

- **Don't change the points after the work is done** to match the time it took. The points say how
  big the story was. The velocity says how fast the team was. If you change the points afterwards,
  you hide the real velocity.
- **Don't lower the hours in `gantt.toml` to make room for meetings.** The velocity already
  includes them.
- **Don't reopen a done story for a small fix.** When the story moves to Done again, its points
  move to that later week. Make a new bug or story instead.
- **Don't use velocity to compare people or teams.** Points mean something different in every
  team. Velocity is a planning tool, not a score.

Keep the data clean, and the velocity becomes a boring, steady number. Boring is exactly what you
want from it.
