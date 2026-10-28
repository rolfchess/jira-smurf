#!/usr/bin/env python3
"""Export Jira Cloud stories as CSV or HTML, or plan them in a Gantt chart."""

import argparse
import base64
import contextlib
import csv
import glob
import heapq
import html
import json
import os
import re
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
    team: dict  # {name: {mon: N, tue: N, ..., days_off: {date, ...}}}
    stories: list  # [key, ...] — ordered list of story keys
    epics: list  # [key, ...] — ordered list of epic keys
    statuses: dict  # {lowercase_name: progress_0_to_1}
    default_hours: float
    velocity: float
    dependencies: dict  # {story_key: [dep_key, ...]}
    include_open_sprints: bool
    include_future_sprints: bool
    holidays: set  # {date, ...} — days off for the whole team
    status_colors: dict  # {lowercase_name: css_color}
    work_start_progress: float  # progress of the status where work on a story starts, 0 if not set
    use_backlog_order: bool  # schedule stories in Jira backlog order instead of the order of the configuration file


def parse_dates(setting, values, path):
    """Return a list of dates as a set of dates.

    Each entry is a TOML date, a "YYYY-MM-DD" string, or a "YYYY-MM-DD to YYYY-MM-DD"
    range that includes both ends.
    """
    if not isinstance(values, list):
        print(f"Error: {setting} in {path} must be a list of dates", file=sys.stderr)
        sys.exit(1)
    result = set()
    for day in values:
        if isinstance(day, date):
            result.add(day)
            continue
        try:
            first, _, last = day.partition(" to ")
            first = date.fromisoformat(first.strip())
            last = date.fromisoformat(last.strip()) if last else first
        except (AttributeError, ValueError):
            print(f"Error: '{day}' in {setting} in {path} must be YYYY-MM-DD "
                  f"or YYYY-MM-DD to YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
        if last < first:
            print(f"Error: '{day}' in {setting} in {path} ends before it starts", file=sys.stderr)
            sys.exit(1)
        result.update(first + timedelta(days=i) for i in range((last - first).days + 1))
    return result


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

    # Holidays are days off for the whole team
    holidays = parse_dates("'holidays'", raw.get("holidays", []), path)

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
        days_off = parse_dates(f"'days_off' for team member '{name}'", avail.get("days_off", []), path)
        avail["days_off"] = days_off | holidays
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

    # This is the optional status where work on a story starts, used for the dates of done stories
    work_starts_at = raw.get("work_starts_at")
    work_start_progress = 0.0
    if work_starts_at is not None:
        if not isinstance(work_starts_at, str) or work_starts_at.lower() not in statuses:
            print(f"Error: 'work_starts_at' must be a status from [statuses], got {work_starts_at!r}", file=sys.stderr)
            sys.exit(1)
        work_start_progress = statuses[work_starts_at.lower()]

    # Parse optional [status_colors] section — maps status name to a row background color
    status_colors_raw = raw.get("status_colors", {})
    if not isinstance(status_colors_raw, dict):
        print(f"Error: [status_colors] in {path} must be a table", file=sys.stderr)
        sys.exit(1)
    for name, color in status_colors_raw.items():
        if not isinstance(color, str) or not re.fullmatch(r"[#\w(),.%\s]+", color):
            print(f"Error: color for status '{name}' must be a CSS color such as \"#ffcccc\", got {color!r}",
                  file=sys.stderr)
            sys.exit(1)
    status_colors = {name.lower(): color for name, color in status_colors_raw.items()}

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
    use_backlog_order = raw.get("use_backlog_order", False)
    for name, val in (("include_open_sprints", include_open_sprints),
                      ("include_future_sprints", include_future_sprints),
                      ("use_backlog_order", use_backlog_order)):
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
                       include_future_sprints=include_future_sprints,
                       holidays=holidays,
                       status_colors=status_colors,
                       work_start_progress=work_start_progress,
                       use_backlog_order=use_backlog_order)


def fetch_stories_for_epic(epic_key):
    """Fetch all stories belonging to an epic.

    Tries next-gen (parent) first, falls back to classic (Epic Link).
    Returns list of issue dicts.
    """
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10028,duedate,issuetype"

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


def fetch_stories_by_keys(story_keys, warn_missing=True):
    """Fetch specific stories by their Jira keys.

    With warn_missing, print a warning for each key that Jira does not return.
    """
    keys_str = ", ".join(story_keys)
    jql = f"key in ({keys_str})"
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10028,duedate,issuetype"
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
    for key in story_keys if warn_missing else ():
        if key not in by_key:
            print(f"Warning: story {key} not found in Jira, skipping", file=sys.stderr)

    return by_key


def fetch_backlog_order(story_keys):
    """Return {story key: position in the Jira backlog} for the given stories, based on the Rank field."""
    jql = f"key in ({', '.join(story_keys)}) ORDER BY Rank ASC"
    positions = {}
    next_page_token = None
    while True:
        params = {"jql": jql, "fields": "summary", "maxResults": PAGE_SIZE}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        data = jira_get("search/jql", params=params)
        for issue in data["issues"]:
            positions[issue["key"]] = len(positions)
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return positions


def fetch_sprint_stories(include_open, include_future):
    """Fetch the project's stories in open sprints, future sprints or both."""
    sprint_functions = []
    if include_open:
        sprint_functions.append("openSprints()")
    if include_future:
        sprint_functions.append("futureSprints()")
    sprint_clause = " OR ".join(f"sprint in {f}" for f in sprint_functions)
    jql = f"project = {JIRA_PROJECT} AND issuetype = Story AND ({sprint_clause}) ORDER BY key ASC"
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10028,duedate,issuetype"
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


def fetch_status_changes(issue_key):
    """Return the status changes of a story as a list of (date, new status name), oldest first."""
    changes = []
    start_at = 0
    while True:
        data = jira_get(f"issue/{issue_key}/changelog", params={"startAt": start_at, "maxResults": 100})
        values = data.get("values", [])
        for history in values:
            day = date.fromisoformat(history["created"][:10])
            for item in history.get("items", []):
                if item.get("field") == "status":
                    changes.append((day, item.get("toString") or ""))
        start_at += len(values)
        if data.get("isLast", True) or not values:
            break
    changes.sort(key=lambda change: change[0])
    return changes


