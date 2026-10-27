#!/usr/bin/env python3
"""Export Jira Cloud stories as CSV or HTML, or plan them in a Gantt chart."""

import argparse
import contextlib
import csv
import heapq
import html
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_PROJECT = os.environ.get("JIRA_PROJECT", "")
STORY_POINTS_FIELD = os.environ.get("JIRA_STORY_POINTS_FIELD", "story_points")
SPRINT_FIELD = os.environ.get("JIRA_SPRINT_FIELD", "customfield_10020")
EXCLUDE_STATUSES = [
    s.strip().lower()
    for s in os.environ.get("JIRA_EXCLUDE_STATUSES", "").split(",")
    if s.strip()
]

# Highlight rules: list of (column, value, color) tuples
# The rules come from a value such as "Assignee=Alice:yellow|Status=Blocked:#ffcccc"
HIGHLIGHT_RULES = []
for rule in os.environ.get("JIRA_HIGHLIGHT_RULES", "").split("|"):
    rule = rule.strip()
    if not rule:
        continue
    try:
        match_part, color = rule.rsplit(":", 1)
        col, val = match_part.split("=", 1)
        HIGHLIGHT_RULES.append((col.strip(), val.strip().lower(), color.strip()))
    except ValueError:
        print(f"Warning: ignoring invalid highlight rule: {rule}", file=sys.stderr)

PAGE_SIZE = 100


def validate_config():
    missing = []
    for var in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"Error: missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy .env.example to .env and fill in your values.", file=sys.stderr)
        sys.exit(1)


