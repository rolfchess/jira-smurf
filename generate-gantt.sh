#!/bin/sh
# Write today's Gantt chart to plannings/ and open it.
set -e
mkdir -p plannings
out="plannings/gantt-$(date +%Y-%m-%d).html"
python3 jira_smurf.py gantt -c gantt.toml -o "$out"
open "$out"