def work_dates(status_changes, statuses, work_start_progress=0.0):
    """Return (first day of work, day it was done) from a story's status changes, or None.

    The first day of work is the first change to a status with progress above 0 and at least
    work_start_progress. The day it was done is the last change to a status with progress 100.
    The result is None if there is no such change.
    """
    start = end = None
    for day, status in status_changes:
        progress = statuses.get(status.lower(), 0)
        if progress > 0 and progress >= work_start_progress and start is None:
            start = day
        if progress >= 1.0:
            end = day
    if end is None:
        return None
    if start is None or start > end:
        start = end
    return start, end


def find_epic_icon_url(issues):
    """Return the address of the Epic issue type icon, or "" if Jira has none."""
    # In team-managed projects the parent of a story is its epic, with its issue type
    for issue in issues:
        parent_type = ((issue["fields"].get("parent") or {}).get("fields") or {}).get("issuetype") or {}
        if parent_type.get("name", "").lower() == "epic" and parent_type.get("iconUrl"):
            return parent_type["iconUrl"]
    try:
        issue_types = jira_get("issuetype")
    except requests.RequestException:
        return ""
    for issue_type in issue_types:
        if issue_type.get("name", "").lower() == "epic" and issue_type.get("iconUrl"):
            return issue_type["iconUrl"]
    return ""


def fetch_icon_images(urls):
    """Download icon images from Jira and return {address: data URI}.

    An icon that cannot be downloaded is left out, with a warning.
    """
    images = {}
    for url in sorted(set(urls)):
        try:
            resp = requests.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Warning: could not download icon {url}: {e}", file=sys.stderr)
            continue
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            print(f"Warning: icon {url} is not an image ({content_type or 'no type'})", file=sys.stderr)
            continue
        images[url] = f"data:{content_type};base64,{base64.b64encode(resp.content).decode('ascii')}"
    return images


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


def hours_on(d, availability):
    """Return the hours this member works on date d. A day off has 0 hours."""
    if d in availability.get("days_off", ()):
        return 0
    return availability.get(DAY_KEYS[d.weekday()], 0)


def next_working_day(d, availability):
    """Advance to the next day where this member has hours > 0."""
    while hours_on(d, availability) <= 0:
        d += timedelta(days=1)
    return d


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


def schedule_stories(config, story_data, ordered_keys, done_dates=None):
    """Schedule stories onto team members based on availability.

    ordered_keys is the merged, deduplicated list of story keys (epics first, then individual).
    done_dates is {story_key: (first day, last day)} for done stories with known dates. Those
    stories are placed on these days. Other done stories are placed at the start of the plan.
    Returns list of dicts with keys:
        key, title, epic, assignee, status, start, end
    """
    done_dates = done_dates or {}
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

        if progress >= 1.0 and story_key in done_dates:
            # A done story is shown on the days it was worked on, from its Jira status history
            start_date, end_date = done_dates[story_key]
            cursor = end_date + timedelta(days=1)
        else:
            # Apply dependency constraints: story cannot start before all dependencies finish
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
                    remaining -= hours_on(cursor, avail)
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
            "due": issue["fields"].get("duedate") or "",
            "issue_type": (issue["fields"].get("issuetype") or {}).get("name", ""),
            "issue_type_icon_url": (issue["fields"].get("issuetype") or {}).get("iconUrl", ""),
            "effort": 0 if progress >= 1.0 else hours,
            "total_hours": total_hours,
            "estimated": sp is not None,
            "start": start_date.isoformat(),
            "end": (end_date + timedelta(days=1)).isoformat(),
        })

    return scheduled


def plan_snapshot(scheduled, status_map, run_date):
    """Return the plan of one run as a dict that can be stored as JSON.

    Story "end" is the day after the last planned day, as in the scheduled stories.
    """
    stories = []
    for s in scheduled:
        progress = status_map.get(s["status"].lower(), 0)
        stories.append({
            "key": s["key"],
            "title": s["title"],
            "epic": s["epic"],
            "epic_key": s.get("epic_key", ""),
            "issue_type": s.get("issue_type", ""),
            "issue_type_icon_url": s.get("issue_type_icon_url", ""),
            "status": s["status"],
            "progress": progress,
            "total_hours": s["total_hours"],
            "remaining_hours": s["effort"],
            "start": s["start"],
            "end": s["end"],
        })
    return {"date": run_date.isoformat(), "stories": stories}