def jira_get(path, params=None):
    """Make an authenticated GET request to the Jira REST API."""
    url = f"{JIRA_URL}/rest/api/3/{path}"
    resp = requests.get(url, params=params, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_stories():
    """Fetch all stories for the project, paginating through results."""
    jql = f"project = {JIRA_PROJECT} AND issuetype = Story ORDER BY key ASC"
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10028"
    issues = []
    next_page_token = None

    while True:
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": PAGE_SIZE,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        data = jira_get("search/jql", params=params)
        issues.extend(data["issues"])

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return issues


def resolve_epic_name(issue):
    """Return the name of the epic that a story belongs to, or "(No Epic)"."""
    fields = issue["fields"]

    # Next-gen projects: epic is the parent issue
    parent = fields.get("parent")
    if parent:
        parent_fields = parent.get("fields", {})
        # parent.fields.summary is the epic name
        name = parent_fields.get("summary")
        if name:
            return name

    # Classic projects: customfield_10014 holds the epic key
    epic_key = fields.get("customfield_10014")
    if epic_key:
        try:
            epic = jira_get(f"issue/{epic_key}", params={"fields": "summary"})
            return epic["fields"]["summary"]
        except requests.HTTPError:
            return epic_key  # fall back to key if we can't fetch it

    return "(No Epic)"


def get_story_points(fields):
    """Extract story points, trying the configured field and common defaults."""
    for field_name in (STORY_POINTS_FIELD, "story_points", "customfield_10028"):
        val = fields.get(field_name)
        if val is not None:
            return str(val).replace(".", ",")
    return ""


def get_sprint(fields):
    """Extract the most recent sprint name."""
    sprints = fields.get(SPRINT_FIELD)
    if sprints:
        # Sprint field is a list of sprint objects; take the last (most recent) one
        return sprints[-1].get("name", "") if isinstance(sprints, list) else ""
    return ""


def get_assignee(fields):
    assignee = fields.get("assignee")
    if assignee:
        return assignee.get("displayName", "")
    return "Unassigned"


def get_story_points_numeric(fields):
    """Extract story points as a float, or None if not set."""
    for field_name in (STORY_POINTS_FIELD, "story_points", "customfield_10028"):
        val = fields.get(field_name)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


@dataclass
class GanttConfig:
    start_date: date
    team: dict  # {name: {mon: N, tue: N, ...}}
    stories: list  # [key, ...] — ordered list of story keys
    epics: list  # [key, ...] — ordered list of epic keys
    statuses: dict  # {lowercase_name: progress_0_to_1}
    default_hours: float
    velocity: float
    dependencies: dict  # {story_key: [dep_key, ...]}
    include_open_sprints: bool
    include_future_sprints: bool


def load_gantt_config(path="gantt.toml"):
    """Parse and validate gantt.toml config file."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        print(f"Error: {path} not found. Create it with [team] and [stories] sections.", file=sys.stderr)
        sys.exit(1)

    # Validate start_date
    start_str = raw.get("start_date")
    if not start_str:
        print(f"Error: 'start_date' missing in {path}", file=sys.stderr)
        sys.exit(1)
    try:
        start = date.fromisoformat(start_str)
    except ValueError:
        print(f"Error: 'start_date' must be YYYY-MM-DD, got '{start_str}'", file=sys.stderr)
        sys.exit(1)

    # Warn and advance weekend start dates to Monday
    if start.weekday() >= 5:  # Saturday=5, Sunday=6
        monday = start + timedelta(days=(7 - start.weekday()))
        print(f"Warning: start_date {start} is a weekend, advancing to {monday}", file=sys.stderr)
        start = monday

    # Validate team
    team = raw.get("team", {})
    if not team:
        print(f"Error: [team] section missing or empty in {path}", file=sys.stderr)
        sys.exit(1)
    for name, avail in team.items():
        auto_assign = avail.get("auto_assign", True)
        if not isinstance(auto_assign, bool):
            print(f"Error: 'auto_assign' for team member '{name}' must be true or false, got {auto_assign!r}", file=sys.stderr)
            sys.exit(1)
        if auto_assign and not has_working_hours(avail):
            print(f"Error: team member '{name}' has no working hours configured. "
                  f"Add hours or set auto_assign = false", file=sys.stderr)
            sys.exit(1)
    if not any(avail.get("auto_assign", True) for avail in team.values()):
        print(f"Error: no team member in {path} has auto_assign enabled", file=sys.stderr)
        sys.exit(1)

    # Global scheduling settings
    default_hours = raw.get("default_hours", 40)
    if default_hours <= 0:
        print(f"Error: 'default_hours' must be > 0", file=sys.stderr)
        sys.exit(1)
    velocity = raw.get("velocity", 8.0)
    if velocity <= 0:
        print(f"Error: 'velocity' must be > 0", file=sys.stderr)
        sys.exit(1)

    # Parse optional [statuses] section — maps status name to progress %
    statuses_raw = raw.get("statuses", {})
    statuses = {name.lower(): val / 100.0 for name, val in statuses_raw.items()}
    for name, val in statuses_raw.items():
        if not (0 <= val <= 100):
            print(f"Error: status '{name}' progress must be 0–100, got {val}", file=sys.stderr)
            sys.exit(1)

    # Parse optional [epics] section
    epics_raw = raw.get("epics", {})
    epic_keys = list(epics_raw.keys())

    # Parse optional [stories] section
    stories_raw = raw.get("stories", {})
    story_keys = list(stories_raw.keys())

    # Parse optional depends_on from story sub-tables
    dependencies = {}
    for key, val in stories_raw.items():
        if isinstance(val, dict) and val.get("depends_on"):
            dependencies[key] = val["depends_on"]

    include_open_sprints = raw.get("include_open_sprints", False)
    include_future_sprints = raw.get("include_future_sprints", False)
    for name, val in (("include_open_sprints", include_open_sprints),
                      ("include_future_sprints", include_future_sprints)):
        if not isinstance(val, bool):
            print(f"Error: '{name}' must be true or false, got {val!r}", file=sys.stderr)
            sys.exit(1)

    if not epic_keys and not story_keys and not include_open_sprints and not include_future_sprints:
        print(f"Error: [epics] and [stories] are missing or empty in {path}, "
              f"and no sprints are included", file=sys.stderr)
        sys.exit(1)

    return GanttConfig(start_date=start, team=team, stories=story_keys,
                       epics=epic_keys, statuses=statuses,
                       default_hours=default_hours, velocity=velocity,
                       dependencies=dependencies,
                       include_open_sprints=include_open_sprints,
                       include_future_sprints=include_future_sprints)


def fetch_stories_for_epic(epic_key):
    """Fetch all stories belonging to an epic.

    Tries next-gen (parent) first, falls back to classic (Epic Link).
    Returns list of issue dicts.
    """
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10028"

    for jql in (
        f"parent = {epic_key} AND issuetype = Story ORDER BY key ASC",
        f'"Epic Link" = {epic_key} AND issuetype = Story ORDER BY key ASC',
    ):
        issues = []
        next_page_token = None
        try:
            while True:
                params = {
                    "jql": jql,
                    "fields": fields,
                    "maxResults": PAGE_SIZE,
                }
                if next_page_token:
                    params["nextPageToken"] = next_page_token
                data = jira_get("search/jql", params=params)
                issues.extend(data["issues"])
                next_page_token = data.get("nextPageToken")
                if not next_page_token:
                    break
        except requests.HTTPError as e:
            print(f"Warning: JQL query for epic {epic_key} failed ({e}), trying fallback", file=sys.stderr)
            continue

        if issues:
            return issues

    return []


def fetch_stories_by_keys(story_keys):
    """Fetch specific stories by their Jira keys."""
    keys_str = ", ".join(story_keys)
    jql = f"key in ({keys_str})"
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10028"
    issues = []
    next_page_token = None

    while True:
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": PAGE_SIZE,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        data = jira_get("search/jql", params=params)
        issues.extend(data["issues"])
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    # Index by key
    by_key = {issue["key"]: issue for issue in issues}

    # Warn about missing stories
    for key in story_keys:
        if key not in by_key:
            print(f"Warning: story {key} not found in Jira, skipping", file=sys.stderr)

    return by_key


def fetch_sprint_stories(include_open, include_future):
    """Fetch the project's stories in open sprints, future sprints or both."""
    sprint_functions = []
    if include_open:
        sprint_functions.append("openSprints()")
    if include_future:
        sprint_functions.append("futureSprints()")
    sprint_clause = " OR ".join(f"sprint in {f}" for f in sprint_functions)
    jql = f"project = {JIRA_PROJECT} AND issuetype = Story AND ({sprint_clause}) ORDER BY key ASC"
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10028"
    issues = []
    next_page_token = None

    while True:
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": PAGE_SIZE,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        data = jira_get("search/jql", params=params)
        issues.extend(data["issues"])
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return issues


def resolve_epic_for_issue(issue, epic_cache):
    """Resolve epic name for an issue, using and updating the cache."""
    cache_key = None
    parent = issue["fields"].get("parent")
    if parent:
        cache_key = parent.get("key")
    else:
        cache_key = issue["fields"].get("customfield_10014")

    if cache_key and cache_key in epic_cache:
        return epic_cache[cache_key]

    epic_name = resolve_epic_name(issue)
    if cache_key:
        epic_cache[cache_key] = epic_name
    return epic_name


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def next_working_day(d, availability):
    """Advance to the next day where this member has hours > 0."""
    while True:
        day_key = DAY_KEYS[d.weekday()]
        if availability.get(day_key, 0) > 0:
            return d
        d += timedelta(days=1)


def has_working_hours(availability):
    """Return True if the member has hours on at least one weekday."""
    return sum(availability.get(d, 0) for d in ("mon", "tue", "wed", "thu", "fri")) > 0


def get_earliest_available_member(member_states, team):
    """Return the name of the auto-assign member who becomes free earliest."""
    candidates = [name for name in member_states if team[name].get("auto_assign", True)]
    return min(candidates, key=lambda name: member_states[name])


def topological_sort(keys, dependencies):
    """Sort keys so dependencies come before dependents.

    Uses Kahn's algorithm with original index as tiebreaker
    to preserve the input order where possible.
    Keys involved in circular deps are appended at the end.
    """
    key_set = set(keys)
    in_degree = {k: 0 for k in keys}
    dependents = {k: [] for k in keys}

    for key in keys:
        for dep in dependencies.get(key, []):
            if dep in key_set:
                in_degree[key] += 1
                dependents[dep].append(key)

    key_index = {k: i for i, k in enumerate(keys)}
    queue = [(key_index[k], k) for k in keys if in_degree[k] == 0]
    heapq.heapify(queue)

    result = []
    while queue:
        _, key = heapq.heappop(queue)
        result.append(key)
        for dep in dependents[key]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                heapq.heappush(queue, (key_index[dep], dep))

    # Append any remaining keys (circular dependencies)
    remaining = [k for k in keys if k not in set(result)]
    if remaining:
        print(f"Warning: circular dependencies detected among {remaining}", file=sys.stderr)
        result.extend(remaining)

    return result


def schedule_stories(config, story_data, ordered_keys):
    """Schedule stories onto team members based on availability.

    ordered_keys is the merged, deduplicated list of story keys (epics first, then individual).
    Returns list of dicts with keys:
        key, title, epic, assignee, status, start, end
    """
    # Topologically sort so dependencies are always scheduled before dependents
    ordered_keys = topological_sort(ordered_keys, config.dependencies)

    # Schedule done tasks first so they don't inflate epic end dates.
    # Done tasks don't advance resource availability, so this is safe.
    def _done_first(key):
        if key not in story_data:
            return 1
        status = story_data[key]["fields"].get("status", {}).get("name", "")
        progress = config.statuses.get(status.lower(), 0)
        return 0 if progress >= 1.0 else 1

    ordered_keys = sorted(ordered_keys, key=_done_first)

    # member_states tracks the next available date per member
    member_states = {}
    for name in config.team:
        if has_working_hours(config.team[name]):
            member_states[name] = next_working_day(config.start_date, config.team[name])

    scheduled = []
    story_end_dates = {}  # {story_key: end_date} for dependency resolution

    for story_key in ordered_keys:
        if story_key not in story_data:
            continue

        issue = story_data[story_key]
        title = issue["fields"]["summary"]
        status = issue["fields"].get("status", {}).get("name", "")
        assignee_name = get_assignee(issue["fields"])
        sprint = get_sprint(issue["fields"])

        # Check if story is fully done via status mapping
        progress = config.statuses.get(status.lower(), 0)
        sp = get_story_points_numeric(issue["fields"])
        if sp is not None:
            total_hours = sp * config.velocity
        else:
            total_hours = config.default_hours

        # Scale down remaining hours by progress (done stories keep full estimate)
        hours = total_hours if progress >= 1.0 else total_hours * (1.0 - progress)

        # Determine who works on this story
        if assignee_name in member_states:
            member = assignee_name
        else:
            if assignee_name in config.team:
                print(f"Warning: assignee '{assignee_name}' for {story_key} has no working hours, auto-assigning", file=sys.stderr)
            elif assignee_name != "Unassigned":
                print(f"Warning: assignee '{assignee_name}' for {story_key} not in [team], auto-assigning", file=sys.stderr)
            member = get_earliest_available_member(member_states, config.team)

        avail = config.team[member]
        cursor = member_states[member]

        # Apply dependency constraints: story cannot start before all deps finish
        deps = config.dependencies.get(story_key, [])
        if deps:
            for dep_key in deps:
                if dep_key in story_end_dates:
                    dep_end = story_end_dates[dep_key]
                    if dep_end > cursor:
                        cursor = dep_end
                elif dep_key not in story_data:
                    print(f"Warning: dependency {dep_key} for {story_key} not in scheduled set, ignoring", file=sys.stderr)

        # Non-done stories cannot start in the past
        if progress < 1.0:
            today = date.today()
            if cursor < today:
                cursor = today

        cursor = next_working_day(cursor, avail)
        start_date = cursor
        remaining = hours

        if remaining <= 0:
            # Zero-hour story: give it a 1-day bar so it's visible
            end_date = cursor
            cursor += timedelta(days=1)
        else:
            while remaining > 0:
                cursor = next_working_day(cursor, avail)
                day_key = DAY_KEYS[cursor.weekday()]
                day_hours = avail.get(day_key, 0)
                remaining -= day_hours
                end_date = cursor
                cursor += timedelta(days=1)

        if progress < 1.0:
            # Only advance availability for stories that aren't done
            member_states[member] = cursor
        story_end_dates[story_key] = cursor

        scheduled.append({
            "key": story_key,
            "title": title,
            "epic": issue.get("_epic", "(No Epic)"),
            "epic_key": issue.get("_epic_key", ""),
            "assignee": member,
            "status": status,
            "sprint": sprint,
            "effort": 0 if progress >= 1.0 else hours,
            "total_hours": total_hours,
            "start": start_date.isoformat(),
            "end": (end_date + timedelta(days=1)).isoformat(),
        })

    return scheduled


EPIC_COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]


