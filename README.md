# jira-smurf

![Example Gantt chart](example-gantt.png)

`jira-smurf` is a command-line tool for Jira Cloud. It can:

- export a project's stories as CSV or HTML;
- schedule stories across a team and create an interactive Gantt chart;
- compare plans over time; and
- calculate the team's historical hours per story point.

## Quick start

You need Python 3.9 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env
cp gantt.example.toml gantt.toml
```

Add your Jira details to `.env`, then try:

```bash
python jira_smurf.py csv -o stories.csv
python jira_smurf.py gantt -o gantt.html
python jira_smurf.py serve --history plannings
```

The local server opens the chart at `http://localhost:8000/` and adds a **Refresh from Jira**
button. Stop it with Ctrl+C.

## Documentation

- [Installation and commands](documentation/getting-started.md)
- [Planning configuration](documentation/configuration.md)
- [How scheduling works](documentation/planning.md)
- [Using the Gantt chart](documentation/gantt-chart.md)
- [Plan history and trends](documentation/history.md)
- [Measuring velocity](documentation/velocity.md)
- [Sharing a chart with Google Drive](documentation/sharing.md)
- [Changes](CHANGES.md)

## License

[MIT](LICENSE)
