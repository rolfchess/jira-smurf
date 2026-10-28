# Velocity: how fast does the team really work?

In our project, the planning kept sliding. New stories and bugs came in, and stories took longer
than we thought. I wanted to see where the time went, so I wrote this tool.

To be able to tell when the project (or an epic) will be done, we need to know how fast the team
moves through the work, and we need an estimate of all the work. The estimates are the points on a
story and they don't need to be exact. You can do T-shirt sizing (Large Medium Small) or do the
classic Fibonacci sequence 1, 2, 3, 5, 8 points etcetera. Don't spend too long on estimating,
because stories need to be small, and many small stories average out the errors.

The speed of the team is called "Velocity" in scrum and is typically expressed as Points per Sprint,
but since most teams spend more hours per point, this tool calculates Velocity as "Hours per Point".
So a Velocity of 8 in this tool means 1 point takes 8 hours to complete.

Or: With a velocity of 7, a story of 5 points takes 5 × 7 = 35 hours of team time.

To calculate the number of hours a story will take to complete, this tool:

1. Uses the points in the story
2. Multiplies that with the Velocity in the config file
3. Assigns those hours to the next available resource.

If a story has no points, the tool uses the `default_hours` setting from the config as the number of
expected hours in the story.

This is all done in the order in which the backlog is sorted. Top stories first, bottom stories
later. It does this for all stories, and this should give you a good indication of what the team
will be doing the coming weeks/months, and when stuff will be done.

## But the Velocity is configuration, how do I know the actual velocity?

This tool can measure the actual Velocity of a project based on the story points of stories that
have moved to "Done", and on the number of available hours of the team during that period. It does
this over a period of 8 weeks (if available), gives you the velocity per week, and the overall
velocity over 8 weeks:

Run this:

```bash
python jira_smurf.py velocity
```

The output could look somewhat like this for a team of three people who are available 8 hours a day,
5 days a week. That is 3 × 8 × 5 = 120 hours.

| Week   | Hours | Points done | Hours per point |
| ------ | ----- | ----------- | --------------- |
| Week 1 | 120   | 15          | 120 ÷ 15 = 8    |
| Week 2 | 120   | 10          | 120 ÷ 10 = 12   |
| Total  | 240   | 25          | 240 ÷ 25 = 9.6  |

One week says 8, the next says 12. For the planning, use the total over all weeks. Put it in
`gantt.toml` as `velocity`.

Note that the hours include everything the team does: meetings, support, bugs, coffee, unplanned
days off. That is on purpose. The plan has to make room for all the things a team does next to the
stories in the sprint. There is always some housekeeping and stuff you can't anticipate or estimate.

### Hours are availability

This tool ignores all hours written in Jira, so you do not have to bother writing them. In our
experience, writing hours in Jira does not make the planning more predictable and only makes the
administration more cumbersome and drains the energy of people who would rather do something else.

Instead, we can configure availability of people in the `[team]` section of the configuration. The
tool assumes that people are there on those days. Sick days, a dentist visit or an evening of extra
work make the real hours different. That error is accepted as a fact of life, and will be reflected
in the calculated average velocity.

It is tempting to make room for meetings by lowering the available hours of team members. For
example, you could set 6 hours on Monday and Friday instead of 8. **Don't do that**. It will pollute
the velocity calculation because you are trying to hide stuff under the carpet.

- **Hours:** how many hours each person is available for the project. Change them only when the
  availability changes, for example for a person who works 4 days a week.
- **Velocity:** how many of those hours one story point takes, **with meetings and everything else
  included**.

The tool measures the past weeks with the hours in `gantt.toml` as they are now. So when you fix the
hours, run `python jira_smurf.py velocity` again and use the new velocity. Try to find the slowest
moving average velocity (so the longest period you can use) and use that as configuration. Don't
change it too often or too radically, it will make your planning unpredictable for everyone.

### Done means released

The points of a story count on the day the story moves to Done, and not earlier. So Done must mean
that the story is released. That is the moment the customer can use it, and the moment there is no
work left on it.

Do not move a story to Done when the code is merged or tested, but not yet released. There is still
work left, and the customer has nothing yet. The velocity then might look better than it is, and
**the plan promises dates that are too early**. That is unfair to the customer and the team.

This only works when the team releases often, ideally every story as soon as it is ready. You will
need to be able to do that anyway: A team that releases once every five weeks cannot fix a zero-day
security problem within an hour either.

To summarize:

- Always give stories points
- Move a story to Done when it is released, available to the customer and 0 work left for the team
- Keep stories small. It is motivating for the team and will increase resolution and predictability
- Assign the story to the person who is working on it
- Don't change points during or after the work is done.
- Don't change availability to compensate for meetings or other work the team is doing.
- Never reopen a story. If you need to fix something, create a new story or bug.
- Your velocity is not someone else's. A velocity is only relevant for your team and project and
  says nothing about other projects and teams within a company.
- Use the stories in daily standup, and always keep stories up to date when you work on them, don't
  postpone administration to the end of the week.

If the data in Jira is clean, the velocity stays steady, and so do the dates in the planning.

Cheers!