def write_gantt_html(scheduled_stories, status_map, dependencies=None):
    """Write a self-contained HTML file with a DHTMLX Gantt chart."""
    # Assign colors to epics
    epics_seen = []
    for s in scheduled_stories:
        if s["epic"] not in epics_seen:
            epics_seen.append(s["epic"])
    epic_color_map = {epic: EPIC_COLORS[i % len(EPIC_COLORS)] for i, epic in enumerate(epics_seen)}

    # Sort: epic order, done stories first, then by start date
    def story_sort_key(s):
        epic_order = epics_seen.index(s["epic"])
        done = 0 if status_map.get(s["status"].lower(), 0) >= 1.0 else 1
        return (epic_order, done, s["start"], s["key"])

    sorted_stories = sorted(scheduled_stories, key=story_sort_key)

    # Collect unknown statuses to warn once per status
    unknown_statuses = set()

    # Build DHTMLX tasks array — create epic parent tasks, then story children
    tasks = []
    epic_task_ids = {}
    task_id = 0

    # Build epic_name -> epic_key mapping from stories
    epic_name_to_key = {}
    for s in sorted_stories:
        if s["epic"] not in epic_name_to_key and s.get("epic_key"):
            epic_name_to_key[s["epic"]] = s["epic_key"]

    # Create a parent task per epic
    for epic_name in epics_seen:
        task_id += 1
        epic_task_ids[epic_name] = task_id
        # Compute epic date span, total effort, and progress from its stories
        epic_stories = [s for s in sorted_stories if s["epic"] == epic_name]
        epic_start = min(s["start"] for s in epic_stories)
        epic_end = max(s["end"] for s in epic_stories)
        epic_effort = sum(s.get("effort", 0) for s in epic_stories)
        effort_str = f"{epic_effort:.0f}h" if epic_effort == int(epic_effort) else f"{epic_effort:.1f}h"

        # Weighted average progress (by total_hours so done stories count fully)
        total_weight = 0
        weighted_progress = 0
        for s in epic_stories:
            status = s["status"].lower()
            progress = status_map.get(status, 0)
            weight = s.get("total_hours", 1)
            weighted_progress += progress * weight
            total_weight += weight
        epic_progress = weighted_progress / total_weight if total_weight > 0 else 0

        tasks.append({
            "id": task_id,
            "text": epic_name,
            "start_date": epic_start,
            "end_date": epic_end,
            "effort": effort_str,
            "status": "",
            "color": epic_color_map[epic_name],
            "type": "project",
            "open": True,
            "story_key": epic_name_to_key.get(epic_name, ""),
            "progress": epic_progress,
        })

    # Create story tasks as children of their epic
    story_key_to_task_id = {}
    for s in sorted_stories:
        task_id += 1
        story_key_to_task_id[s["key"]] = task_id
        status = s["status"].lower()
        if status in status_map:
            progress = status_map[status]
        else:
            progress = 0
            if status:
                unknown_statuses.add(s["status"])

        effort = s.get("effort", 0)
        effort_str = f"{effort:.0f}h" if effort == int(effort) else f"{effort:.1f}h"

        tasks.append({
            "id": task_id,
            "text": f"{s['key']}: {s['title']}",
            "assignee": s["assignee"],
            "start_date": s["start"],
            "end_date": s["end"],
            "progress": progress,
            "effort": effort_str,
            "status": s["status"],
            "color": epic_color_map[s["epic"]],
            "parent": epic_task_ids[s["epic"]],
            "story_key": s["key"],
        })

    # Build dependency links
    links = []
    if dependencies:
        link_id = 0
        for story_key, dep_keys in dependencies.items():
            if story_key not in story_key_to_task_id:
                continue
            target_id = story_key_to_task_id[story_key]
            for dep_key in dep_keys:
                if dep_key not in story_key_to_task_id:
                    continue
                link_id += 1
                links.append({
                    "id": link_id,
                    "source": story_key_to_task_id[dep_key],
                    "target": target_id,
                    "type": "0",  # finish-to-start
                })

    for name in sorted(unknown_statuses):
        print(f"Warning: status '{name}' not in [statuses], using 0% progress", file=sys.stderr)

    tasks_json = json.dumps(tasks, indent=2)
    links_json = json.dumps(links, indent=2)

    # Build legend HTML
    legend_items = []
    for epic, color in epic_color_map.items():
        legend_items.append(
            f'<span style="display:inline-flex;align-items:center;margin-right:18px;">'
            f'<span style="display:inline-block;width:16px;height:16px;background:{color};'
            f'border-radius:3px;margin-right:6px;"></span>{html.escape(epic)}</span>'
        )
    legend_html = "\n".join(legend_items)

    print(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Gantt Chart — {date.today().strftime('%B %-d, %Y')}</title>
<link rel="stylesheet" href="https://cdn.dhtmlx.com/gantt/9.0/dhtmlxgantt.css">
<style>
  body {{ font-family: sans-serif; margin: 20px; }}
  #legend {{ margin-bottom: 16px; padding: 10px; background: #f8f8f8; border-radius: 6px; }}
  #gantt_here {{ width: 100%; height: calc(100vh - 160px); }}
  .gantt_task_line.task-done {{ opacity: 0.45; filter: grayscale(60%); }}
  .gantt_task_line.task-done .gantt_task_content {{ text-decoration: line-through; }}
  .gantt_row.task-done-row {{ color: #999; }}
  .gantt_task_line .gantt_task_progress {{ opacity: 0.35; }}
  #mouse-ruler {{ position: absolute; top: 0; bottom: 0; width: 1px; background: rgba(0,0,0,0.25); pointer-events: none; z-index: 999; display: none; }}
  .weekend {{ background: #f0f0f0; }}
  .today-marker .gantt_marker_content {{ background: #e33; color: #fff; font-size: 11px; padding: 2px 6px; }}
  .today-marker {{ background: #e33; }}
  #toolbar {{ margin-bottom: 12px; display: flex; align-items: center; gap: 16px; }}
  #toolbar .zoom-buttons {{ display: flex; gap: 4px; }}
  #toolbar button {{
    padding: 5px 14px; border: 1px solid #ccc; border-radius: 4px;
    background: #fff; cursor: pointer; font-size: 13px;
  }}
  #toolbar button:hover {{ background: #e8e8e8; }}
  #toolbar button.active {{ background: #4e79a7; color: #fff; border-color: #4e79a7; }}
  #hide-done-wrap {{ display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }}
  #search-wrap {{ display: flex; align-items: center; gap: 6px; margin-left: auto; }}
  #search-input {{
    padding: 5px 10px; border: 1px solid #ccc; border-radius: 4px;
    font-size: 13px; min-width: 220px;
  }}
  #search-input:focus {{ outline: none; border-color: #4e79a7; }}
  #search-count {{ font-size: 12px; color: #666; min-width: 70px; }}
  .gantt_task_line.search-match {{ outline: 3px solid #ffd400; outline-offset: 2px; z-index: 50; }}
  .gantt_task_line.search-dim {{ opacity: 0.2; }}
  .gantt_row.search-match-row {{ background: #fff7c2 !important; font-weight: 600; }}
  .gantt_row.search-dim-row {{ opacity: 0.45; }}
</style>
</head>
<body>
<h2>Gantt Chart — {date.today().strftime('%B %-d, %Y')}</h2>
<div id="toolbar">
  <div class="zoom-buttons">
    <button onclick="setZoom('day')">Day</button>
    <button onclick="setZoom('week')">Week</button>
    <button onclick="setZoom('month')">Month</button>
    <button onclick="setZoom('quarter')">Quarter</button>
    <button onclick="zoomToFit()">Fit All</button>
  </div>
  <label id="hide-done-wrap"><input type="checkbox" id="hide-done" checked onchange="onHideDoneChange()"> Hide done stories</label>
  <div id="search-wrap">
    <input id="search-input" type="search" placeholder="Search stories, for example PROJ-123" oninput="onSearchInput()" onkeydown="if(event.key==='Escape'){{this.value='';onSearchInput();}}">
    <span id="search-count"></span>
  </div>
</div>
<div id="legend">{legend_html}</div>
<div id="gantt_here"></div>
<script src="https://cdn.dhtmlx.com/gantt/9.0/dhtmlxgantt.js"></script>
<script>
gantt.plugins({{ tooltip: true, marker: true }});
gantt.config.date_format = "%Y-%m-%d";
gantt.config.readonly = true;
gantt.config.grid_resize = true;
gantt.config.columns = [
  {{name: "text", label: "Story", width: 400, tree: true, resize: true,
    template: function(task) {{ return "<span title='" + task.text.replace(/'/g, "&#39;") + "'>" + task.text + "</span>"; }} }},
  {{name: "status", label: "Status", align: "left", width: 120, resize: true}},
  {{name: "effort", label: "Remaining", align: "center", width: 80, resize: true}}
];
gantt.templates.tooltip_text = function(start, end, task) {{
  if (!task.text) return "";
  return "<b>" + task.text + "</b><br/>" +
    gantt.date.date_to_str("%d %M %Y")(start) + " – " +
    gantt.date.date_to_str("%d %M %Y")(end);
}};
gantt.config.open_tree_initial_level = 1;
gantt.config.row_height = 32;
gantt.config.bar_height = 24;
gantt.config.fit_tasks = true;
gantt.config.show_progress = true;
gantt.ext.zoom.init({{
  levels: [
    {{
      name: "day",
      scale_height: 27,
      min_column_width: 80,
      scales: [{{ unit: "day", step: 1, format: "%d %M" }}]
    }},
    {{
      name: "week",
      scale_height: 50,
      min_column_width: 50,
      scales: [
        {{ unit: "week", step: 1, format: "Week %W" }},
        {{ unit: "day", step: 1, format: "%j %D" }}
      ]
    }},
    {{
      name: "month",
      scale_height: 50,
      min_column_width: 120,
      scales: [
        {{ unit: "month", step: 1, format: "%F %Y" }},
        {{ unit: "week", step: 1, format: "W%W" }}
      ]
    }},
    {{
      name: "quarter",
      scale_height: 50,
      min_column_width: 90,
      scales: [
        {{ unit: "year", step: 1, format: "%Y" }},
        {{ unit: "month", step: 1, format: "%M" }}
      ]
    }}
  ]
}});
gantt.ext.zoom.setLevel("week"); // initial level before fit
function setZoom(level) {{
  gantt.ext.zoom.setLevel(level);
  document.querySelectorAll("#toolbar button").forEach(function(b) {{
    b.classList.toggle("active", b.textContent.toLowerCase() === level);
  }});
}}
var hideDone = false;
var epicDates = {{}};  // {{epicId: {{start_date, end_date}}}} with the dates from all stories
function isTaskShown(task) {{
  if (!hideDone) return true;
  if (task.type === "project") return !!openStoryDates(task.id);
  return task.progress < 1;
}}
// Earliest start and latest end of the stories of an epic that are not done, or null.
function openStoryDates(epicId) {{
  var start = null, end = null;
  gantt.eachTask(function(child) {{
    if (child.progress >= 1) return;
    if (!start || child.start_date < start) start = child.start_date;
    if (!end || child.end_date > end) end = child.end_date;
  }}, epicId);
  return start ? {{ start_date: start, end_date: end }} : null;
}}
gantt.attachEvent("onBeforeTaskDisplay", function(id, task) {{
  return isTaskShown(task);
}});
function onHideDoneChange() {{
  hideDone = document.getElementById("hide-done").checked;
  gantt.eachTask(function(task) {{
    if (task.parent !== gantt.config.root_id) return;
    if (!epicDates[task.id]) epicDates[task.id] = {{ start_date: task.start_date, end_date: task.end_date }};
    var dates = hideDone ? openStoryDates(task.id) : null;
    // A "project" task takes its dates from all its children, so use a plain task while done stories are hidden.
    task.type = dates ? "task" : "project";
    task.start_date = (dates || epicDates[task.id]).start_date;
    task.end_date = (dates || epicDates[task.id]).end_date;
  }});
  zoomToFit();
  gantt.render();
}}
function shownDates() {{
  var start = null, end = null;
  gantt.eachTask(function(task) {{
    if (!isTaskShown(task)) return;
    if (!start || task.start_date < start) start = task.start_date;
    if (!end || task.end_date > end) end = task.end_date;
  }});
  return {{ start_date: start, end_date: end }};
}}
function zoomToFit() {{
  var project = shownDates();
  if (!project.start_date || !project.end_date) return;
  gantt.config.start_date = hideDone ? project.start_date : undefined;
  gantt.config.end_date = hideDone ? gantt.date.add(project.end_date, 1, "day") : undefined;
  var areaWidth = gantt.$task.offsetWidth;
  var levels = ["day", "week", "month", "quarter"];
  // Pick the most detailed level that fits, trying "day" first
  var best = "quarter";
  for (var i = 0; i < levels.length; i++) {{
    gantt.ext.zoom.setLevel(levels[i]);
    var cols = gantt.getScale().count;
    var minWidth = gantt.config.min_column_width || 50;
    if (cols * minWidth <= areaWidth) {{
      best = levels[i];
      break;
    }}
  }}
  setZoom(best);
}}
var searchTerm = "";
var searchMatchIds = {{}};      // {{taskId: true}} for tasks whose own text matches
var searchVisibleIds = {{}};    // {{taskId: true}} for matches + their ancestors (kept un-dimmed)
gantt.templates.task_class = function(start, end, task) {{
  var base = task.progress >= 1 ? "clickable-task task-done" : "clickable-task";
  if (!searchTerm) return base;
  if (searchMatchIds[task.id]) return base + " search-match";
  if (searchVisibleIds[task.id]) return base;
  return base + " search-dim";
}};
gantt.templates.grid_row_class = function(start, end, task) {{
  var base = task.progress >= 1 ? "task-done-row" : "";
  if (!searchTerm) return base;
  if (searchMatchIds[task.id]) return (base + " search-match-row").trim();
  if (searchVisibleIds[task.id]) return base;
  return (base + " search-dim-row").trim();
}};
function onSearchInput() {{
  var raw = document.getElementById("search-input").value.trim().toLowerCase();
  searchTerm = raw;
  searchMatchIds = {{}};
  searchVisibleIds = {{}};
  var countEl = document.getElementById("search-count");
  if (!raw) {{
    countEl.textContent = "";
    gantt.refreshData();
    return;
  }}
  var firstMatchId = null;
  var matchCount = 0;
  gantt.eachTask(function(task) {{
    var hay = ((task.text || "") + " " + (task.story_key || "")).toLowerCase();
    if (hay.indexOf(raw) !== -1) {{
      searchMatchIds[task.id] = true;
      searchVisibleIds[task.id] = true;
      matchCount++;
      if (firstMatchId === null) firstMatchId = task.id;
      var p = gantt.getParent(task.id);
      while (p && p !== gantt.config.root_id) {{
        searchVisibleIds[p] = true;
        gantt.open(p);
        p = gantt.getParent(p);
      }}
    }}
  }});
  countEl.textContent = matchCount === 1 ? "1 match" : matchCount + " matches";
  gantt.refreshData();
  if (firstMatchId !== null) gantt.showTask(firstMatchId);
}}
gantt.templates.task_text = function(start, end, task) {{
  return task.text + " " + Math.round(task.progress * 100) + "%";
}};
gantt.templates.rightside_text = function(start, end, task) {{
  var parts = [];
  if (task.assignee) parts.push(task.assignee);
  parts.push(gantt.date.date_to_str("%d %M")(end));
  return "<span style='font-size:11px;color:#666;'>" + parts.join(" \u2022 ") + "</span>";
}};
gantt.templates.timeline_cell_class = function(task, date) {{
  var scale = gantt.getState().scale_unit;
  if (scale === "day" && (date.getDay() === 0 || date.getDay() === 6)) return "weekend";
  return "";
}};
gantt.init("gantt_here");
gantt.addMarker({{ start_date: new Date(), css: "today-marker", text: "Today" }});
(function() {{
  var ruler = document.createElement("div");
  ruler.id = "mouse-ruler";
  var taskArea = gantt.$task;
  taskArea.style.position = "relative";
  taskArea.appendChild(ruler);
  taskArea.addEventListener("mousemove", function(e) {{
    var rect = taskArea.getBoundingClientRect();
    ruler.style.left = (e.clientX - rect.left) + "px";
    ruler.style.display = "block";
  }});
  taskArea.addEventListener("mouseleave", function() {{
    ruler.style.display = "none";
  }});
}})();
gantt.parse({{ data: {tasks_json}, links: {links_json} }});
onHideDoneChange();
document.getElementById("gantt_here").addEventListener("click", function(e) {{
  var bar = e.target.closest(".gantt_task_line");
  if (!bar) return;
  var id = bar.getAttribute("task_id");
  if (!id) return;
  try {{
    var task = gantt.getTask(id);
    if (task.story_key) {{
      window.open("{JIRA_URL}/browse/" + task.story_key, "_blank");
    }}
  }} catch(ex) {{}}
}});
</script>
</body>
</html>""")


def discover_fields():
    """Fetch one story with all fields and print numeric fields to help find story points."""
    validate_config()
    jql = f"project = {JIRA_PROJECT} AND issuetype = Story ORDER BY key ASC"
    data = jira_get("search/jql", params={"jql": jql, "fields": "*all", "maxResults": 1})
    if not data["issues"]:
        print("No stories found in project.", file=sys.stderr)
        sys.exit(1)

    issue = data["issues"][0]
    print(f"Fields for {issue['key']} — {issue['fields']['summary']}\n", file=sys.stderr)
    print(f"{'Field':<35} {'Value'}", file=sys.stderr)
    print(f"{'-'*35} {'-'*45}", file=sys.stderr)

    for key, val in sorted(issue["fields"].items()):
        if isinstance(val, (int, float)):
            print(f"{key:<35} {val}", file=sys.stderr)
    print(file=sys.stderr)
    print("Set JIRA_STORY_POINTS_FIELD in .env to the field name that contains your story points.", file=sys.stderr)


def run_gantt(config_path):
    """Fetch the configured stories, schedule them and print a Gantt chart as HTML."""
    config = load_gantt_config(config_path)

    # Expand epics into stories
    ordered_keys = []
    story_data = {}
    for epic_key in config.epics:
        print(f"Fetching stories for epic {epic_key}...", file=sys.stderr)
        epic_issues = fetch_stories_for_epic(epic_key)
        if not epic_issues:
            print(f"Warning: epic {epic_key} has 0 stories, skipping", file=sys.stderr)
            continue
        print(f"  Found {len(epic_issues)} stories in {epic_key}", file=sys.stderr)
        for issue in epic_issues:
            key = issue["key"]
            if key not in story_data:
                ordered_keys.append(key)
                story_data[key] = issue

    # Fetch individual stories
    if config.stories:
        print(f"Fetching {len(config.stories)} individual stories...", file=sys.stderr)
        individual = fetch_stories_by_keys(config.stories)
        for key in config.stories:
            if key in individual and key not in story_data:
                ordered_keys.append(key)
                story_data[key] = individual[key]

    # Fetch stories in open and future sprints
    if config.include_open_sprints or config.include_future_sprints:
        print("Fetching sprint stories...", file=sys.stderr)
        added = 0
        for issue in fetch_sprint_stories(config.include_open_sprints, config.include_future_sprints):
            key = issue["key"]
            if key not in story_data:
                ordered_keys.append(key)
                story_data[key] = issue
                added += 1
        print(f"  Added {added} stories from sprints", file=sys.stderr)

    # Fetch any dependency targets not already in the scheduled set
    dep_keys_needed = set()
    for deps in config.dependencies.values():
        for dep_key in deps:
            if dep_key not in story_data:
                dep_keys_needed.add(dep_key)
    if dep_keys_needed:
        print(f"Fetching {len(dep_keys_needed)} dependency stories...", file=sys.stderr)
        dep_issues = fetch_stories_by_keys(list(dep_keys_needed))
        for key in dep_keys_needed:
            if key not in dep_issues or key in story_data:
                continue
            issue = dep_issues[key]
            status = issue["fields"].get("status", {}).get("name", "")
            progress = config.statuses.get(status.lower(), 0)
            if progress >= 1.0:
                print(f"  Dependency {key} is done ({status}), skipping", file=sys.stderr)
                continue
            ordered_keys.append(key)
            story_data[key] = issue

    # Resolve epics and attach to issue dicts
    epic_cache = {}
    for issue in story_data.values():
        issue["_epic"] = resolve_epic_for_issue(issue, epic_cache)
        parent = issue["fields"].get("parent")
        issue["_epic_key"] = (parent.get("key", "") if parent
                              else issue["fields"].get("customfield_10014", ""))

    scheduled = schedule_stories(config, story_data, ordered_keys)
    print(f"Scheduled {len(scheduled)} stories.", file=sys.stderr)
    write_gantt_html(scheduled, config.statuses, config.dependencies)


def run_export(fmt):
    """Fetch all stories of the project and print them as CSV or as an HTML table."""
    print(f"Fetching stories from {JIRA_PROJECT}...", file=sys.stderr)
    issues = fetch_stories()
    print(f"Found {len(issues)} stories.", file=sys.stderr)

    # Build rows and resolve epics
    epic_cache = {}
    rows = []
    for issue in issues:
        key = issue["key"]
        status = issue["fields"].get("status", {}).get("name", "")

        # Filter out excluded statuses
        if EXCLUDE_STATUSES and status.lower() in EXCLUDE_STATUSES:
            continue

        epic_name = resolve_epic_for_issue(issue, epic_cache)

        rows.append({
            "Epic": epic_name,
            "Story ID": key,
            "Title": issue["fields"]["summary"],
            "Status": status,
            "Sprint": get_sprint(issue["fields"]),
            "Points": get_story_points(issue["fields"]),
            "Assignee": get_assignee(issue["fields"]),
        })

    columns = ["Epic", "Sprint", "Story ID", "Title", "Points", "Status", "Assignee"]

    if fmt == "html":
        # Sort by sprint first (empty sprint last), then epic, then story key
        rows.sort(key=lambda r: (r["Sprint"] == "", r["Sprint"], r["Epic"], r["Story ID"]))
        write_html(rows, columns)
    else:
        # CSV: sort by epic, then story key
        rows.sort(key=lambda r: (r["Epic"], r["Story ID"]))
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows, columns):
    """Write rows as an HTML table with Story ID linked to Jira."""
    print("<html><head>")
    print("<style>")
    print("  table { border-collapse: collapse; font-family: sans-serif; }")
    print("  th, td { border: 1px solid #ccc; padding: 6px 12px; text-align: left; }")
    print("  th { background: #f4f4f4; }")
    print("  tr:hover { background: #f9f9f9; }")
    print("  a { color: #0052cc; text-decoration: none; }")
    print("  a:hover { text-decoration: underline; }")
    print("</style>")
    print("</head><body>")
    print("<table>")
    print("<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in columns) + "</tr>")
    for row in rows:
        bg = ""
        for col, val, color in HIGHLIGHT_RULES:
            if str(row.get(col, "")).lower() == val:
                bg = f' style="background: {html.escape(color)};"'
                break
        print(f"<tr{bg}>", end="")
        for col in columns:
            val = html.escape(str(row[col]))
            if col == "Story ID":
                url = f"{JIRA_URL}/browse/{row[col]}"
                print(f'<td><a href="{html.escape(url)}">{val}</a></td>', end="")
            else:
                print(f"<td>{val}</td>", end="")
        print("</tr>")
    print("</table>")
    print("</body></html>")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jira_smurf.py",
        description="Export Jira Cloud stories as CSV or HTML, or plan them in a Gantt chart.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    csv_parser = subparsers.add_parser("csv", help="export all stories of the project as CSV")
    csv_parser.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")

    html_parser = subparsers.add_parser("html", help="export all stories of the project as an HTML table")
    html_parser.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")

    gantt_parser = subparsers.add_parser("gantt", help="schedule the configured stories and write a Gantt chart")
    gantt_parser.add_argument("-c", "--config", metavar="FILE", default="gantt.toml",
                              help="planning configuration (default: gantt.toml)")
    gantt_parser.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")

    subparsers.add_parser("fields", help="list the numeric fields of one story, to find the story points field")
    return parser


def main():
    args = build_parser().parse_args()
    validate_config()

    if args.command == "fields":
        discover_fields()
        return

    output = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
    try:
        with contextlib.redirect_stdout(output):
            if args.command == "gantt":
                run_gantt(args.config)
            else:
                run_export(args.command)
    finally:
        if args.output:
            output.close()
    if args.output:
        print(f"Wrote {args.output}", file=sys.stderr)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jira_smurf.py",
        description="Export Jira Cloud stories as CSV or HTML, or plan them in a Gantt chart.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    csv_parser = subparsers.add_parser("csv", help="export all stories of the project as CSV")
    csv_parser.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")

    html_parser = subparsers.add_parser("html", help="export all stories of the project as an HTML table")
    html_parser.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")

    gantt_parser = subparsers.add_parser("gantt", help="schedule the configured stories and write a Gantt chart")
    gantt_parser.add_argument("-c", "--config", metavar="FILE", default="gantt.toml",
                              help="planning configuration (default: gantt.toml)")
    gantt_parser.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")

    subparsers.add_parser("fields", help="list the numeric fields of one story, to find the story points field")
    return parser


def main():
    args = build_parser().parse_args()
    validate_config()

    if args.command == "fields":
        discover_fields()
        return

    output = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
    try:
        with contextlib.redirect_stdout(output):
            if args.command == "gantt":
                run_gantt(args.config)
            else:
                run_export(args.command)
    finally:
        if args.output:
            output.close()
    if args.output:
        print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
