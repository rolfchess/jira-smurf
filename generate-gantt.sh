#!/bin/sh
# Write today's Gantt chart and plan snapshot to plannings/ and open the chart.
# Extra arguments go to the gantt command, for example: --since 2026-10-01
set -e
mkdir -p plannings
out="plannings/gantt-$(date +%Y-%m-%d).html"
# Use the virtual environment in .venv when there is one
python=python3
[ -x .venv/bin/python ] && python=.venv/bin/python
"$python" jira_smurf.py gantt -c gantt.toml -o "$out" --history plannings "$@"
open "$out"