def load_snapshots(history_dir):
    """Return all plan-YYYY-MM-DD.json snapshots in history_dir, oldest first."""
    snapshots = []
    for path in sorted(glob.glob(os.path.join(history_dir, "plan-*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                snapshot = json.load(f)
            date.fromisoformat(snapshot["date"])
        except (OSError, ValueError, KeyError) as e:
            print(f"Warning: skipping snapshot {path}: {e}", file=sys.stderr)
            continue
        snapshots.append(snapshot)
    snapshots.sort(key=lambda s: s["date"])
    return snapshots


def pick_baseline(snapshots, run_date, since=None):
    """Return the snapshot to compare with, or None. A snapshot of run_date is never used.

    Without since, this is the latest snapshot. With since, it is the latest snapshot on or
    before that date, or the oldest snapshot if there is none before it.
    """
    earlier = [s for s in snapshots if s["date"] < run_date.isoformat()]
    if not earlier:
        return None
    if since is None:
        return earlier[-1]
    on_or_before = [s for s in earlier if s["date"] <= since.isoformat()]
    baseline = on_or_before[-1] if on_or_before else earlier[0]
    if baseline["date"] != since.isoformat():
        print(f"Warning: there is no earlier snapshot of {since} to compare with, "
              f"using the one of {baseline['date']}", file=sys.stderr)
    return baseline


def working_days_between(start, end, holidays):
    """Return the number of weekdays that are not holidays from start up to end.

    The result is negative when end is before start.
    """
    sign = 1
    if end < start:
        start, end, sign = end, start, -1
    days = 0
    d = start
    while d < end:
        if d.weekday() < 5 and d not in holidays:
            days += 1
        d += timedelta(days=1)
    return sign * days


def epic_spans(stories):
    """Return {epic name: (first start, last end)} for a list of snapshot stories."""
    spans = {}
    for s in stories:
        start, end = spans.get(s["epic"], (s["start"], s["end"]))
        spans[s["epic"]] = (min(start, s["start"]), max(end, s["end"]))
    return spans


def compare_plans(baseline, current, holidays, today):
    """Compare two snapshots and return the changes per story and per epic, and totals.

    Returns a dict with:
        since: date of the baseline
        stories: {key: {is_new, base_start, base_end, base_total, hours_delta, moved_days, late}}
        epics: {epic name: {base_start, base_end, moved_days}}
        removed: [baseline story, ...] for open stories that are no longer in the plan
        summary: totals for the summary box
    """
    base_by_key = {s["key"]: s for s in baseline["stories"]}
    current_keys = {s["key"] for s in current["stories"]}
    today_str = today.isoformat()

    stories = {}
    summary = {
        "new": [], "new_hours": 0.0,
        "grew": [], "grew_hours": 0.0,
        "shrank": [], "shrank_hours": 0.0,
        "late": [], "late_hours": 0.0,
    }
    for s in current["stories"]:
        done = s["progress"] >= 1.0
        base = base_by_key.get(s["key"])
        if base is None:
            stories[s["key"]] = {"is_new": True}
            summary["new"].append(s)
            summary["new_hours"] += s["remaining_hours"]
            continue
        change = {"is_new": False}
        if not done:
            change["base_start"] = base["start"]
            change["base_end"] = base["end"]
            change["moved_days"] = working_days_between(
                date.fromisoformat(base["end"]), date.fromisoformat(s["end"]), holidays)
            if base.get("total_hours") is not None:
                change["base_total"] = base["total_hours"]
                delta = s["total_hours"] - base["total_hours"]
                if abs(delta) >= 0.5:
                    change["hours_delta"] = delta
                    if delta > 0:
                        summary["grew"].append(s)
                        summary["grew_hours"] += delta
                    else:
                        summary["shrank"].append(s)
                        summary["shrank_hours"] += delta
            if base["progress"] < 1.0 and base["end"] <= today_str:
                change["late"] = True
                summary["late"].append(s)
                summary["late_hours"] += s["remaining_hours"]
        stories[s["key"]] = change

    removed = [s for s in baseline["stories"] if s["key"] not in current_keys and s["progress"] < 1.0]
    summary["removed"] = removed
    summary["removed_hours"] = sum(s["remaining_hours"] for s in removed)

    epics = {}
    base_spans = epic_spans(baseline["stories"])
    for epic, (start, end) in epic_spans(current["stories"]).items():
        if epic not in base_spans:
            continue
        base_start, base_end = base_spans[epic]
        epics[epic] = {
            "base_start": base_start,
            "base_end": base_end,
            "moved_days": working_days_between(date.fromisoformat(base_end), date.fromisoformat(end), holidays),
        }

    base_end = max((s["end"] for s in baseline["stories"]), default=None)
    end = max((s["end"] for s in current["stories"]), default=None)
    summary["base_end"] = base_end
    summary["end"] = end
    summary["moved_days"] = (working_days_between(date.fromisoformat(base_end), date.fromisoformat(end), holidays)
                             if base_end and end else 0)
    summary["base_remaining"] = sum(s["remaining_hours"] for s in baseline["stories"])
    summary["remaining"] = sum(s["remaining_hours"] for s in current["stories"])

    return {"since": baseline["date"], "stories": stories, "epics": epics, "removed": removed, "summary": summary}


def format_last_day(iso_end):
    """Return the last planned day for an end date that is the day after it, as YYYY-MM-DD."""
    return (date.fromisoformat(iso_end) - timedelta(days=1)).isoformat()


def format_hours(hours):
    """Return hours as "8h" or "7.5h"."""
    return f"{hours:.0f}h" if hours == int(hours) else f"{hours:.1f}h"


def format_signed(value, unit):
    """Return a number with its sign and unit, for example "+8h" or "−3 working days"."""
    sign = "+" if value > 0 else "−" if value < 0 else ""
    number = format_hours(abs(value))[:-1] if unit == "h" else f"{abs(value):g}"
    return f"{sign}{number}{unit}"


def changes_summary_html(changes):
    """Return the HTML of the box that explains how the plan changed since the baseline."""
    s = changes["summary"]

    def story_list(stories):
        items = "".join(
            f"<li><a href=\"{html.escape(JIRA_URL)}/browse/{html.escape(st['key'])}\" target=\"_blank\">"
            f"{html.escape(st['key'])}</a>: {html.escape(st['title'])}</li>"
            for st in stories
        )
        return f"<details><summary>Show stories</summary><ul>{items}</ul></details>" if stories else ""

    def row(label, stories, hours):
        count = len(stories)
        noun = "story" if count == 1 else "stories"
        return (f"<li><b>{count} {label.format(noun=noun)}</b>: {format_signed(hours, 'h')}"
                f"{story_list(stories)}</li>")

    if s["end"] and s["base_end"]:
        moved = s["moved_days"]
        days = format_signed(moved, " working days") if moved else "no change"
        end_line = (f"<p>The plan end moved from <b>{format_last_day(s['base_end'])}</b> "
                    f"to <b>{format_last_day(s['end'])}</b> ({days}).</p>")
    else:
        end_line = ""
    return f"""<div id="changes-summary">
<h3>Changes since {changes['since']}</h3>
{end_line}
<p>Remaining work went from {format_hours(s['base_remaining'])} to {format_hours(s['remaining'])}.</p>
<ul>
{row("new {noun}", s["new"], s["new_hours"])}
{row("{noun} with a larger estimate", s["grew"], s["grew_hours"])}
{row("{noun} with a smaller estimate", s["shrank"], s["shrank_hours"])}
{row("{noun} no longer in the plan", s["removed"], -s["removed_hours"])}
<li><b>{len(s["late"])} {"story" if len(s["late"]) == 1 else "stories"} planned to be done by now but still open</b>:
{format_hours(s["late_hours"])} remaining{story_list(s["late"])}</li>
</ul>
</div>"""


def trend_html(snapshots, epic_color_map, holidays, compare_date=None):
    """Return the HTML of a milestone trend chart: the planned end of each epic at each run.

    The table below the chart compares the latest run with the run of compare_date, or with
    the oldest run if compare_date is None.

    The x axis is the run date and the y axis the planned end date. A flat line means the
    end date did not move. Only epics in epic_color_map are drawn, in their epic color.
    """
    if len(snapshots) < 2:
        return ""
    width, height = 900, 420
    left, right, top, bottom = 90, 20, 20, 50
    plot_w, plot_h = width - left - right, height - top - bottom

    # {epic: [(run date, last planned day), ...]} and the same for the whole plan
    series = {epic: [] for epic in epic_color_map}
    whole_plan = []
    for snap in snapshots:
        run = date.fromisoformat(snap["date"])
        for epic, (_, end) in epic_spans(snap["stories"]).items():
            if epic in series:
                series[epic].append((run, date.fromisoformat(end) - timedelta(days=1)))
        if snap["stories"]:
            last = max(s["end"] for s in snap["stories"])
            whole_plan.append((run, date.fromisoformat(last) - timedelta(days=1)))
    series = {epic: points for epic, points in series.items() if points}

    runs = [date.fromisoformat(s["date"]) for s in snapshots]
    all_ends = [d for points in series.values() for _, d in points] + [d for _, d in whole_plan]
    x_min, x_max = runs[0], runs[-1]
    y_min, y_max = min(all_ends + [x_min]), max(all_ends + [x_max])
    x_span = max((x_max - x_min).days, 1)
    y_span = max((y_max - y_min).days, 1)

    def x(d):
        return left + (d - x_min).days / x_span * plot_w

    def y(d):
        return top + plot_h - (d - y_min).days / y_span * plot_h

    parts = [f'<svg id="trend-svg" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Planned end date of each epic at each run">']

    # Horizontal grid lines and y labels at the first day of each month
    d = date(y_min.year, y_min.month, 1)
    while d <= y_max:
        if d >= y_min:
            parts.append(f'<line class="grid" x1="{left}" x2="{left + plot_w}" y1="{y(d):.1f}" y2="{y(d):.1f}"/>')
            parts.append(f'<text class="axis" x="{left - 8}" y="{y(d) + 4:.1f}" text-anchor="end">'
                         f'{d.strftime("%Y-%m")}</text>')
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)

    # X labels at the first day of each month
    d = date(x_min.year, x_min.month, 1)
    while d <= x_max:
        if d >= x_min:
            parts.append(f'<line class="grid" x1="{x(d):.1f}" x2="{x(d):.1f}" y1="{top}" y2="{top + plot_h}"/>')
            parts.append(f'<text class="axis" x="{x(d):.1f}" y="{top + plot_h + 18}" text-anchor="middle">'
                         f'{d.strftime("%Y-%m")}</text>')
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    parts.append(f'<text class="axis" x="{left + plot_w / 2:.0f}" y="{height - 6}" text-anchor="middle">Run date</text>')

    # Diagonal where the planned end equals the run date: an epic that reaches it is done
    lo, hi = max(x_min, y_min), min(x_max, y_max)
    if lo < hi:
        parts.append(f'<line class="diagonal" x1="{x(lo):.1f}" y1="{y(lo):.1f}" x2="{x(hi):.1f}" y2="{y(hi):.1f}"/>')

    def draw(name, points, color, css_class):
        coords = " ".join(f"{x(r):.1f},{y(e):.1f}" for r, e in points)
        label = html.escape(name)
        parts.append(f'<g class="trend-series {css_class}" data-epic="{label}">')
        parts.append(f'<polyline points="{coords}" stroke="{color}"><title>{label}</title></polyline>')
        for r, e in points:
            parts.append(f'<circle cx="{x(r):.1f}" cy="{y(e):.1f}" r="4" fill="{color}">'
                         f'<title>{label}\nRun {r}: planned end {e}</title></circle>')
        parts.append('</g>')

    for epic, points in series.items():
        draw(epic, points, epic_color_map[epic], "")
    draw("Whole plan", whole_plan, "#333", "whole-plan")
    parts.append('</svg>')

    legend = "".join(
        f'<span class="trend-legend-item" data-epic="{html.escape(epic)}">'
        f'<span class="swatch" style="background:{color}"></span>{html.escape(epic)}</span>'
        for epic, color in list(epic_color_map.items()) + [("Whole plan", "#333")]
        if epic in series or epic == "Whole plan"
    )

    compare_run = date.fromisoformat(compare_date) if compare_date else runs[0]
    rows = []
    for name, points in list(series.items()) + [("Whole plan", whole_plan)]:
        then, last = dict(points).get(compare_run), points[-1][1]
        if then is None:
            then_cell = moved_cell = "–"
        else:
            moved = working_days_between(then + timedelta(days=1), last + timedelta(days=1), holidays)
            then_cell = then.isoformat()
            moved_cell = format_signed(moved, " working days") if moved else "no change"
        rows.append(f"<tr><td>{html.escape(name)}</td><td>{then_cell}</td>"
                    f"<td>{last}</td><td>{moved_cell}</td></tr>")
    table = ("<details><summary>Show as table</summary><table><tr><th>Epic</th>"
             f"<th>Planned end {compare_run}</th>"
             "<th>Latest planned end</th><th>Moved</th></tr>" + "".join(rows) + "</table></details>")

    return f"""<div id="trend">
<h3>Planned end date per run</h3>
<p class="trend-help">Each line is an epic. A flat line means its planned end did not move.
A rising line means it moved later. The gray diagonal shows where the planned end equals the run date.</p>
<div id="trend-legend">{legend}</div>
{"".join(parts)}
{table}
</div>"""


# Icons shown before the title in the Story column, by lowercase Jira issue type
ISSUE_TYPE_ICONS = {
    "epic": "📖",
    "story": "🔖",
    "bug": "🐛",
    "task": "🔧",
    "sub-task": "🔩",
    "subtask": "🔩",
    "spike": "🔬",
    "improvement": "📈",
    "new feature": "✨",
    "feature": "✨",
    "initiative": "🎯",
    "test": "🧪",
    "incident": "🚨",
    "support request": "🛟",
    "technical debt": "🧹",
}
OTHER_ISSUE_TYPE_ICON = "📄"


def issue_type_icon(issue_type):
    """Return the icon for a Jira issue type name, or "" if the name is empty."""
    if not issue_type:
        return ""
    return ISSUE_TYPE_ICONS.get(issue_type.lower(), OTHER_ISSUE_TYPE_ICON)


EPIC_COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]


def write_gantt_html(scheduled_stories, status_map, dependencies=None, holidays=(), status_colors=None,
                     changes=None, snapshots=(), icon_images=None, epic_icon_url=""):
    """Write a self-contained HTML file with a DHTMLX Gantt chart.

    With changes (from compare_plans), the chart can mark what changed since the baseline.
    With two or more snapshots, a trend chart of the planned end dates is added below it.
    icon_images is {icon address: data URI} for the issue type icons from Jira. A type
    without an image there is shown with an emoji.
    """
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
        # The total is partly a guess when an open story in the epic has no estimate
        epic_unestimated = any(not st.get("estimated", True) and status_map.get(st["status"].lower(), 0) < 1.0
                               for st in epic_stories)
        if epic_unestimated:
            effort_str += "?"

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
            "icon": ISSUE_TYPE_ICONS["epic"],
            "unestimated": epic_unestimated,
            "icon_url": epic_icon_url,
            "story_key": epic_name_to_key.get(epic_name, ""),
            "progress": epic_progress,
            **(changes["epics"].get(epic_name, {}) if changes else {}),
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
        unestimated = not s.get("estimated", True) and progress < 1.0
        if unestimated:
            # The hours come from default_hours, because the story has no estimate in Jira
            effort_str += "?"

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
            "due_date": s.get("due", ""),
            "icon": issue_type_icon(s.get("issue_type", "")),
            "icon_url": s.get("issue_type_icon_url", ""),
            "unestimated": unestimated,
            **(changes["stories"].get(s["key"], {}) if changes else {}),
        })

    # Open stories of the baseline that are no longer in the plan, in one group at the end
    if changes and changes["removed"]:
        removed = changes["removed"]
        task_id += 1
        group_id = task_id
        tasks.append({
            "id": group_id,
            "text": "No longer in the plan",
            "start_date": min(r["start"] for r in removed),
            "end_date": max(r["end"] for r in removed),
            "effort": format_hours(sum(r["remaining_hours"] for r in removed)),
            "status": "",
            "color": "#bbb",
            "type": "project",
            "open": True,
            "progress": 0,
            "removed": True,
        })
        for r in removed:
            task_id += 1
            tasks.append({
                "id": task_id,
                "text": f"{r['key']}: {r['title']} ({r['epic']})",
                "icon": issue_type_icon(r.get("issue_type", "")),
                "icon_url": r.get("issue_type_icon_url", ""),
                "start_date": r["start"],
                "end_date": r["end"],
                "progress": 0,
                "effort": format_hours(r["remaining_hours"]),
                "status": r["status"],
                "color": "#bbb",
                "parent": group_id,
                "story_key": r["key"],
                "removed": True,
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
    holidays_json = json.dumps(sorted(d.isoformat() for d in holidays))
    show_changes_json = "true" if changes else "false"
    icon_images_json = json.dumps(icon_images or {})
    if changes:
        since = changes["since"]
        changes_toggle = (f'<label id="show-changes-wrap"><input type="checkbox" id="show-changes" checked '
                          f'onchange="onShowChangesChange()"> Show changes since {since}</label>')
        moved_column = (',\n  {name: "moved", label: "Moved", align: "center", width: 70, resize: true,'
                        ' template: movedText}')
    else:
        changes_toggle = ""
        moved_column = ""
    summary_html = changes_summary_html(changes) if changes else ""
    trend = trend_html(list(snapshots), epic_color_map, set(holidays), changes["since"] if changes else None)

    # Each status color has one CSS class, used on the grid row and the timeline row
    status_colors = status_colors or {}
    status_classes = {name: f"status-color-{i}" for i, name in enumerate(status_colors)}
    status_classes_json = json.dumps(status_classes)
    status_css = "\n".join(
        f"  .gantt_row.{cls}, .gantt_task_row.{cls} {{ background: {status_colors[name]}; }}"
        for name, cls in status_classes.items()
    )

    print(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Gantt Chart — {date.today()}</title>
<link rel="stylesheet" href="https://cdn.dhtmlx.com/gantt/9.0/dhtmlxgantt.css">
<style>
  body {{ font-family: sans-serif; margin: 20px; }}
  #gantt_here {{ width: 100%; height: calc(100vh - 110px); }}
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
  #show-done-wrap {{ display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }}
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
  #show-changes-wrap {{ display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }}
  .gantt_row.change-new-row, .gantt_task_row.change-new-row {{ background: #fff3bf; }}
  .gantt_row.change-removed-row {{ color: #999; text-decoration: line-through; }}
  .gantt_task_line.change-removed {{ opacity: 0.5; }}
  .gantt_task_line.change-late {{ box-shadow: 0 0 0 2px #d62728; }}
  .delta-up {{ color: #c62828; font-weight: 600; }}
  .delta-down {{ color: #2e7d32; font-weight: 600; }}
  .due-marker {{ position: absolute; width: 10px; height: 10px; background: #d62728; border: 1px solid #fff;
    transform: rotate(45deg); pointer-events: none; z-index: 2; }}
  .baseline-bar {{ position: absolute; height: 3px; background: #888; border-radius: 2px; pointer-events: none; }}
  #changes-summary, #trend {{ margin-top: 20px; padding: 10px 16px; background: #f8f8f8; border-radius: 6px; }}
  #changes-summary h3, #trend h3 {{ margin: 4px 0 8px; }}
  #changes-summary ul {{ margin: 6px 0; }}
  #changes-summary details, #trend details {{ margin: 2px 0 6px; font-size: 13px; }}
  #trend-svg {{ width: 100%; max-width: 900px; height: auto; display: block; background: #fff; }}
  #trend-svg .grid {{ stroke: #e6e6e6; stroke-width: 1; }}
  #trend-svg .axis {{ font-size: 11px; fill: #666; }}
  #trend-svg .diagonal {{ stroke: #bbb; stroke-width: 1; stroke-dasharray: 4 4; }}
  #trend-svg polyline {{ fill: none; stroke-width: 2; }}
  #trend-svg .whole-plan polyline {{ stroke-width: 3; }}
  #trend-svg circle {{ stroke: #fff; stroke-width: 2; }}
  #trend-svg .trend-series {{ transition: opacity 0.15s; }}
  #trend-svg.highlighting .trend-series {{ opacity: 0.15; }}
  #trend-svg.highlighting .trend-series.active {{ opacity: 1; }}
  #trend-legend {{ margin: 8px 0; font-size: 13px; }}
  .trend-legend-item {{ display: inline-flex; align-items: center; margin-right: 16px; cursor: default; }}
  .trend-legend-item .swatch {{ display: inline-block; width: 14px; height: 3px; margin-right: 6px; }}
  .trend-help {{ font-size: 13px; color: #555; margin: 0; }}
  #trend table {{ border-collapse: collapse; margin-top: 6px; }}
  #trend th, #trend td {{ border: 1px solid #ddd; padding: 4px 10px; text-align: left; }}
  .type-icon {{ margin-right: 5px; }}
  img.type-icon {{ width: 16px; height: 16px; vertical-align: -3px; }}
  .tree-buttons {{ margin-left: 12px; }}
  .tree-buttons button {{
    padding: 1px 8px; margin-right: 4px; border: 1px solid #ccc; border-radius: 4px;
    background: #fff; cursor: pointer; font-size: 11px;
  }}
  .tree-buttons button:hover {{ background: #e8e8e8; }}
{status_css}
</style>
</head>
<body>
<h2>Gantt Chart — {date.today()}</h2>
<div id="toolbar">
  <div class="zoom-buttons">
    <button onclick="setZoom('day')">Day</button>
    <button onclick="setZoom('week')">Week</button>
    <button onclick="setZoom('month')">Month</button>
    <button onclick="setZoom('quarter')">Quarter</button>
    <button onclick="zoomToFit()">Fit All</button>
  </div>
  <label id="show-done-wrap"><input type="checkbox" id="show-done" onchange="onShowDoneChange()"> Show done stories</label>
  {changes_toggle}
  <div id="search-wrap">
    <input id="search-input" type="search" placeholder="Search stories, for example PROJ-123" oninput="onSearchInput()" onkeydown="if(event.key==='Escape'){{this.value='';onSearchInput();}}">
    <span id="search-count"></span>
  </div>
</div>
<div id="gantt_here"></div>
{summary_html}
{trend}
<script src="https://cdn.dhtmlx.com/gantt/9.0/dhtmlxgantt.js"></script>
<script>
gantt.plugins({{ tooltip: true, marker: true }});
var typeIconImages = {icon_images_json};  // {{icon address: data URI}} of the issue type icons from Jira
var showChanges = {show_changes_json};
function signed(value, unit) {{
  var number = Math.round(Math.abs(value) * 10) / 10;
  return (value > 0 ? "+" : "\u2212") + number + unit;
}}
// Remaining hours, followed by the change of the estimate since the baseline.
function effortText(task) {{
  if (!showChanges || !task.hours_delta) return task.effort;
  var cls = task.hours_delta > 0 ? "delta-up" : "delta-down";
  return task.effort + " <span class='" + cls + "'>" + signed(task.hours_delta, "h") + "</span>";
}}
// "NEW" for a story that is not in the baseline, or the working days its end date moved.
function movedText(task) {{
  if (!showChanges) return "";
  if (task.is_new) return "<span class='delta-up'>NEW</span>";
  if (!task.moved_days) return "";
  var cls = task.moved_days > 0 ? "delta-up" : "delta-down";
  return "<span class='" + cls + "'>" + signed(task.moved_days, "d") + "</span>";
}}
function onShowChangesChange() {{
  showChanges = document.getElementById("show-changes").checked;
  gantt.render();
}}
gantt.config.date_format = "%Y-%m-%d";
gantt.config.readonly = true;
gantt.config.grid_resize = true;
gantt.config.columns = [
  {{name: "text", width: 400, tree: true, resize: true,
    label: "Story <span class='tree-buttons'>" +
      "<button onclick='setAllOpen(false); event.stopPropagation();'>Collapse all</button>" +
      "<button onclick='setAllOpen(true); event.stopPropagation();'>Expand all</button></span>",
    template: function(task) {{
      var image = typeIconImages[task.icon_url];
      var icon = image ? "<img class='type-icon' src='" + image + "' alt=''>" :
        task.icon ? "<span class='type-icon'>" + task.icon + "</span>" : "";
      return icon + "<span title='" + task.text.replace(/'/g, "&#39;") + "'>" + task.text + "</span>";
    }} }},
  {{name: "status", label: "Status", align: "left", width: 120, resize: true}},
  {{name: "effort", label: "Remaining", align: "center", width: 100, resize: true, template: effortText}}{moved_column}
];
gantt.templates.tooltip_text = function(start, end, task) {{
  if (!task.text) return "";
  var fmt = gantt.date.date_to_str("%d %M %Y");
  var text = "<b>" + task.text + "</b><br/>" + fmt(start) + " – " + fmt(gantt.date.add(end, -1, "day"));
  if (task.unestimated) {{
    text += task.type === "project" || gantt.hasChild(task.id)
      ? "<br/>Some stories have no estimate in Jira, so the default hours are used for them"
      : "<br/>No estimate in Jira, so the default hours are used";
  }}
  if (task.due_date) {{
    var due = gantt.date.str_to_date("%Y-%m-%d")(task.due_date);
    text += "<br/>Due: " + fmt(due) + (end > gantt.date.add(due, 1, "day") ? " (planned to end later)" : "");
  }}
  if (!showChanges) return text;
  if (task.is_new) text += "<br/>New since the baseline";
  if (task.removed && task.parent) text += "<br/>No longer in the plan";
  if (task.base_start) {{
    var parse = gantt.date.str_to_date("%Y-%m-%d");
    var baseEnd = gantt.date.add(parse(task.base_end), -1, "day");
    text += "<br/>Baseline: " + fmt(parse(task.base_start)) + " – " + fmt(baseEnd);
  }}
  if (task.hours_delta) text += "<br/>Estimate: " + task.base_total + "h → " + (task.base_total + task.hours_delta) + "h";
  if (task.late) text += "<br/>Planned to be done by now";
  return text;
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
// Open or close every epic, so the chart shows all stories or only the epics.
function setAllOpen(open) {{
  gantt.batchUpdate(function() {{
    gantt.eachTask(function(task) {{
      if (!gantt.hasChild(task.id)) return;
      if (open) gantt.open(task.id);
      else gantt.close(task.id);
    }});
  }});
}}
function setZoom(level) {{
  gantt.ext.zoom.setLevel(level);
  document.querySelectorAll("#toolbar button").forEach(function(b) {{
    b.classList.toggle("active", b.textContent.toLowerCase() === level);
  }});
}}
var hideDone = false;
var epicDates = {{}};  // {{epicId: {{start_date, end_date}}}} with the dates from all stories
function isTaskShown(task) {{
  if (task.removed) return showChanges;
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
function onShowDoneChange() {{
  hideDone = !document.getElementById("show-done").checked;
  gantt.eachTask(function(task) {{
    if (task.parent !== gantt.config.root_id || task.removed) return;
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
    // Stories that left the plan have old dates, so they do not count for the shown range
    if (!isTaskShown(task) || task.removed) return;
    if (!start || task.start_date < start) start = task.start_date;
    if (!end || task.end_date > end) end = task.end_date;
  }});
  return {{ start_date: start, end_date: end }};
}}
function zoomToFit() {{
  var project = shownDates();
  if (!project.start_date || !project.end_date) return;
  gantt.config.start_date = project.start_date;
  gantt.config.end_date = gantt.date.add(project.end_date, 1, "day");
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
  // A long range does not fit even at the widest level, so make its columns narrower
  var count = gantt.getScale().count;
  if (count * gantt.config.min_column_width > areaWidth) {{
    gantt.config.min_column_width = Math.max(20, Math.floor(areaWidth / count));
    gantt.render();
  }}
}}
var searchTerm = "";
var searchMatchIds = {{}};      // {{taskId: true}} for tasks whose own text matches
var searchVisibleIds = {{}};    // {{taskId: true}} for matches + their ancestors (kept un-dimmed)
function changeBarClass(task) {{
  if (!showChanges) return "";
  if (task.removed) return " change-removed";
  return task.late ? " change-late" : "";
}}
function changeRowClass(task) {{
  if (!showChanges) return "";
  if (task.removed && task.parent) return "change-removed-row";
  return task.is_new ? "change-new-row" : "";
}}
function joinClasses() {{
  return Array.prototype.filter.call(arguments, Boolean).join(" ");
}}
gantt.templates.task_class = function(start, end, task) {{
  var base = (task.progress >= 1 ? "clickable-task task-done" : "clickable-task") + changeBarClass(task);
  if (!searchTerm) return base;
  if (searchMatchIds[task.id]) return base + " search-match";
  if (searchVisibleIds[task.id]) return base;
  return base + " search-dim";
}};
var statusClasses = {status_classes_json};  // {{lowercase status: CSS class}} for the row background
function statusRowClass(task) {{
  return statusClasses[(task.status || "").toLowerCase()] || "";
}}
gantt.templates.task_row_class = function(start, end, task) {{
  return joinClasses(statusRowClass(task), changeRowClass(task));
}};
gantt.templates.grid_row_class = function(start, end, task) {{
  var base = joinClasses(task.progress >= 1 ? "task-done-row" : "", statusRowClass(task), changeRowClass(task));
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
  if (task.removed) return task.text;
  return task.text + " " + Math.round(task.progress * 100) + "%";
}};
gantt.templates.rightside_text = function(start, end, task) {{
  var parts = [];
  if (task.assignee) parts.push(task.assignee);
  parts.push(gantt.date.date_to_str("%d %M")(end));
  return "<span style='font-size:11px;color:#666;'>" + parts.join(" \u2022 ") + "</span>";
}};
var holidays = new Set({holidays_json});
var dayKey = gantt.date.date_to_str("%Y-%m-%d");
gantt.templates.timeline_cell_class = function(task, date) {{
  var scale = gantt.getState().scale_unit;
  if (scale !== "day") return "";
  if (date.getDay() === 0 || date.getDay() === 6 || holidays.has(dayKey(date))) return "weekend";
  return "";
}};
// Thin gray bars at the baseline dates, under the real bars.
function drawBaselineBars() {{
  var area = gantt.$task_data;
  if (!area) return;
  var layer = document.getElementById("baseline-layer");
  if (!layer || layer.parentNode !== area) {{
    layer = document.createElement("div");
    layer.id = "baseline-layer";
    area.appendChild(layer);
  }}
  layer.innerHTML = "";
  if (!showChanges) return;
  var parse = gantt.date.str_to_date("%Y-%m-%d");
  gantt.eachTask(function(task) {{
    if (!task.base_start || !gantt.isTaskVisible(task.id)) return;
    var pos = gantt.getTaskPosition(task, parse(task.base_start), parse(task.base_end));
    var bar = document.createElement("div");
    bar.className = "baseline-bar";
    bar.style.left = pos.left + "px";
    bar.style.width = Math.max(pos.width, 2) + "px";
    // pos.top is the top of the row and the bar is centered in it
    bar.style.top = Math.round(pos.top + (pos.rowHeight + pos.height) / 2) + 1 + "px";
    layer.appendChild(bar);
  }});
}}
gantt.attachEvent("onDataRender", drawBaselineBars);
// A red diamond at the end of the due day of each story with a due date in Jira.
function drawDueMarkers() {{
  var area = gantt.$task_data;
  if (!area) return;
  var layer = document.getElementById("due-layer");
  if (!layer || layer.parentNode !== area) {{
    layer = document.createElement("div");
    layer.id = "due-layer";
    area.appendChild(layer);
  }}
  layer.innerHTML = "";
  var parse = gantt.date.str_to_date("%Y-%m-%d");
  gantt.eachTask(function(task) {{
    if (!task.due_date || !gantt.isTaskVisible(task.id)) return;
    var dayAfter = gantt.date.add(parse(task.due_date), 1, "day");
    var pos = gantt.getTaskPosition(task, dayAfter, dayAfter);
    var marker = document.createElement("div");
    marker.className = "due-marker";
    // The marker is 12px wide with its border, so move it back by half of that
    marker.style.left = Math.round(pos.left - 6) + "px";
    marker.style.top = Math.round(pos.top + pos.rowHeight / 2 - 6) + "px";
    layer.appendChild(marker);
  }});
}}
gantt.attachEvent("onDataRender", drawDueMarkers);
// Make the chart as tall as its shown rows, but no taller than the window.
function fitHeight() {{
  var content = gantt.config.scale_height + gantt.getVisibleTaskCount() * gantt.config.row_height +
    (gantt.config.scroll_size || 20) + 2;
  var height = Math.max(150, Math.min(content, window.innerHeight - 110)) + "px";
  var container = document.getElementById("gantt_here");
  if (container.style.height === height) return;
  container.style.height = height;
  gantt.setSizes();
}}
gantt.attachEvent("onDataRender", fitHeight);
window.addEventListener("resize", fitHeight);
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
onShowDoneChange();
(function() {{
  var svg = document.getElementById("trend-svg");
  if (!svg) return;
  function highlight(epic) {{
    svg.classList.toggle("highlighting", !!epic);
    svg.querySelectorAll(".trend-series").forEach(function(g) {{
      g.classList.toggle("active", g.getAttribute("data-epic") === epic);
    }});
  }}
  document.querySelectorAll(".trend-legend-item, #trend-svg .trend-series").forEach(function(el) {{
    el.addEventListener("mouseenter", function() {{ highlight(el.getAttribute("data-epic")); }});
    el.addEventListener("mouseleave", function() {{ highlight(null); }});
  }});
}})();
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


def run_gantt(config_path, history_dir=None, since=None):
    """Fetch the configured stories, schedule them and print a Gantt chart as HTML.

    With history_dir, the plan is also stored there as plan-YYYY-MM-DD.json, and the chart
    shows the trend over all snapshots. With since, the chart also shows the changes since an
    earlier snapshot: since is a date, or "last" for the latest snapshot before the current day.
    """
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

    # Place done stories on the days they were worked on, from their status history
    done_keys = [key for key in ordered_keys
                 if config.statuses.get(story_data[key]["fields"].get("status", {}).get("name", "").lower(), 0) >= 1.0]
    done_dates = {}
    if done_keys:
        print(f"Fetching the status history of {len(done_keys)} done stories...", file=sys.stderr)
        for key in done_keys:
            dates = work_dates(fetch_status_changes(key), config.statuses, config.work_start_progress)
            if dates:
                done_dates[key] = dates
        missing = len(done_keys) - len(done_dates)
        if missing:
            print(f"  {missing} done {'story has' if missing == 1 else 'stories have'} no change to a done status "
                  f"in the history, so the plan start is used", file=sys.stderr)

    # The planner takes stories in this order; the chart rows keep the order of the configuration file
    schedule_keys = ordered_keys
    if config.use_backlog_order:
        print("Fetching the backlog order...", file=sys.stderr)
        positions = fetch_backlog_order(ordered_keys)
        schedule_keys = sorted(ordered_keys, key=lambda key: positions.get(key, len(positions)))

    scheduled = schedule_stories(config, story_data, schedule_keys, done_dates)
    config_position = {key: i for i, key in enumerate(ordered_keys)}
    scheduled.sort(key=lambda s: config_position[s["key"]])
    print(f"Scheduled {len(scheduled)} stories.", file=sys.stderr)
    changes = None
    snapshots = []
    if history_dir:
        today = date.today()
        current = plan_snapshot(scheduled, config.statuses, today)
        snapshots = [snap for snap in load_snapshots(history_dir) if snap["date"] != current["date"]]
        snapshots.append(current)
        if since:
            baseline = pick_baseline(snapshots, today, None if since == "last" else since)
            if baseline:
                changes = compare_plans(baseline, current, config.holidays, today)
                print(f"Comparing with the snapshot of {baseline['date']}.", file=sys.stderr)
            else:
                print(f"No earlier snapshot in {history_dir}, so there are no changes to show.", file=sys.stderr)
        os.makedirs(history_dir, exist_ok=True)
        snapshot_path = os.path.join(history_dir, f"plan-{current['date']}.json")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=1)
        print(f"Wrote {snapshot_path}", file=sys.stderr)

    # Stories that left the plan come from an older snapshot, which may not have their issue type
    if changes:
        untyped = [r for r in changes["removed"] if not r.get("issue_type_icon_url")]
        if untyped:
            found = fetch_stories_by_keys([r["key"] for r in untyped], warn_missing=False)
            for r in untyped:
                if r["key"] not in found:
                    print(f"Warning: story {r['key']} is no longer in Jira, so it has no type icon", file=sys.stderr)
                issue_type = (found.get(r["key"], {}).get("fields", {}).get("issuetype")) or {}
                r["issue_type"] = issue_type.get("name", "")
                r["issue_type_icon_url"] = issue_type.get("iconUrl", "")

    # Put the Jira issue type icons into the page, so it shows them without a Jira login
    epic_icon_url = find_epic_icon_url(story_data.values())
    icon_urls = [s["issue_type_icon_url"] for s in scheduled if s["issue_type_icon_url"]]
    if changes:
        icon_urls += [r["issue_type_icon_url"] for r in changes["removed"] if r.get("issue_type_icon_url")]
    if epic_icon_url:
        icon_urls.append(epic_icon_url)
    icon_images = {}
    if icon_urls:
        print(f"Downloading {len(set(icon_urls))} issue type icons...", file=sys.stderr)
        icon_images = fetch_icon_images(icon_urls)

    write_gantt_html(scheduled, config.statuses, config.dependencies, config.holidays, config.status_colors,
                     changes, snapshots, icon_images, epic_icon_url)


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


def parse_since(value):
    """Return the --since value as a date, or "last" when it is "last"."""
    if value == "last":
        return value
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a date in the form YYYY-MM-DD")


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
    gantt_parser.add_argument("--history", metavar="DIR",
                              help="store a snapshot of the plan in DIR, and show the changes and the trend "
                                   "from the snapshots there")
    gantt_parser.add_argument("--since", metavar="YYYY-MM-DD", nargs="?", const="last", type=parse_since,
                              help="show the changes since the latest snapshot on or before this date; "
                                   "without a date, since the latest snapshot before the current day")

    subparsers.add_parser("fields", help="list the numeric fields of one story, to find the story points field")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "since", None) and not args.history:
        parser.error("--since needs --history")
    validate_config()

    if args.command == "fields":
        discover_fields()
        return

    output = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
    try:
        with contextlib.redirect_stdout(output):
            if args.command == "gantt":
                run_gantt(args.config, args.history, args.since)
            else:
                run_export(args.command)
    finally:
        if args.output:
            output.close()
    if args.output:
        print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
