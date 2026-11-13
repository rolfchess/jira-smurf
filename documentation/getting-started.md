# Installation and commands

## Requirements

- Python 3.9 or newer
- A Jira Cloud account and API token

Install the dependencies and create local configuration files:

```bash
pip install -r requirements.txt
cp .env.example .env
cp gantt.example.toml gantt.toml
```

Both local files are ignored by Git. Keep your API token in `.env`, never in source control.

## Jira connection

Set these values in `.env`:

| Variable                  | Purpose                                                                                        |
| ------------------------- | ---------------------------------------------------------------------------------------------- |
| `JIRA_URL`                | Jira Cloud URL, such as `https://yourorg.atlassian.net`                                        |
| `JIRA_EMAIL`              | Atlassian account email                                                                        |
| `JIRA_API_TOKEN`          | Token from [Atlassian API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `JIRA_PROJECT`            | Project key, such as `PROJ`                                                                    |
| `JIRA_STORY_POINTS_FIELD` | Optional story-points field; defaults to `story_points`, then tries `customfield_10016`        |
| `JIRA_SPRINT_FIELD`       | Optional sprint field; defaults to `customfield_10020`                                         |
| `JIRA_EXCLUDE_STATUSES`   | Optional comma-separated statuses omitted from exports, such as `Done,Cancelled`               |
| `JIRA_HIGHLIGHT_RULES`    | Optional HTML-export colors, such as `Assignee=Alice:yellow\|Status=Blocked:#ffcccc`           |

Use `python jira_smurf.py fields` to find the story-points field.

## Commands

```bash
# Export stories
python jira_smurf.py csv
python jira_smurf.py csv -o stories.csv
python jira_smurf.py html -o stories.html

# Create or serve a chart
python jira_smurf.py gantt -o gantt.html
python jira_smurf.py gantt -c other.toml -o gantt.html
python jira_smurf.py serve --history plannings
python jira_smurf.py serve --port 8080 --no-open

# Compare plans
python jira_smurf.py gantt -o gantt.html --history plannings --since
python jira_smurf.py gantt -o gantt.html --history plannings --since 2026-10-01

# Measure velocity
python jira_smurf.py velocity
python jira_smurf.py velocity --weeks 4
```

Run `python jira_smurf.py --help` or add `--help` after a command for all options. Progress is
written to standard error, so it does not contaminate exported output.

## Convenience script

On macOS, `./generate-gantt.sh` creates `plannings/gantt-YYYY-MM-DD.html`, stores plan history, and
opens the chart. It uses `.venv/bin/python` when available. Extra arguments are passed to `gantt`:

```bash
./generate-gantt.sh --since 2026-10-01
```

Next: [configure the plan](configuration.md).
