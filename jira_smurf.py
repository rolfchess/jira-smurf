#!/usr/bin/env python3
"""Export Jira Cloud stories as CSV or HTML, or plan them in a Gantt chart."""

import argparse
import base64
import contextlib
import csv
import glob
import heapq
import html
import http.server
import io
import json
import os
import re
import sys
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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


def jira_post(path, body):
    """Make an authenticated POST request with a JSON body to the Jira REST API."""
    url = f"{JIRA_URL}/rest/api/3/{path}"
    resp = requests.post(url, json=body, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_stories():
    """Fetch all stories for the project, paginating through results."""
    jql = f"project = {JIRA_PROJECT} AND issuetype = Story ORDER BY key ASC"
    fields = f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,customfield_10016"
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


# Name of the group of stories that do not belong to an epic
NO_EPIC = "(No Epic)"


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

    return NO_EPIC


def get_story_points(fields):
    """Extract story points, trying the configured field and common defaults."""
    for field_name in (STORY_POINTS_FIELD, "story_points", "customfield_10016"):
        val = fields.get(field_name)
        if val is not None:
            return str(val).replace(".", ",")
    return ""


def get_sprint(fields):
    """Return the name of the active sprint of a story, or else of its latest sprint, or ""."""
    sprints = fields.get(SPRINT_FIELD)
    if not sprints or not isinstance(sprints, list):
        return ""
    # Jira does not keep this list in sprint order, so pick by state, then by date
    state_order = {"active": 2, "future": 1}
    latest = max(sprints, key=lambda s: (state_order.get(s.get("state"), 0),
                                         s.get("endDate") or s.get("startDate") or "", s.get("id", 0)))
    return latest.get("name", "")


def in_active_sprint(fields):
    """Return True when the story is in a sprint that is running now."""
    sprints = fields.get(SPRINT_FIELD)
    return isinstance(sprints, list) and any(s.get("state") == "active" for s in sprints)


def get_assignee(fields):
    assignee = fields.get("assignee")
    if assignee:
        return assignee.get("displayName", "")
    return "Unassigned"


def get_story_points_numeric(fields):
    """Extract story points as a float, or None if not set."""
    for field_name in (STORY_POINTS_FIELD, "story_points", "customfield_10016"):
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
    group_by_space: bool  # with use_backlog_order, put the epics under a header row per Jira space
    dependency_links: list  # lowercase names of Jira link types that make a story wait for another
    auto_assign: bool  # give stories without a team member to the team; if False, plan them under "Unassigned"
    implementation_status: str  # status where open stories show their days of work in a badge, "" if not set
    exclude: list  # [key or project key, ...] — uppercase; stories that match are left out of the chart
    badges: list  # [{field, label, color, stripe}, ...] — badges on epic rows where a Jira field is set
    visuals: dict  # {past_due_blink, past_due_color, behind_baseline_blink, behind_baseline_color, search_color}


def is_excluded(key, exclude):
    """Return True if the issue key matches an entry of exclude.

    An entry with a number, such as "ACME-123", matches that one key. An entry without one,
    such as "ACME", matches every key of that project.
    """
    key = key.upper()
    project = key.rsplit("-", 1)[0]
    return any(key == entry or project == entry for entry in exclude)


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


def parse_periods(name, values, path):
    """Return the in_team periods of a team member as a list of (first day, last day), or None.

    Each entry is "YYYY-MM-DD to YYYY-MM-DD". Either side may be empty, which leaves the period
    open on that side. None means the member has no in_team setting.
    """
    if values is None:
        return None
    if not isinstance(values, list) or not values:
        print(f"Error: 'in_team' for team member '{name}' in {path} must be a list of periods", file=sys.stderr)
        sys.exit(1)
    periods = []
    for value in values:
        parts = value.split("to") if isinstance(value, str) else []
        try:
            if len(parts) != 2:
                raise ValueError
            first, last = (date.fromisoformat(part.strip()) if part.strip() else None for part in parts)
            if (first is None and last is None) or (first and last and last < first):
                raise ValueError
        except ValueError:
            print(f"Error: '{value}' in 'in_team' for team member '{name}' must be "
                  f"\"YYYY-MM-DD to YYYY-MM-DD\", \"YYYY-MM-DD to\" or \"to YYYY-MM-DD\"", file=sys.stderr)
            sys.exit(1)
        periods.append((first, last))
    return periods


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

    # Without auto-assign, stories without a team member are not given to the team
    auto_assign_team = raw.get("auto_assign", True)
    if not isinstance(auto_assign_team, bool):
        print(f"Error: 'auto_assign' must be true or false, got {auto_assign_team!r}", file=sys.stderr)
        sys.exit(1)

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
        avail["own_days_off"] = days_off  # without the holidays, for the marks in the date header
        avail["days_off"] = days_off | holidays
        avail["in_team"] = parse_periods(name, avail.get("in_team"), path)
        if auto_assign and not has_working_hours(avail):
            print(f"Error: team member '{name}' has no working hours configured. "
                  f"Add hours or set auto_assign = false", file=sys.stderr)
            sys.exit(1)
    if auto_assign_team and not any(avail.get("auto_assign", True) for avail in team.values()):
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

    # This is the optional status where open stories show their days of work in a badge
    implementation_status = raw.get("implementation_status")
    if implementation_status is not None:
        if not isinstance(implementation_status, str) or implementation_status.lower() not in statuses:
            print(f"Error: 'implementation_status' must be a status from [statuses], got {implementation_status!r}",
                  file=sys.stderr)
            sys.exit(1)

    # Parse optional [status_colors] section — maps status name to a row background color
    status_colors_raw = raw.get("status_colors", {})
    if not isinstance(status_colors_raw, dict):
        print(f"Error: [status_colors] in {path} must be a table", file=sys.stderr)
        sys.exit(1)
    for name, color in status_colors_raw.items():
        if not is_css_color(color):
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
    group_by_space = raw.get("group_by_space", True)
    dependency_links = raw.get("dependency_links", ["Blocks"])
    if not isinstance(dependency_links, list) or not all(isinstance(name, str) for name in dependency_links):
        print(f"Error: 'dependency_links' must be a list of Jira link type names, got {dependency_links!r}",
              file=sys.stderr)
        sys.exit(1)
    for name, val in (("include_open_sprints", include_open_sprints),
                      ("include_future_sprints", include_future_sprints),
                      ("use_backlog_order", use_backlog_order),
                      ("group_by_space", group_by_space)):
        if not isinstance(val, bool):
            print(f"Error: '{name}' must be true or false, got {val!r}", file=sys.stderr)
            sys.exit(1)

    # Stories or whole projects to leave out of the chart, such as issues of another team
    exclude = raw.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(entry, str) and entry.strip() for entry in exclude):
        print(f"Error: 'exclude' must be a list of issue keys or project keys, got {exclude!r}", file=sys.stderr)
        sys.exit(1)

    badges = parse_badges(raw.get("badges", []), path)
    visuals = parse_visuals(raw.get("visuals", {}), path)

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
                       use_backlog_order=use_backlog_order,
                       group_by_space=group_by_space,
                       dependency_links=[name.lower() for name in dependency_links],
                       auto_assign=auto_assign_team,
                       implementation_status=implementation_status or "",
                       exclude=[entry.strip().upper() for entry in exclude],
                       badges=badges,
                       visuals=visuals)


DEFAULT_BADGE_COLOR = "#d9a520"  # amber, so a badge does not look like the orange "over the estimate" badge
# Without 'values', a badge ignores these field values, so a Yes/No select set to "No"
# or a number field set to 0 gives no badge
NO_BADGE_VALUES = {"no", "false", "0"}


def is_css_color(value):
    """Return True if value looks like a CSS color, such as "#ffcccc" or "rgb(1, 2, 3)"."""
    return isinstance(value, str) and re.fullmatch(r"[#\w(),.%\s]+", value) is not None


def parse_badges(badges_raw, path):
    """Return the [[badges]] of the configuration as [{field, label, color, stripe, values}, ...].

    Each badge marks the epic rows where a Jira field, such as a "Paid Feature" checkbox, has a value.
    values are the lowercase field values that give the badge, or [] for any value but "No", "False" or 0.
    """
    if not isinstance(badges_raw, list) or not all(isinstance(b, dict) for b in badges_raw):
        print(f"Error: badges in {path} must be a list of [[badges]] tables", file=sys.stderr)
        sys.exit(1)
    badges = []
    for i, badge in enumerate(badges_raw, 1):
        unknown = set(badge) - {"field", "label", "color", "stripe", "values"}
        if unknown:
            print(f"Error: [[badges]] number {i} in {path} has unknown settings: {', '.join(sorted(unknown))}",
                  file=sys.stderr)
            sys.exit(1)
        field = badge.get("field")
        if not isinstance(field, str) or not re.fullmatch(r"\w+", field.strip()):
            print(f"Error: [[badges]] number {i} in {path} needs a field ID such as \"customfield_10410\", "
                  f"got {field!r}", file=sys.stderr)
            sys.exit(1)
        label = badge.get("label", "")
        if not isinstance(label, str):
            print(f"Error: 'label' of badge {field} must be text, got {label!r}", file=sys.stderr)
            sys.exit(1)
        color = badge.get("color", DEFAULT_BADGE_COLOR)
        if not is_css_color(color):
            print(f"Error: 'color' of badge {field} must be a CSS color such as \"#d9a520\", got {color!r}",
                  file=sys.stderr)
            sys.exit(1)
        stripe = badge.get("stripe", False)
        if not isinstance(stripe, bool):
            print(f"Error: 'stripe' of badge {field} must be true or false, got {stripe!r}", file=sys.stderr)
            sys.exit(1)
        values = badge.get("values", [])
        if "values" in badge and values == []:
            # Not "any value", which is what leaving 'values' out means
            print(f"Error: 'values' of badge {field} is empty, so no value could give the badge. "
                  f"List the values that give it, or leave 'values' out for any value", file=sys.stderr)
            sys.exit(1)
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            print(f"Error: 'values' of badge {field} must be a list of texts such as [\"Yes\"], got {values!r}",
                  file=sys.stderr)
            sys.exit(1)
        badges.append({"field": field.strip(), "label": label.strip(), "color": color, "stripe": stripe,
                       "values": [v.strip().lower() for v in values]})
    return badges


DEFAULT_VISUALS = {"past_due_blink": True, "past_due_color": "#d62728",
                   "behind_baseline_blink": False, "behind_baseline_color": "#f2c200", "search_color": "#9c27b0",
                   "days_off_bar_from": 5, "holiday_icon": "flag", "enable_vacation_banners": True}
HOLIDAY_ICONS = ("flag", "star", "calendar", "dot", "text", "none")


def parse_visuals(visuals_raw, path):
    """Return the [visuals] of the configuration as {past_due_blink, past_due_color, behind_baseline_blink,
    behind_baseline_color, search_color, days_off_bar_from, holiday_icon, enable_vacation_banners}.

    Each setting is optional and has the value of DEFAULT_VISUALS when it is left out.
    """
    if not isinstance(visuals_raw, dict):
        print(f"Error: [visuals] in {path} must be a table", file=sys.stderr)
        sys.exit(1)
    unknown = set(visuals_raw) - set(DEFAULT_VISUALS)
    if unknown:
        print(f"Error: [visuals] in {path} has unknown settings: {', '.join(sorted(unknown))}", file=sys.stderr)
        sys.exit(1)
    visuals = dict(DEFAULT_VISUALS, **visuals_raw)
    for name in ("past_due_blink", "behind_baseline_blink", "enable_vacation_banners"):
        if not isinstance(visuals[name], bool):
            print(f"Error: '{name}' in [visuals] must be true or false, got {visuals[name]!r}", file=sys.stderr)
            sys.exit(1)
    for name in ("past_due_color", "behind_baseline_color", "search_color"):
        if not is_css_color(visuals[name]):
            print(f"Error: '{name}' in [visuals] must be a CSS color such as \"{DEFAULT_VISUALS[name]}\", "
                  f"got {visuals[name]!r}", file=sys.stderr)
            sys.exit(1)
    bar_from = visuals["days_off_bar_from"]
    if isinstance(bar_from, bool) or not isinstance(bar_from, int) or bar_from < 1:
        print(f"Error: 'days_off_bar_from' in [visuals] must be a whole number of 1 or more, got {bar_from!r}",
              file=sys.stderr)
        sys.exit(1)
    if visuals["holiday_icon"] not in HOLIDAY_ICONS:
        print(f"Error: 'holiday_icon' in [visuals] must be one of {', '.join(map(repr, HOLIDAY_ICONS))}, "
              f"got {visuals['holiday_icon']!r}", file=sys.stderr)
        sys.exit(1)
    return visuals


def story_fields(start_date_field=None):
    """Return the Jira fields to fetch for a story to plan, with the Start date field when there is one."""
    fields = (f"summary,status,assignee,parent,{STORY_POINTS_FIELD},{SPRINT_FIELD},customfield_10014,"
              f"customfield_10016,duedate,created,issuetype,issuelinks")
    return f"{fields},{start_date_field}" if start_date_field else fields


def fetch_stories_for_epic(epic_key, start_date_field=None):
    """Fetch all stories belonging to an epic.

    Tries next-gen (parent) first, falls back to classic (Epic Link).
    Returns list of issue dicts.
    """
    fields = story_fields(start_date_field)

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


def fetch_stories_by_keys(story_keys, warn_missing=True, start_date_field=None):
    """Fetch specific stories by their Jira keys.

    With warn_missing, print a warning for each key that Jira does not return.
    """
    keys_str = ", ".join(story_keys)
    jql = f"key in ({keys_str})"
    fields = story_fields(start_date_field)
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


def fetch_finished_stories(start, end):
    """Fetch the stories of the project that moved to a done status from start up to end (not included)."""
    jql = (f'project = {JIRA_PROJECT} AND issuetype = Story AND statusCategory = Done '
           f'AND statusCategoryChangedDate >= "{start}" AND statusCategoryChangedDate < "{end}" ORDER BY key ASC')
    fields = f"summary,assignee,statuscategorychangedate,{STORY_POINTS_FIELD},customfield_10016"
    issues = []
    next_page_token = None
    while True:
        params = {"jql": jql, "fields": fields, "maxResults": PAGE_SIZE}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        data = jira_get("search/jql", params=params)
        issues.extend(data["issues"])
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return issues


def fetch_sprint_stories(include_open, include_future, start_date_field=None):
    """Fetch the project's stories in open sprints, future sprints or both."""
    sprint_functions = []
    if include_open:
        sprint_functions.append("openSprints()")
    if include_future:
        sprint_functions.append("futureSprints()")
    sprint_clause = " OR ".join(f"sprint in {f}" for f in sprint_functions)
    jql = f"project = {JIRA_PROJECT} AND issuetype = Story AND ({sprint_clause}) ORDER BY key ASC"
    fields = story_fields(start_date_field)
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


CHANGELOG_BULK_SIZE = 1000  # the most issues that one bulk changelog call of Jira takes
PARALLEL_CALLS = 4  # the most calls to Jira at a time


def history_day(created):
    """Return the day of the "created" time of a Jira change history.

    The time is text such as "2026-10-07T15:00:00.000+0200", or a number of milliseconds since
    1970, as in the bulk changelog. A number gives the day in the time zone of this computer.
    """
    if isinstance(created, (int, float)) or str(created).isdigit():
        return datetime.fromtimestamp(int(created) / 1000).date()
    return date.fromisoformat(created[:10])


def status_changes_from_histories(histories):
    """Return the status changes in a list of Jira change histories as (date, new status name), oldest first."""
    def order(history):
        # Jira numbers the histories in the order they were made, so the number orders changes on one day
        number = str(history.get("id", ""))
        return history_day(history["created"]), int(number) if number.isdigit() else 0

    changes = []
    for history in sorted(histories, key=order):
        day = history_day(history["created"])
        for item in history.get("items", []):
            if item.get("field") == "status":
                changes.append((day, item.get("toString") or ""))
    return changes


def fetch_status_changes(issue_key):
    """Return the status changes of a story as a list of (date, new status name), oldest first."""
    histories = []
    start_at = 0
    while True:
        data = jira_get(f"issue/{issue_key}/changelog", params={"startAt": start_at, "maxResults": 100})
        values = data.get("values", [])
        histories.extend(values)
        start_at += len(values)
        if data.get("isLast", True) or not values:
            break
    return status_changes_from_histories(histories)


def fetch_status_changes_bulk(issues):
    """Return {issue key: status changes} for the given issues, from the bulk changelog of Jira.

    issues are Jira issue dicts with an "id" and a "key". The status changes are as in
    fetch_status_changes. This raises requests.RequestException when a call fails, and
    ValueError when Jira answers in another form.
    """
    key_by_id = {str(issue["id"]): issue["key"] for issue in issues}
    histories = {key: [] for key in key_by_id.values()}
    ids = list(key_by_id)
    for first in range(0, len(ids), CHANGELOG_BULK_SIZE):
        body = {"issueIdsOrKeys": ids[first:first + CHANGELOG_BULK_SIZE], "fieldIds": ["status"],
                "maxResults": 10000}
        while True:
            data = jira_post("changelog/bulkfetch", body)
            if not isinstance(data.get("issueChangeLogs"), list):
                raise ValueError("the answer has no issueChangeLogs")
            for log in data["issueChangeLogs"]:
                key = key_by_id.get(str(log.get("issueId")))
                if key is None:
                    raise ValueError(f"the answer has an unknown issue {log.get('issueId')!r}")
                histories[key].extend(log.get("changeHistories") or [])
            if not data.get("nextPageToken"):
                break
            body = dict(body, nextPageToken=data["nextPageToken"])
    return {key: status_changes_from_histories(found) for key, found in histories.items()}


def fetch_all_status_changes(issues):
    """Return {issue key: status changes} for the given Jira issue dicts, which have an "id" and a "key".

    The status changes are as in fetch_status_changes. This uses the bulk changelog of Jira,
    one call per 1000 issues. When that fails, it fetches the changelog of each issue,
    PARALLEL_CALLS at a time.
    """
    if not issues:
        return {}
    try:
        return fetch_status_changes_bulk(issues)
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"Warning: the bulk changelog of Jira failed ({e}), so the history of each story is fetched "
              f"separately, {PARALLEL_CALLS} at a time", file=sys.stderr)
    keys = [issue["key"] for issue in issues]
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        return dict(zip(keys, pool.map(fetch_status_changes, keys)))


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


def implementation_stretches(status_changes, status, statuses):
    """Return the stretches a story spent in the given status (any case) or a later one.

    The result is a list of (first day, day it moved back) pairs, oldest first. The day it
    moved back is None for a stretch that is still going on. A later status is one with more
    progress in statuses that is not done. So a story that goes back from Code Review for
    rework stays in its stretch, but the days a story was parked, for example back in Ready
    for Development, are left out. Before the first change the story counts as in an earlier status.
    """
    progress = statuses.get(status.lower(), 0)
    stretches = []
    start = None
    for day, name in status_changes:
        in_work = progress <= statuses.get(name.lower(), 0) < 1.0
        if in_work and start is None:
            start = day
        elif not in_work and start is not None:
            if day > start:  # a move back on the same day leaves no stretch
                stretches.append((start, day))
            start = None
    if start is not None:
        stretches.append((start, None))
    return stretches


def link_dependencies(issues, link_types):
    """Return {story key: {key of a story it waits for, ...}} from the Jira links between the given issues.

    Only links of the given lowercase type names count. They must work like Blocks: an issue
    that "blocks" another must finish before the other, which "is blocked by" it, can start.
    Links to issues that are not in the given list are left out.
    """
    keys = {issue["key"] for issue in issues}
    dependencies = {}
    for issue in issues:
        for link in issue["fields"].get("issuelinks") or []:
            if (link.get("type") or {}).get("name", "").lower() not in link_types:
                continue
            # Jira describes a link from this issue: it "blocks" an outwardIssue
            # and "is blocked by" an inwardIssue
            if "outwardIssue" in link:
                waiting, first = link["outwardIssue"]["key"], issue["key"]
            elif "inwardIssue" in link:
                waiting, first = issue["key"], link["inwardIssue"]["key"]
            else:
                continue
            if waiting in keys and first in keys:
                dependencies.setdefault(waiting, set()).add(first)
    return dependencies


def fetch_jira_fields():
    """Return the list of all Jira fields, or [] with a warning if Jira does not give it."""
    try:
        return jira_get("field")
    except requests.RequestException as e:
        print(f"Warning: could not read the list of Jira fields ({e}), so stories and epics get no start date, "
              f"badge fields are not checked and badge tooltips show field IDs", file=sys.stderr)
        return []


def find_start_date_field(jira_fields):
    """Return the id of the Jira field named "Start date", or None if there is none."""
    for field in jira_fields:
        if field.get("name", "").lower() == "start date" and (field.get("schema") or {}).get("type") == "date":
            return field["id"]
    return None


def field_values(value):
    """Return the value of a Jira field as a list of (text, parent), or [] when the field is empty.

    This handles text, numbers, checkboxes and select lists (options with a "value"),
    cascading selects (an option with a "child" option, as "Parent > Child"),
    users (with a "displayName"), other objects with a "name", and rich text (an ADF document).
    parent is the parent option of a cascading select, and None for any other value.
    """
    if value is None or value is False or value == "":
        return []
    if isinstance(value, list):
        return [item_value for item in value for item_value in field_values(item)]
    if isinstance(value, dict) and value.get("type") == "doc":
        text = " ".join(adf_text(value).split())
        return [(text, None)] if text else []
    if isinstance(value, dict):
        for key in ("value", "displayName", "name"):
            # An option with the value 0 is that value, the same as a number field holding 0
            own = field_values(value.get(key)) if not isinstance(value.get(key), (dict, list)) else []
            if own:
                child = field_values(value.get("child"))
                parent = own[0][0]
                return [(f"{parent} > {child[0][0]}", parent) if child else (parent, None)]
        return []
    if value is True:
        return [("Yes", None)]
    if isinstance(value, float) and value.is_integer():
        return [(str(int(value)), None)]  # Jira gives numbers as floats; show 5, not 5.0
    return [(str(value), None)]


def adf_text(node):
    """Return the text in a rich text (ADF) node of Jira, with a space between its blocks.

    The text nodes inside a block are joined without a space: ADF splits a word such as
    "Pre" + bold "mium" into two text nodes wherever the formatting changes.
    Mentions, emoji, statuses, dates and links keep their text in their attrs.
    """
    kind, attrs = node.get("type"), node.get("attrs") or {}
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return " "
    if kind in ("mention", "emoji", "status"):
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind == "date":
        try:
            # The timestamp is midnight UTC of the date
            return datetime.fromtimestamp(int(attrs.get("timestamp")) / 1000, timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return ""
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return f" {attrs.get('url', '')} "
    inner = "".join(adf_text(child) for child in node.get("content", []) if isinstance(child, dict))
    # A block such as a paragraph, heading or list item has content; a space keeps it apart from the next
    return f" {inner} " if "content" in node else inner


BADGE_TEXT_LENGTH = 30  # a badge without a label cuts off longer field values; the tooltip has all of them


def epic_badges(fields, badges, field_names):
    """Return the badges for an epic with these Jira fields, as [{text, title, search, color, stripe}, ...].

    search is the label and the field values in full, for the search box.

    An epic gets a badge when the field of that badge has a value: one of the 'values' of the badge,
    or without 'values' any value but "No", "False" or 0. A value "Parent > Child" of a cascading
    select also matches on its parent alone. The badge shows its label, or those values of the field
    when it has no label, cut off at BADGE_TEXT_LENGTH. The label is never cut off, because the
    tooltip does not show it.
    """
    def matches(value, texts):
        text, parent = value
        return text.lower() in texts or (parent is not None and parent.lower() in texts)

    result = []
    for badge in badges:
        values = field_values(fields.get(badge["field"]))
        if badge["values"]:
            values = [v for v in values if matches(v, badge["values"])]
        else:
            values = [v for v in values if not matches(v, NO_BADGE_VALUES)]
        values = [text for text, _ in values]
        if not values:
            continue
        name = field_names.get(badge["field"], badge["field"])
        text = ", ".join(values)
        if len(text) > BADGE_TEXT_LENGTH:
            text = text[:BADGE_TEXT_LENGTH - 1].rstrip() + "…"
        text = badge["label"] or text
        result.append({
            "text": text,
            "title": f"{name}: {', '.join(values)}",
            "search": f"{badge['label']} {', '.join(values)}".strip(),
            "color": badge["color"],
            "stripe": badge["stripe"],
        })
    return result


def fetch_epics(epic_keys, start_date_field=None, badges=(), field_names=None):
    """Return {epic key: {key, name, space, start, due, badges}} for the given epics.

    space is the name of the Jira space (project) of the epic, and space_avatar the address of its
    avatar image, or "".

    start and due are YYYY-MM-DD strings, or "" when the epic has no such date in Jira.
    badges is the list from epic_badges for the [[badges]] of the configuration.
    """
    fields = ",".join(["summary", "duedate", "project"] + ([start_date_field] if start_date_field else [])
                      + [badge["field"] for badge in badges])
    jql = f"key in ({', '.join(epic_keys)})"
    epics = {}
    next_page_token = None
    while True:
        params = {"jql": jql, "fields": fields, "maxResults": PAGE_SIZE}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        data = jira_get("search/jql", params=params)
        for issue in data["issues"]:
            f = issue["fields"]
            epics[issue["key"]] = {
                "key": issue["key"],
                "name": f.get("summary") or issue["key"],
                "space": (f.get("project") or {}).get("name", ""),
                "space_avatar": ((f.get("project") or {}).get("avatarUrls") or {}).get("24x24", ""),
                "start": (f.get(start_date_field) or "")[:10] if start_date_field else "",
                "due": (f.get("duedate") or "")[:10],
                "badges": epic_badges(f, badges, field_names or {}),
            }
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return epics


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
    def download(url):
        try:
            resp = requests.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Warning: could not download icon {url}: {e}", file=sys.stderr)
            return None
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            print(f"Warning: icon {url} is not an image ({content_type or 'no type'})", file=sys.stderr)
            return None
        return f"data:{content_type};base64,{base64.b64encode(resp.content).decode('ascii')}"

    urls = sorted(set(urls))
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        return {url: image for url, image in zip(urls, pool.map(download, urls)) if image}


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


def is_in_team(d, availability):
    """Return True if date d is in one of the in_team periods of this member, or if there are none."""
    periods = availability.get("in_team")
    if periods is None:
        return True
    return any((first is None or first <= d) and (last is None or d <= last) for first, last in periods)


def last_team_day(availability):
    """Return the last day of the last in_team period of this member, or None if there is no last day."""
    periods = availability.get("in_team")
    if not periods or any(last is None for _, last in periods):
        return None
    return max(last for _, last in periods)


def hours_on(d, availability):
    """Return the hours this member works on date d. A day off, or a day outside the team, has 0 hours."""
    if d in availability.get("days_off", ()) or not is_in_team(d, availability):
        return 0
    return availability.get(DAY_KEYS[d.weekday()], 0)


def hours_worked(start, end, availability):
    """Return the hours this member works from start up to end (not included)."""
    hours = 0
    d = start
    while d < end:
        hours += hours_on(d, availability)
        d += timedelta(days=1)
    return hours


def next_working_day(d, availability):
    """Return the first day from d on where this member has hours > 0, or None if there is no such day."""
    last = last_team_day(availability)
    while last is None or d <= last:
        if hours_on(d, availability) > 0:
            return d
        d += timedelta(days=1)
    return None


def has_working_hours(availability):
    """Return True if the member has hours on at least one weekday."""
    return sum(availability.get(d, 0) for d in ("mon", "tue", "wed", "thu", "fri")) > 0


def plan_work(first_day, availability, hours):
    """Plan hours of work for one person from first_day on.

    Returns (start, last day, day after the last day, hours left), or None if the person has no
    working day left. Hours left is above 0 when the person leaves the team before the work is
    done; the plan then ends on their last working day.
    """
    start = next_working_day(first_day, availability)
    if start is None:
        return None
    end = start
    remaining = hours
    day = start
    while remaining > 0:
        day = next_working_day(day, availability)
        if day is None:
            break
        remaining -= hours_on(day, availability)
        end = day
        day += timedelta(days=1)
    return start, end, end + timedelta(days=1), max(remaining, 0)


def pick_member(member_states, team, earliest, hours):
    """Return (name, plan) of the auto-assign member who finishes the work first.

    member_states has the first free day of each member. A member who can finish the work wins
    over a member who leaves the team before it is done. If nobody can finish it, the member with
    the fewest hours left wins.
    """
    plans = {}
    for name, free_from in member_states.items():
        if team[name].get("auto_assign", True):
            plan = plan_work(max(free_from, earliest), team[name], hours)
            if plan is not None:
                plans[name] = plan
    if not plans:
        print("Error: no team member with auto_assign has working days left in the plan", file=sys.stderr)
        sys.exit(1)
    name = min(plans, key=lambda n: (plans[n][3], plans[n][1], plans[n][0]))
    return name, plans[name]


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

    # Plan the stories in groups, keeping the given order within a group:
    # done (so they don't inflate epic end dates), started, in the active sprint, the rest.
    # Started means at least the progress of work_starts_at in [statuses], or above 0 without it.
    def _group(key):
        if key not in story_data:
            return 3
        fields = story_data[key]["fields"]
        progress = config.statuses.get(fields.get("status", {}).get("name", "").lower(), 0)
        if progress >= 1.0:
            return 0
        started = (progress >= config.work_start_progress) if config.work_start_progress > 0 else (progress > 0)
        if started:
            return 1
        if in_active_sprint(fields):
            return 2
        return 3

    ordered_keys = sorted(ordered_keys, key=_group)
    # Then make sure dependencies are always scheduled before dependents
    ordered_keys = topological_sort(ordered_keys, config.dependencies)

    # Without auto-assign, stories without an assignee are planned one after another
    # under "Unassigned", on a standard week
    standard_week = {"mon": 8, "tue": 8, "wed": 8, "thu": 8, "fri": 8, "days_off": config.holidays}
    team = dict(config.team)
    if not config.auto_assign:
        team["Unassigned"] = standard_week

    # A story assigned in Jira to someone outside [team] stays with that person, who gets
    # their own row on a standard week and is never given other stories
    outsiders = sorted({get_assignee(story_data[key]["fields"]) for key in ordered_keys if key in story_data}
                       - set(team) - {"Unassigned"})
    for name in outsiders:
        print(f"Note: {name} is not in [team], planning their stories on a standard week", file=sys.stderr)
        team[name] = dict(standard_week, auto_assign=False)

    # member_states tracks the next available date per member
    member_states = {}  # {name: first day the member is free}, only for members with working days left
    for name in team:
        if has_working_hours(team[name]):
            first_day = next_working_day(config.start_date, team[name])
            if first_day is not None:
                member_states[name] = first_day

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

        # A story that started in the implementation status has already used part of its estimate.
        # It has what is left of the estimate after the hours worked in that status or a later one,
        # or what its status says is left if that is less. Over the estimate, only its status says
        # what is left. The days it was parked in an earlier status don't count as worked.
        # The hours worked are counted on the week of the Jira assignee, or a standard week.
        work_start = None
        if progress < 1.0 and issue.get("_implementation_date"):
            work_start = date.fromisoformat(issue["_implementation_date"])
            worked_week = team.get(assignee_name, standard_week)
            worked = sum(hours_worked(date.fromisoformat(start), date.fromisoformat(end) if end else date.today(),
                                      worked_week)
                         for start, end in issue.get("_implementation_stretches", []))
            if total_hours - worked > 0:
                hours = min(hours, total_hours - worked)

        member = None
        auto_assigned = False
        if progress >= 1.0 and story_key in done_dates:
            # A done story is shown on the days it was worked on, from its Jira status history
            start_date, end_date = done_dates[story_key]
            cursor = end_date + timedelta(days=1)
        else:
            # The story starts after its dependencies are finished, and an open story not in the past
            earliest = config.start_date
            for dep_key in config.dependencies.get(story_key, []):
                if dep_key in story_end_dates:
                    earliest = max(earliest, story_end_dates[dep_key])
                elif dep_key not in story_data:
                    print(f"Warning: dependency {dep_key} for {story_key} not in scheduled set, ignoring", file=sys.stderr)
            if progress < 1.0:
                earliest = max(earliest, date.today())
                # Work that has not started yet waits for the Start date of the story in Jira,
                # or else that of its epic
                if issue.get("_start_date") and _group(story_key) > 1:
                    try:
                        earliest = max(earliest, date.fromisoformat(issue["_start_date"]))
                    except ValueError:
                        print(f"Warning: start date '{issue['_start_date']}' of {story_key} is not a date, "
                              f"ignoring it", file=sys.stderr)

            if progress >= 1.0:
                # A done story keeps its assignee from Jira and takes no time from anyone.
                # Without Jira dates, it is placed on standard working days from the plan start.
                plan = plan_work(earliest, standard_week, hours)
            else:
                # A story assigned in Jira stays with its assignee, also one outside [team].
                # Other stories go to the auto-assign member who finishes them first, or to
                # "Unassigned" when auto_assign is false.
                plan = None
                if assignee_name in member_states:
                    member = assignee_name
                    plan = plan_work(max(member_states[member], earliest), team[member], hours)
                    if plan is None:
                        member = None
                if member is None:
                    action = "auto-assigning" if config.auto_assign else "planning under 'Unassigned'"
                    if assignee_name != "Unassigned":
                        print(f"Warning: assignee '{assignee_name}' for {story_key} has no working hours left "
                              f"in the plan, {action}", file=sys.stderr)
                    if config.auto_assign:
                        member, plan = pick_member(member_states, team, earliest, hours)
                        auto_assigned = True
                    else:
                        member = "Unassigned"
                        plan = plan_work(max(member_states[member], earliest), team[member], hours)
                if plan[3] > 0:
                    print(f"Warning: {story_key} does not fit before '{member}' leaves the team on {plan[1]}, "
                          f"so its bar ends there", file=sys.stderr)
            start_date, end_date, cursor, _ = plan

            # The days already worked show in the past
            if work_start and work_start < start_date:
                start_date = work_start

        if member is not None:
            # Only open stories take time from their member
            free_from = next_working_day(cursor, team[member])
            if free_from is None:
                del member_states[member]
            else:
                member_states[member] = free_from
        story_end_dates[story_key] = cursor

        scheduled.append({
            "key": story_key,
            "title": title,
            "epic": issue.get("_epic", NO_EPIC),
            "epic_key": issue.get("_epic_key", ""),
            "assignee": member or assignee_name,
            "auto_assigned": auto_assigned,  # the planner picked the member, not Jira
            "status": status,
            "sprint": sprint,
            "due": issue["fields"].get("duedate") or "",
            "start_date": issue.get("_start_date", ""),  # the Start date from Jira, of the story or its epic
            "created": (issue["fields"].get("created") or "")[:10],
            "implementation_date": issue.get("_implementation_date", ""),
            "implementation_stretches": issue.get("_implementation_stretches", []),
            "implementation_status": config.implementation_status,
            "issue_type":(issue["fields"].get("issuetype") or {}).get("name", ""),
            "issue_type_icon_url": (issue["fields"].get("issuetype") or {}).get("iconUrl", ""),
            "effort": 0 if progress >= 1.0 else hours,
            "total_hours": total_hours,
            "points": sp,
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


def format_hours(hours):
    """Return hours as "8h" or "7.5h"."""
    return f"{hours:.0f}h" if hours == int(hours) else f"{hours:.1f}h"


def format_points(points):
    """Return story points as "5" or "0.5", or "" for a story without points."""
    return "" if points is None else f"{points:g}"


def story_age_days(created):
    """Return the number of days from a created date (YYYY-MM-DD) up to today, or None without a date."""
    if not created:
        return None
    try:
        return max((date.today() - date.fromisoformat(created[:10])).days, 0)
    except ValueError:
        return None


HOURS_PER_DAY = 8  # one working day, to turn an estimate in hours into days


def implementation_work_days(stretches, holidays):
    """Return the working days in the given stretches, or None without stretches.

    Each stretch is [first day, day it moved back] as YYYY-MM-DD, with "" for a stretch that is
    still going on, which counts up to today. Weekends and holidays don't count. A stretch that
    started today, or that ended on the day it started, has 0 days.
    """
    if not stretches:
        return None
    try:
        return sum(max(working_days_between(date.fromisoformat(start[:10]),
                                            date.fromisoformat(end[:10]) if end else date.today(),
                                            set(holidays)), 0)
                   for start, end in stretches)
    except ValueError:
        return None


def sprint_number(sprint):
    """Return the number at the end of a sprint name, such as "26" for "DTIC Sprint 26", or the whole name."""
    match = re.search(r"(\d+)\s*$", sprint)
    return match.group(1) if match else sprint


def format_signed(value, unit):
    """Return a number with its sign and unit, for example "+8h" or "−3 working days"."""
    sign = "+" if value > 0 else "−" if value < 0 else ""
    number = format_hours(abs(value))[:-1] if unit == "h" else f"{abs(value):g}"
    return f"{sign}{number}{unit}"


def trend_html(snapshots, epic_color_map, holidays, compare_date=None, epic_spaces=None):
    """Return the HTML of a milestone trend chart: the planned end of each epic at each run.

    The table below the chart compares the latest run with the run of compare_date, or with
    the oldest run if compare_date is None. With epic_spaces, {epic name: Jira space name}, the
    table has a header row above the epics of each space.

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

    def table_row(name, points, css_class=""):
        then, last = dict(points).get(compare_run), points[-1][1]
        if then is None:
            then_cell = moved_cell = "–"
        else:
            moved = working_days_between(then + timedelta(days=1), last + timedelta(days=1), holidays)
            then_cell = then.isoformat()
            moved_cell = format_signed(moved, " working days") if moved else "no change"
        row_class = f' class="{css_class}"' if css_class else ""
        return (f"<tr{row_class}><td>{html.escape(name)}</td><td>{then_cell}</td>"
                f"<td>{last}</td><td>{moved_cell}</td></tr>")

    rows = []
    current_space = None
    for name, points in series.items():
        space = (epic_spaces or {}).get(name, "")
        # An epic without a space after a space group gets its own header, so it does not look part of that group
        if epic_spaces and space != current_space and (space or current_space):
            rows.append(f'<tr class="space-row"><th colspan="4">{html.escape(space or "No space")}</th></tr>')
        current_space = space
        rows.append(table_row(name, points))
    rows.append(table_row("Whole plan", whole_plan, "whole-plan-row"))
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


def plan_history(snapshots, today):
    """Return the snapshots before today, newest first, for the plan comparison in the chart.

    Each is {"date", "stories": {key: [start, end, progress, total hours, remaining hours, status]}},
    with end the day after the last planned day.
    """
    plans = []
    for snap in snapshots:
        if snap["date"] >= today.isoformat():
            continue
        plans.append({
            "date": snap["date"],
            "stories": {s["key"]: [s["start"], s["end"], s["progress"], s.get("total_hours"),
                                   s["remaining_hours"], s["status"]] for s in snap["stories"]},
        })
    plans.sort(key=lambda plan: plan["date"], reverse=True)
    return plans


def removed_stories(snapshots, current_keys, today):
    """Return the stories that are open in a snapshot before today and not in current_keys.

    Each story is given once, as in the newest snapshot that has it.
    """
    removed = {}
    for snap in sorted(snapshots, key=lambda snap: snap["date"], reverse=True):
        if snap["date"] >= today.isoformat():
            continue
        for s in snap["stories"]:
            if s["key"] not in current_keys and s["key"] not in removed and s["progress"] < 1.0:
                removed[s["key"]] = s
    return list(removed.values())


# The thin line of a space row in the timeline
SPACE_COLOR = "#8a9bb0"

EPIC_COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]


MEMBER_COLORS = ["#e15759", "#4e79a7", "#59a14f", "#b07aa1", "#f28e2b", "#76b7b2", "#9c755f", "#edc948",
                 "#ff9da7", "#bab0ac"]


def member_initials(name):
    """Return the initials of a team member: "Rolf Heller" is "RH", "jeroen.de.koning" is "JK"."""
    words = [w for w in re.split(r"[\s._-]+", name) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def days_off_marks(team, holidays, bar_from=DEFAULT_VISUALS["days_off_bar_from"]):
    """Return the days off of each team member, for the marks in the date header of the chart.

    The result is [{name, initials, color, runs: [{first, last, days, lane}]}], one entry per member
    with days off, in the order of [team]. A run is a period of days off: its days are the working
    days of the member in it, so without weekends, holidays, days with 0 hours and days outside the
    team. Two days off are in one run when the member has no working day between them. A run of
    bar_from days or more is shown as one bar, in its lane; a shorter run has lane None.
    """
    holidays = set(holidays)

    def works(d, avail):
        return d not in holidays and is_in_team(d, avail) and avail.get(DAY_KEYS[d.weekday()], 0) > 0

    members = []
    for i, (name, avail) in enumerate(team.items()):
        days = sorted(d for d in avail.get("own_days_off", ()) if works(d, avail))
        runs = []
        for d in days:
            if runs:
                last = runs[-1]["days"][-1]
                between = (last + timedelta(days=n) for n in range(1, (d - last).days))
                if not any(works(b, avail) for b in between):
                    runs[-1]["days"].append(d)
                    continue
            runs.append({"days": [d]})
        if runs:
            members.append({"name": name, "initials": member_initials(name),
                            "color": MEMBER_COLORS[i % len(MEMBER_COLORS)], "runs": runs})
    # The bars of long runs that overlap go in lanes above each other
    lane_ends = []
    long_runs = sorted((r for m in members for r in m["runs"] if len(r["days"]) >= bar_from),
                       key=lambda r: r["days"][0])
    for run in long_runs:
        lane = next((n for n, end in enumerate(lane_ends) if end < run["days"][0]), len(lane_ends))
        if lane == len(lane_ends):
            lane_ends.append(None)
        lane_ends[lane] = run["days"][-1]
        run["lane"] = lane
    for m in members:
        m["runs"] = [{"first": r["days"][0].isoformat(), "last": r["days"][-1].isoformat(),
                      "days": [d.isoformat() for d in r["days"]], "lane": r.get("lane")} for r in m["runs"]]
    return members


def write_gantt_html(scheduled_stories, status_map, dependencies=None, holidays=(), status_colors=None,
                     compare_date=None, snapshots=(), removed=(), icon_images=None, epic_icon_url="",
                     epics_info=None, roadmap_epics=(), epic_order=(), visuals=None, group_by_space=False,
                     keep_story_order=False, team=None):
    """Write a self-contained HTML file with a DHTMLX Gantt chart.

    With snapshots from before today, the page can compare the plan with any of them: its list picks
    one, compare_date (a snapshot date) at first. removed (from removed_stories) are the stories that
    are open in an earlier snapshot and no longer in the plan.
    With two or more snapshots, a trend chart of the planned end dates is added below it.
    icon_images is {icon address: data URI} for the issue type icons from Jira. A type
    without an image there is shown with an emoji.
    epics_info is {epic key: {key, name, start, due}} from fetch_epics. roadmap_epics lists
    epics without stories; each is shown as a striped bar from its start to its due date.
    epic_order is the list of epic keys in the order of the epic rows. Epics that are not in it
    come after them.
    With group_by_space, the epics are put under a header row per Jira space, when they are in more
    than one space. epic_order must then list the epics of a space together.
    With keep_story_order, the stories of an epic keep the order of scheduled_stories, such as the
    Jira rank, instead of being sorted by start date. Done stories still come first.
    visuals is the [visuals] of the configuration, from parse_visuals.
    team is the [team] of the configuration. The days off of its members are marked in the date header.
    """
    # Assign colors to epics
    epics_seen = []
    for s in scheduled_stories:
        if s["epic"] not in epics_seen:
            epics_seen.append(s["epic"])
    # Sort: epic order, done stories first, then by start date or in the given order
    given_position = {id(s): i for i, s in enumerate(scheduled_stories)}

    def story_sort_key(s):
        epic_order = epics_seen.index(s["epic"])
        done = 0 if status_map.get(s["status"].lower(), 0) >= 1.0 else 1
        if keep_story_order:
            return (epic_order, done, given_position[id(s)])
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

    # Order of the epic rows: the epics of epic_order in its order, then the others
    epics_info = epics_info or {}
    roadmap_by_key = {r["key"]: r for r in roadmap_epics}
    key_to_name = {key: name for name, key in epic_name_to_key.items()}
    epic_rows = []
    for key in epic_order:
        if key in key_to_name and ("stories", key_to_name[key]) not in epic_rows:
            epic_rows.append(("stories", key_to_name[key]))
        elif key in roadmap_by_key:
            epic_rows.append(("roadmap", roadmap_by_key[key]))
    epic_rows += [("stories", name) for name in epics_seen if ("stories", name) not in epic_rows]
    # The stories without an epic come first
    if ("stories", NO_EPIC) in epic_rows:
        epic_rows.remove(("stories", NO_EPIC))
        epic_rows.insert(0, ("stories", NO_EPIC))
    row_names = [row if kind == "stories" else row["name"] for kind, row in epic_rows]
    epic_color_map = {name: EPIC_COLORS[i % len(EPIC_COLORS)] for i, name in enumerate(row_names)}

    # The [[badges]] of an epic: pills before its name, so a long name does not push them out of
    # the Story column, and stripes on the left of its row, side by side when several badges have one.
    # Each combination of stripe colors has one CSS class.
    stripe_classes = {}

    def badge_fields(epic_key):
        badges = epics_info.get(epic_key, {}).get("badges", [])
        pills = "".join(
            f"<span class='field-badge' style='--badge-color: {b['color']}' "
            f"title='{html.escape(b['title'])}'>{html.escape(b['text'])}</span>"
            for b in badges)
        stripes = tuple(dict.fromkeys(b["color"] for b in badges if b["stripe"]))
        if stripes and stripes not in stripe_classes:
            stripe_classes[stripes] = f"badge-stripe-{len(stripe_classes)}"
        # The search box also finds an epic by the label and values of its badges, also by the part
        # a badge cuts off. Not by the field name in the tooltip: a word such as "feature" in it
        # would match every epic with that badge.
        search_text = " ".join(b["search"] for b in badges)
        return {"badges_html": pills, "badges_text": search_text, "stripe_class": stripe_classes.get(stripes, "")}

    # A header row per Jira space above its epics, when the epics are in more than one space
    def row_space(kind, row):
        key = row["key"] if kind == "roadmap" else epic_name_to_key.get(row, "")
        return epics_info.get(key, {})

    spaces = {row_space(kind, row).get("space", "") for kind, row in epic_rows if row != NO_EPIC} - {""}
    use_spaces = group_by_space and len(spaces) > 1
    # {epic row name: space name} for the table of the trend chart
    epic_spaces = {(row if kind == "stories" else row["name"]): row_space(kind, row).get("space", "")
                   for kind, row in epic_rows if row != NO_EPIC} if use_spaces else None
    space_task_ids = {}  # {space name: task id}
    epic_parents = {}    # {epic task id: space task id}

    # Create a parent task per epic
    for kind, row in epic_rows:
        space_info = row_space(kind, row) if use_spaces and row != NO_EPIC else {}
        space = space_info.get("space", "")
        if space and space not in space_task_ids:
            task_id += 1
            space_task_ids[space] = task_id
            tasks.append({
                "id": task_id,
                "text": space,
                "effort": "",
                "status": "",
                "color": SPACE_COLOR,
                "type": "project",
                "is_space": True,
                "open": True,
                "icon_url": space_info.get("space_avatar", ""),
                "progress": 0,
            })
        task_id += 1
        if space:
            epic_parents[task_id] = space_task_ids[space]
        if kind == "roadmap":
            # An epic without stories is shown as a striped bar from its start date to its due date in Jira
            first, last = row["start"] or row["due"], row["due"] or row["start"]
            tasks.append({
                "id": task_id,
                "text": row["name"],
                "start_date": first,
                "end_date": (date.fromisoformat(last) + timedelta(days=1)).isoformat(),
                "effort": "",
                "status": "",
                "color": epic_color_map[row["name"]],
                "icon": ISSUE_TYPE_ICONS["epic"],
                "icon_url": epic_icon_url,
                "story_key": row["key"],
                "progress": 0,
                "roadmap": True,
                "is_epic": True,
                "due_date": row["due"],
                **({"parent": epic_parents[task_id]} if task_id in epic_parents else {}),
                **badge_fields(row["key"]),
            })
            continue
        epic_name = row
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

        epic_points = sum(s.get("points") or 0 for s in epic_stories)

        tasks.append({
            "id": task_id,
            "text": epic_name,
            "start_date": epic_start,
            "end_date": epic_end,
            "points": format_points(epic_points) if epic_points else "",
            "effort": effort_str,
            "status": "",
            "color": epic_color_map[epic_name],
            "type": "project",
            "is_epic": True,
            # The stories without an epic are a group, not one piece of work, so the row gets no bar
            "no_bar": epic_name == NO_EPIC,
            "open": True,
            "icon": ISSUE_TYPE_ICONS["epic"],
            "unestimated": epic_unestimated,
            "icon_url": epic_icon_url,
            "story_key": epic_name_to_key.get(epic_name, ""),
            "due_date": epics_info.get(epic_name_to_key.get(epic_name, ""), {}).get("due", ""),
            "jira_start_date": epics_info.get(epic_name_to_key.get(epic_name, ""), {}).get("start", ""),
            "progress": epic_progress,
            **({"parent": epic_parents[task_id]} if task_id in epic_parents else {}),
            "hours": epic_effort,
            "points_value": epic_points,
            **badge_fields(epic_name_to_key.get(epic_name, "")),
        })

    # A space row spans the bars of its epics and adds up their hours and points
    for space_id in space_task_ids.values():
        space_task = next(t for t in tasks if t["id"] == space_id)
        epics = [t for t in tasks if t.get("parent") == space_id]
        space_task["start_date"] = min(t["start_date"] for t in epics)
        space_task["end_date"] = max(t["end_date"] for t in epics)
        hours = sum(t.get("hours", 0) for t in epics)
        points = sum(t.get("points_value", 0) for t in epics)
        unestimated = any(t.get("unestimated") for t in epics)
        space_task["effort"] = (f"{hours:.0f}h" if hours == int(hours) else f"{hours:.1f}h") + ("?" if unestimated else "")
        space_task["points"] = format_points(points) if points else ""
        space_task["unestimated"] = unestimated

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
            "auto_assigned": s.get("auto_assigned", False),
            "start_date": s["start"],
            "end_date": s["end"],
            "progress": progress,
            "points": format_points(s.get("points")),
            "points_value": s.get("points") or 0,
            "hours": effort,
            "sprint": sprint_number(s.get("sprint", "")),
            "sprint_name": s.get("sprint", ""),
            "effort": effort_str,
            "status": s["status"],
            "color": epic_color_map[s["epic"]],
            "parent": epic_task_ids[s["epic"]],
            "story_key": s["key"],
            "due_date": s.get("due", ""),
            "jira_start_date": s.get("start_date", ""),
            "created": s.get("created", ""),
            # Days since the story was created, only for open stories: a done story stops ageing
            "age_days": story_age_days(s.get("created", "")) if progress < 1.0 else None,
            "implementation_date": s.get("implementation_date", ""),
            "implementation_status": s.get("implementation_status", ""),
            # Working days in the implementation status or a later one, only for open stories
            "implementation_days": (implementation_work_days(s.get("implementation_stretches", []), holidays)
                                    if progress < 1.0 else None),
            # True when the story went back to an earlier status in between, so those days don't count
            "implementation_parked": len(s.get("implementation_stretches", [])) > 1,
            # The estimate in working days of 8 hours, to compare with the days in implementation
            "estimate_days": round(s.get("total_hours", 0) / HOURS_PER_DAY, 1),
            "icon": issue_type_icon(s.get("issue_type", "")),
            "icon_url": s.get("issue_type_icon_url", ""),
            "unestimated": unestimated,
            # The estimate including the done part, to compare with an earlier plan
            "total_hours": s.get("total_hours", 0),
        })

    # Stories that are open in an earlier plan and no longer in this one, in one group at the end. The page
    # shows those of the compared plan, with their dates and status in that plan.
    if removed:
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
            # Not a "project": that takes its dates from all its stories, also those the page hides
            "type": "task",
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
    header_visuals = visuals or DEFAULT_VISUALS
    days_off = (days_off_marks(team or {}, holidays, header_visuals["days_off_bar_from"])
                if header_visuals["enable_vacation_banners"] else [])
    days_off_json = json.dumps(days_off, separators=(",", ":"))
    holiday_icon_json = json.dumps(header_visuals["holiday_icon"])
    icon_images_json = json.dumps(icon_images or {})
    plans_json = json.dumps(plan_history(snapshots, date.today()), separators=(",", ":"))
    compare_date_json = json.dumps(compare_date)
    trend = trend_html(list(snapshots), epic_color_map, set(holidays), compare_date, epic_spaces)

    # The outline of a story or epic that ends after its due date, blinking or not
    visuals = visuals or DEFAULT_VISUALS
    past_due = visuals["past_due_color"]
    behind = visuals["behind_baseline_color"]
    search = visuals["search_color"]
    behind_css = (f"  .gantt_task_line.change-late {{ box-shadow: 0 0 0 2px {behind}, "
                  f"0 0 6px 3px color-mix(in srgb, {behind} 60%, transparent); }}")
    if visuals["behind_baseline_blink"]:
        behind_css = f"""  .gantt_task_line.change-late {{ box-shadow: 0 0 0 2px {behind}; animation: change-late-glow 0.5s ease-in-out infinite; }}
  @keyframes change-late-glow {{
    0%, 100% {{ box-shadow: 0 0 0 2px {behind}, 0 0 2px 1px color-mix(in srgb, {behind} 30%, transparent); }}
    50% {{ box-shadow: 0 0 0 2px {behind}, 0 0 8px 4px color-mix(in srgb, {behind} 60%, transparent); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .gantt_task_line.change-late {{ animation: none; }}
  }}"""
    past_due_css = (f"  .gantt_task_line.due-late {{ box-shadow: 0 0 0 2px {past_due}, "
                    f"0 0 6px 3px color-mix(in srgb, {past_due} 45%, transparent); }}")
    if visuals["past_due_blink"]:
        past_due_css = f"""  .gantt_task_line.due-late {{ box-shadow: 0 0 0 2px {past_due}; animation: due-late-glow 0.5s ease-in-out infinite; }}
  @keyframes due-late-glow {{
    0%, 100% {{ box-shadow: 0 0 0 2px {past_due}, 0 0 2px 1px color-mix(in srgb, {past_due} 30%, transparent); }}
    50% {{ box-shadow: 0 0 0 2px {past_due}, 0 0 8px 4px color-mix(in srgb, {past_due} 60%, transparent); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .gantt_task_line.due-late {{ animation: none; }}
  }}"""

    # Each status color has one CSS class, used on the grid row and the timeline row
    status_colors = status_colors or {}
    status_classes = {name: f"status-color-{i}" for i, name in enumerate(status_colors)}
    status_classes_json = json.dumps(status_classes)
    status_css = "\n".join(
        f"  #gantt_here .gantt_row.{cls}, #gantt_here .gantt_task_row.{cls} {{ background: {status_colors[name]}; }}"
        for name, cls in status_classes.items()
    )
    status_css += "".join(
        # One 4px stripe per color; a wider shadow lies under the narrower ones before it. The padding
        # keeps the icon and name 2px clear of the stripes, as dhtmlx's 6px does for one stripe.
        f"\n  .gantt_row.{cls} .gantt_cell:first-child {{ padding-left: {4 * len(colors) + 2}px; box-shadow: "
        f"{', '.join(f'inset {4 * (i + 1)}px 0 0 {color}' for i, color in enumerate(colors))}; }}"
        for colors, cls in stripe_classes.items()
    )

    print(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Gantt Chart — {date.today()}</title>
<link rel="stylesheet" href="https://cdn.dhtmlx.com/gantt/9.0/dhtmlxgantt.css">
<style>
  body {{ font-family: sans-serif; margin: 20px; }}
  /* Keep bar colours and row backgrounds when printing / saving as PDF */
  @media print {{
    * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
  #gantt_here {{ width: 100%; height: calc(100vh - 150px); }}
  .gantt_task_line.task-done {{ opacity: 0.45; filter: grayscale(60%); }}
  .gantt_task_line.task-done .gantt_task_content {{ text-decoration: line-through; }}
  .gantt_row.task-done-row {{ color: #999; }}
  .gantt_task_line .gantt_task_progress {{ background: rgba(0, 0, 0, 0.3); }}
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
  #toolbar button:disabled {{ cursor: wait; color: #888; background: #f4f4f4; }}
  /* A failed refresh: one line under the refresh button, cut off with … ; its tooltip has the whole message */
  #refresh-error {{ display: flex; align-items: center; gap: 6px; margin: -6px 0 10px; font-size: 12px; color: #c33; }}
  #refresh-error[hidden] {{ display: none; }}
  #refresh-error-text {{ min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  #refresh-error-close {{
    flex: none; padding: 0 4px; border: none; background: none; color: #c33;
    font-size: 16px; line-height: 1; cursor: pointer;
  }}
  #refresh-error-close:hover {{ color: #900; }}
  /* Covers the page while a refresh from Jira runs; the page reloads when it is done */
  #refresh-overlay {{
    position: fixed; inset: 0; z-index: 10000; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 14px;
    background: rgba(255, 255, 255, 0.7); color: #333; font-size: 14px; cursor: wait;
  }}
  #refresh-overlay[hidden] {{ display: none; }}
  #refresh-overlay .spinner {{
    width: 44px; height: 44px; border: 4px solid #d5dfeb; border-top-color: #4e79a7;
    border-radius: 50%; animation: refresh-spin 0.8s linear infinite;
  }}
  @keyframes refresh-spin {{ to {{ transform: rotate(360deg); }} }}
  /* Marks a member the planner picked; the soft blue of the toolbar, quiet next to the grey label */
  .auto-badge {{
    display: inline-block; margin-left: 3px; padding: 0 5px; border: 1px solid #c9d7e8; border-radius: 7px;
    background: #eaf0f7; color: #3d6491; font-size: 9px; font-weight: 600; line-height: 12px;
    letter-spacing: 0.3px; text-transform: uppercase; vertical-align: 1px; cursor: help;
  }}
  /* Days since a story was created; neutral grey so it reads as information, not as a warning */
  .age-badge {{
    display: inline-block; margin-left: 3px; padding: 0 5px; border: 1px solid #d6d6d6; border-radius: 7px;
    background: #f2f2f2; color: #555; font-size: 9px; font-weight: 600; line-height: 12px;
    vertical-align: 1px; cursor: help;
  }}
  /* Days since a story started in the implementation status */
  .impl-badge {{
    display: inline-block; margin-left: 3px; padding: 0 5px; border: 1px solid #b9dcc4; border-radius: 7px;
    background: #ebf6ee; color: #2f6b42; font-size: 9px; font-weight: 600; line-height: 12px;
    vertical-align: 1px; cursor: help;
  }}
  /* Longer in implementation than the estimate */
  .impl-badge.impl-over {{ border-color: #f0c08a; background: #fdf1e3; color: #9a5a12; }}
  /* A [[badges]] pill on an epic row, in a light and a dark shade of the color of the badge */
  .field-badge {{
    display: inline-block; margin-right: 4px; padding: 0 5px; border-radius: 7px;
    border: 1px solid color-mix(in srgb, var(--badge-color) 55%, #fff);
    background: color-mix(in srgb, var(--badge-color) 15%, #fff);
    color: color-mix(in srgb, var(--badge-color) 65%, #000);
    font-size: 9px; font-weight: 600; line-height: 12px; letter-spacing: 0.3px;
    text-transform: uppercase; vertical-align: 1px; cursor: help;
  }}
  /* Story points in the Points column; the green of the impl badge */
  .points-badge {{
    display: inline-block; min-width: 14px; padding: 0 6px; border: 1px solid #b9dcc4; border-radius: 8px;
    background: #ebf6ee; color: #2f6b42; font-size: 11px; font-weight: 600; line-height: 16px;
    text-align: center; vertical-align: middle;
  }}
  .points-badge.points-over {{ border-color: #f0c08a; background: #fdf1e3; color: #9a5a12; }}
  .gantt_row.task-done-row .points-badge {{ opacity: 0.6; }}
  /* Epic rows stand out from their stories */
  #gantt_here .gantt_row.epic-row, #gantt_here .gantt_task_row.epic-row {{ background: #eef2f7; }}
  .gantt_row.epic-row {{ font-weight: 600; }}
  /* A darker shade of the blue-grey epic row background */
  .gantt_row.epic-row .points-badge {{ border-color: #52677f; background: #52677f; color: #fff; }}
  /* A space row is a section heading above its epics, one shade darker than an epic row */
  #gantt_here .gantt_row.space-row, #gantt_here .gantt_task_row.space-row {{ background: #dde4ec; }}
  .gantt_row.space-row {{ font-weight: 700; color: #34495e; }}
  .gantt_row.space-row .gantt_cell:first-child span {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; }}
  .gantt_row.space-row .points-badge {{ border-color: #34495e; background: #34495e; color: #fff; }}
  img.type-icon.space-avatar {{ border-radius: 3px; }}
  /* In the timeline a space is a thin line under the bars of its epics, not a bar of its own */
  .gantt_task_line.space-bar {{ background: transparent; border: none; box-shadow: none; }}
  .gantt_task_line.space-bar .gantt_task_progress_wrapper,
  .gantt_task_line.space-bar .gantt_task_content {{ display: none; }}
  .gantt_task_line.space-bar::before {{
    content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 4px; margin-top: -2px;
    border-radius: 2px; background: {SPACE_COLOR};
  }}
  #show-done-wrap {{ display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }}
  #search-wrap {{ position: relative; margin-left: auto; }}
  #search-input {{
    padding: 5px 10px; border: 1px solid #ccc; border-radius: 4px;
    font-size: 13px; min-width: 220px;
  }}
  #search-input:focus {{ outline: none; border-color: #4e79a7; }}
  /* While the search box has text, it and the rows it found have a frame in the search color */
  #search-input.searching {{ border-color: {search}; box-shadow: 0 0 0 1px {search}; }}
  /* Under the search box, so a longer count does not move the box */
  #search-count {{ position: absolute; top: 100%; right: 0; margin-top: 2px; font-size: 12px; color: #666; white-space: nowrap; }}
  #member-filter {{ margin-bottom: 12px; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 13px; }}
  #member-filter .filter-label {{ color: #666; margin-right: 4px; }}
  .member-badge {{
    padding: 3px 10px; border: 1px solid #ccc; border-radius: 12px;
    background: #fff; cursor: pointer; font-size: 12px;
  }}
  .member-badge:hover {{ background: #e8e8e8; }}
  .member-badge.active {{ background: #4e79a7; color: #fff; border-color: #4e79a7; }}
  .member-badge .count {{ margin-left: 4px; opacity: 0.7; }}
  #member-clear {{ border: none; background: none; color: #4e79a7; cursor: pointer; font-size: 12px; text-decoration: underline; }}
  .gantt_task_line.search-dim {{ opacity: 0.2; }}
  .gantt_row.search-dim-row {{ opacity: 0.45; }}
  /* One frame around a found row: the grid half has no right edge, the timeline half no left edge */
  #gantt_here .gantt_row.search-match-row::after, #gantt_here .gantt_task_row.search-match-row::after {{
    content: ""; position: absolute; inset: 0; z-index: 1; pointer-events: none; border: 2px solid {search};
  }}
  #gantt_here .gantt_row.search-match-row::after {{ border-right: none; }}
  #gantt_here .gantt_task_row.search-match-row::after {{ border-left: none; }}
  #gantt_here .gantt_row.change-new-row, #gantt_here .gantt_task_row.change-new-row {{ background: #fff3bf; }}
  /* The row under the mouse darkens in the grid and in the timeline. The row colors above have the
     #gantt_here prefix, so they win over the grey hover of dhtmlx, and this layer darkens them. */
  #gantt_here {{ --dhx-gantt-base-colors-hover-color: var(--dhx-gantt-task-row-background); }}
  /* The date row of the header has a line of marks under the date: holidays and days off. dhtmlx gives
     the rows of the header the same height, so the date row, the last one, takes what the row above leaves. */
  #gantt_here .gantt_task_scale {{ display: flex; flex-direction: column; }}
  #gantt_here .gantt_task_scale .gantt_scale_line {{ height: auto !important; flex: 1 1 auto; }}
  #gantt_here .gantt_task_scale .gantt_scale_line:not(:last-child) {{ flex: 0 0 24px; line-height: 24px !important; }}
  #gantt_here .gantt_task_scale .gantt_scale_line:last-child {{ line-height: normal !important; }}
  #gantt_here .gantt_scale_line:last-child .gantt_scale_cell {{ height: 100%; }}
  #gantt_here .gantt_scale_line:last-child .gantt_scale_cell > span {{ display: block; height: 100%; }}
  .marked-cell {{ position: relative; height: 100%; }}
  .marked-label {{ line-height: 16px; padding-top: 2px; }}
  .marked-marks {{ display: flex; justify-content: center; align-items: center; height: 16px; }}
  .holiday-flag {{ color: #c62828; }}
  .holiday-count {{ font-size: 10px; color: #c62828; font-weight: 600; margin: 0 3px 0 1px; }}
  .holiday-text {{ font-size: 9px; font-weight: 700; letter-spacing: 0.3px; line-height: 13px; }}
  .off-avatar {{ display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px;
    border-radius: 50%; border: 1px solid #fff; box-sizing: border-box; color: #fff; font-size: 8px;
    font-weight: 700; letter-spacing: -0.2px; text-transform: none; line-height: 1; }}
  .off-avatar + .off-avatar, .holiday-flag + .off-avatar {{ margin-left: -4px; }}
  .holiday-flag + .off-avatar {{ margin-left: 2px; }}
  .off-avatar.off-more {{ background: #888; }}
  /* A long absence: a bar across its days, one lane for each absence at the same time */
  .off-bar {{ position: absolute; left: -1px; right: -1px; height: 9px; color: #fff; font-size: 7px; font-weight: 700;
    line-height: 9px; padding-left: 4px; text-align: left; text-transform: none; overflow: hidden; }}
  .off-bar.bar-first {{ left: 2px; border-radius: 5px 0 0 5px; }}
  .off-bar.bar-last {{ right: 2px; border-radius: 0 5px 5px 0; }}
  .off-bar.bar-first.bar-last {{ border-radius: 5px; }}
  /* The info popup has the look of the dhtmlx tooltip (class gantt_tooltip); the page places it */
  #task-popup {{ position: fixed; z-index: 10001; pointer-events: none; }}
  #task-popup:not(.shown) {{ display: none; }}
  #gantt_here .gantt_row.row-hover, #gantt_here .gantt_task_row.row-hover {{
    background-image: linear-gradient(rgba(0, 0, 0, 0.07), rgba(0, 0, 0, 0.07)) !important;
  }}
  .gantt_row.change-removed-row {{ color: #999; text-decoration: line-through; }}
  .gantt_task_line.change-removed {{ opacity: 0.5; }}
  .gantt_task_line.roadmap-epic {{ opacity: 0.75;
    background-image: repeating-linear-gradient(45deg, rgba(255,255,255,0.45) 0 6px, transparent 6px 12px); }}
  .gantt_task_line.no-bar {{ display: none; }}
{behind_css}
{past_due_css}
  .delta-up {{ color: #c62828; font-weight: 600; }}
  .delta-down {{ color: #2e7d32; font-weight: 600; }}
  .due-marker {{ position: absolute; width: 10px; height: 10px; background: #d62728; border: 1px solid #fff;
    transform: rotate(45deg); pointer-events: none; z-index: 2; }}
  /* The dates of a story, epic or space in the compared plan: a thin striped line under its bar */
  .baseline-bar {{ position: absolute; height: 4px; background-color: #888; border-radius: 2px; pointer-events: none;
    background-image: repeating-linear-gradient(135deg, rgba(255,255,255,0.6) 0 2px, transparent 2px 5px); }}
  #compare-plan-wrap {{ display: flex; align-items: center; gap: 6px; font-size: 13px; }}
  #compare-plan-wrap[hidden] {{ display: none; }}
  #compare-plan {{ padding: 4px 6px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; background: #fff; }}
  #compare-plan.comparing {{ border-color: #4e79a7; box-shadow: 0 0 0 1px #4e79a7; }}
  #changes-summary[hidden] {{ display: none; }}
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
  /* The colors of the space rows of the chart */
  #trend tr.space-row th {{
    background: #dde4ec; color: #34495e; font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  }}
  #trend tr.whole-plan-row td {{ font-weight: 700; }}
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
  <button id="refresh-button" hidden onclick="refreshChart()" title="Fetch the stories from Jira again and redraw the chart">Refresh from Jira</button>
  <div class="zoom-buttons">
    <button onclick="chooseZoom('day')">Day</button>
    <button onclick="chooseZoom('week')">Week</button>
    <button onclick="chooseZoom('month')">Month</button>
    <button onclick="chooseZoom('quarter')">Quarter</button>
    <button onclick="chooseZoom(null)">Fit All</button>
  </div>
  <label id="show-done-wrap"><input type="checkbox" id="show-done" onchange="applyFilters()"> Show done stories</label>
  <label id="compare-plan-wrap" hidden title="Show the changes since an earlier plan, and its dates as a striped line under the bars">
    Compare with plan
    <select id="compare-plan" onchange="onComparePlanChange()"><option value="">none</option></select>
  </label>
  <div id="search-wrap">
    <input id="search-input" type="search" placeholder="Search stories, for example PROJ-123" oninput="onSearchTyping()" onkeydown="if(event.key==='Escape'){{this.value='';clearTimeout(searchTimer);onSearchInput();}}">
    <span id="search-count"></span>
  </div>
</div>
<div id="refresh-error" hidden role="alert">
  <span id="refresh-error-text"></span>
  <button id="refresh-error-close" title="Close" aria-label="Close"
    onclick="document.getElementById('refresh-error').hidden = true">&times;</button>
</div>
<div id="member-filter"></div>
<div id="refresh-overlay" hidden role="status" aria-live="polite">
  <div class="spinner"></div>
  <div>Fetching the stories from Jira…</div>
</div>
<script>
// The refresh button works only when the page comes from "jira_smurf.py serve", not from a file
if (location.protocol.startsWith("http")) {{
  document.getElementById("refresh-button").hidden = false;
}}
// The error of a failed refresh. For an HTML page, the message is its title and the detail its text.
function refreshError(response, text) {{
  const isHtml = (response.headers.get("Content-Type") || "").includes("html") || /^\\s*</.test(text);
  if (!isHtml) return new Error(text || response.statusText);
  const page = new DOMParser().parseFromString(text, "text/html");
  let body = "";
  if (page.body) {{
    page.body.querySelectorAll("script, style").forEach(element => element.remove());
    // A space after each element keeps the words of two blocks, such as a heading and a paragraph, apart
    page.body.querySelectorAll("*").forEach(element => element.after(" "));
    body = page.body.textContent.replace(/\\s+/g, " ").trim();
  }}
  const title = page.title.trim();
  const error = new Error(title || body || response.status + " " + response.statusText);
  error.detail = title ? body : "";
  return error;
}}
function refreshChart() {{
  const button = document.getElementById("refresh-button");
  const errorLine = document.getElementById("refresh-error");
  const errorText = document.getElementById("refresh-error-text");
  const overlay = document.getElementById("refresh-overlay");
  button.disabled = true;
  button.textContent = "Refreshing…";
  errorLine.hidden = true;
  overlay.hidden = false;
  fetch("refresh", {{method: "POST", headers: {{"X-Jira-Smurf": "refresh"}}}})
    .then(response => response.text().then(text => {{
      if (!response.ok) throw refreshError(response, text);
      location.reload();
    }}))
    .catch(error => {{
      overlay.hidden = true;
      button.disabled = false;
      button.textContent = "Refresh from Jira";
      const message = "Refresh failed: " + error.message.replace(/\\s+/g, " ").trim();
      errorText.textContent = message;
      errorText.title = error.detail ? message + "\\n\\n" + error.detail : message;
      errorLine.hidden = false;
    }});
}}
</script>
<div id="gantt_here"></div>
<div id="changes-summary" hidden></div>
{trend}
<script src="https://cdn.dhtmlx.com/gantt/9.0/dhtmlxgantt.js"></script>
<script>
gantt.plugins({{ marker: true }});
var typeIconImages = {icon_images_json};  // {{icon address: data URI}} of the issue type icons from Jira
var showChanges = false;  // true while a plan is picked in the Compare with plan list
function signed(value, unit) {{
  var number = Math.round(Math.abs(value) * 10) / 10;
  return (value > 0 ? "+" : "\u2212") + number + unit;
}}
// "NEW" for a story that is not in the baseline, or the working days its end date moved.
function movedText(task) {{
  if (!showChanges) return "";
  if (task.is_new) return "<span class='delta-up'>NEW</span>";
  if (!task.moved_days) return "";
  var cls = task.moved_days > 0 ? "delta-up" : "delta-down";
  return "<span class='" + cls + "'>" + signed(task.moved_days, "d") + "</span>";
}}
// The plans before today, newest first: {{date, stories: {{key: [start, end, progress, total hours,
// remaining hours, status]}}}}. End is the day after the last planned day.
var earlierPlans = {plans_json};
var defaultComparePlan = {compare_date_json};  // the plan of --since, used when the address names none
var comparePlan = null;  // the plan picked in the Compare with plan list
function workingDaysBetween(from, to) {{
  var sign = 1;
  if (to < from) {{ var t = from; from = to; to = t; sign = -1; }}
  var days = 0;
  for (var d = new Date(from); d < to; d = gantt.date.add(d, 1, "day")) {{
    if (d.getDay() !== 0 && d.getDay() !== 6 && !holidays.has(dayKey(d))) days++;
  }}
  return sign * days;
}}
function formatHours(hours) {{
  return Math.round(hours * 10) / 10 + "h";
}}
// The latest plan on or before a date, or the oldest plan when there is none
function planOnOrBefore(day) {{
  var plans = earlierPlans.filter(function(plan) {{ return plan.date <= day; }});
  return plans.length ? plans[0] : earlierPlans[earlierPlans.length - 1];
}}
// Mark each task with its changes since the compared plan, as the Moved column, the striped lines,
// the info popup and the summary use them, and fill in the stories that are no longer in the plan.
function applyComparison() {{
  var plan = comparePlan;
  showChanges = !!plan;
  var parse = gantt.date.str_to_date("%Y-%m-%d");
  var today = dayKey(new Date());
  var fields = ["is_new", "base_start", "base_end", "base_open", "base_total", "hours_delta", "moved_days", "late",
                "in_compare"];
  var summary = {{ newStories: [], newHours: 0, grew: [], grewHours: 0, shrank: [], shrankHours: 0,
                   late: [], lateHours: 0, removed: [], removedHours: 0, end: null, remaining: 0 }};
  var removedGroup = null;
  gantt.eachTask(function(task) {{
    fields.forEach(function(field) {{ delete task[field]; }});
    if (task.removed && !task.parent) removedGroup = task;
    if (!plan || task.removed || task.is_epic || task.is_space || !task.story_key) return;
    // A story still in the plan
    summary.remaining += task.hours || 0;
    if (!summary.end || task.end_date > summary.end) summary.end = task.end_date;
    var base = plan.stories[task.story_key];
    if (!base) {{
      task.is_new = true;
      summary.newStories.push(task);
      summary.newHours += task.hours || 0;
      return;
    }}
    task.base_start = base[0];
    task.base_end = base[1];
    task.base_open = base[2] < 1;
    // For a done story, the bar shows the days it was worked on, so this is how much earlier or later
    // it finished than that plan said
    task.moved_days = workingDaysBetween(parse(base[1]), task.end_date);
    if (task.progress >= 1) return;
    if (base[3] !== null && base[3] !== undefined) {{
      var delta = (task.total_hours || 0) - base[3];
      if (Math.abs(delta) >= 0.5) {{
        task.base_total = base[3];
        task.hours_delta = delta;
        if (delta > 0) {{ summary.grew.push(task); summary.grewHours += delta; }}
        else {{ summary.shrank.push(task); summary.shrankHours += delta; }}
      }}
    }}
    if (base[2] < 1 && base[1] <= today) {{
      task.late = true;
      summary.late.push(task);
      summary.lateHours += task.hours || 0;
    }}
  }});
  // The open stories of the compared plan that are no longer in the plan, with their dates in that plan
  if (removedGroup) {{
    var first = null, last = null;
    gantt.eachTask(function(task) {{
      var base = plan && plan.stories[task.story_key];
      if (!base || base[2] >= 1) return;
      task.in_compare = true;
      task.start_date = parse(base[0]);
      task.end_date = parse(base[1]);
      task.status = base[5];
      task.effort = formatHours(base[4]);
      summary.removed.push(task);
      summary.removedHours += base[4];
      if (!first || task.start_date < first) first = task.start_date;
      if (!last || task.end_date > last) last = task.end_date;
    }}, removedGroup.id);
    if (first) {{
      removedGroup.in_compare = true;
      removedGroup.start_date = first;
      removedGroup.end_date = last;
      removedGroup.effort = formatHours(summary.removedHours);
    }}
  }}
  gantt.config.columns = gantt.config.columns.filter(function(column) {{ return column !== movedColumn; }});
  if (plan) gantt.config.columns.push(movedColumn);
  showChangesSummary(plan, summary);
}}
// The dates of each epic and space in the compared plan, from the same stories as its bar: those that the
// filters show. With done stories hidden, the line of an epic so starts where its first open story was
// planned, not where its done stories were. Call it after the filters set the dates of the bars.
function compareGroups() {{
  if (!comparePlan) return;
  var parse = gantt.date.str_to_date("%Y-%m-%d");
  function span(task, children, isShown) {{
    delete task.base_start;
    delete task.base_end;
    delete task.moved_days;
    children.forEach(function(child) {{
      if (!child.base_start || !isShown(child)) return;
      if (!task.base_start || child.base_start < task.base_start) task.base_start = child.base_start;
      if (!task.base_end || child.base_end > task.base_end) task.base_end = child.base_end;
    }});
    if (task.base_end) task.moved_days = workingDaysBetween(parse(task.base_end), task.end_date);
  }}
  function children(task) {{
    return gantt.getChildren(task.id).map(function(id) {{ return gantt.getTask(id); }});
  }}
  // The stories without an epic are a group, not one piece of work, so it gets no bar and no changes
  gantt.eachTask(function(task) {{
    if (task.is_epic && !task.roadmap && !task.no_bar) span(task, children(task), isStoryShown);
  }});
  gantt.eachTask(function(task) {{
    if (task.is_space) span(task, children(task), isTaskShownByFilters);
  }});
}}
// The box under the chart that explains how the plan changed since the compared plan
function showChangesSummary(plan, s) {{
  var box = document.getElementById("changes-summary");
  box.hidden = !plan;
  if (!plan) {{ box.innerHTML = ""; return; }}
  var parse = gantt.date.str_to_date("%Y-%m-%d");
  function escape(text) {{
    return String(text).replace(/[&<>"']/g, function(c) {{
      return {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}}[c];
    }});
  }}
  function storyList(stories) {{
    if (!stories.length) return "";
    return "<details><summary>Show stories</summary><ul>" + stories.map(function(task) {{
      // The text of a task is "KEY: title"
      return "<li><a href='{JIRA_URL}/browse/" + encodeURIComponent(task.story_key) + "' target='_blank'>" +
        escape(task.story_key) + "</a>" + escape(task.text.slice(task.story_key.length)) + "</li>";
    }}).join("") + "</ul></details>";
  }}
  function hours(value) {{
    return value ? signed(value, "h") : "0h";
  }}
  function row(label, stories, value) {{
    var noun = stories.length === 1 ? "story" : "stories";
    return "<li><b>" + stories.length + " " + label.replace("{{noun}}", noun) + "</b>: " + hours(value) +
      storyList(stories) + "</li>";
  }}
  var baseEnd = null, baseRemaining = 0;
  Object.keys(plan.stories).forEach(function(key) {{
    var base = plan.stories[key];
    if (!baseEnd || base[1] > baseEnd) baseEnd = base[1];
    baseRemaining += base[4];
  }});
  var lastDay = gantt.date.date_to_str("%Y-%m-%d");
  var endLine = "";
  if (baseEnd && s.end) {{
    var moved = workingDaysBetween(parse(baseEnd), s.end);
    endLine = "<p>The plan end moved from <b>" + lastDay(gantt.date.add(parse(baseEnd), -1, "day")) + "</b> to <b>" +
      lastDay(gantt.date.add(s.end, -1, "day")) + "</b> (" + (moved ? signed(moved, " working days") : "no change") +
      ").</p>";
  }}
  box.innerHTML = "<h3>Changes since the plan of " + plan.date + "</h3>" + endLine +
    "<p>Remaining work went from " + formatHours(baseRemaining) + " to " + formatHours(s.remaining) + ".</p><ul>" +
    row("new {{noun}}", s.newStories, s.newHours) +
    row("{{noun}} with a larger estimate", s.grew, s.grewHours) +
    row("{{noun}} with a smaller estimate", s.shrank, s.shrankHours) +
    row("{{noun}} no longer in the plan", s.removed, -s.removedHours) +
    "<li><b>" + s.late.length + " " + (s.late.length === 1 ? "story" : "stories") +
    " planned to be done by now but still open</b>: " + formatHours(s.lateHours) + " remaining" +
    storyList(s.late) + "</li></ul>";
}}
// The list of earlier plans. The plan comes from ?compare= in the address, else from --since.
function initComparePlan() {{
  if (!earlierPlans.length) return;
  var select = document.getElementById("compare-plan");
  earlierPlans.forEach(function(plan) {{
    var option = document.createElement("option");
    option.value = plan.date;
    option.textContent = plan.date;
    select.appendChild(option);
  }});
  document.getElementById("compare-plan-wrap").hidden = false;
  var asked = new URLSearchParams(location.search).get("compare");
  if (asked === "none") comparePlan = null;
  else if (asked && /^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(asked)) comparePlan = planOnOrBefore(asked);
  else if (defaultComparePlan) comparePlan = planOnOrBefore(defaultComparePlan);
  select.value = comparePlan ? comparePlan.date : "";
  select.classList.toggle("comparing", !!comparePlan);
  applyComparison();
}}
function onComparePlanChange() {{
  var select = document.getElementById("compare-plan");
  comparePlan = earlierPlans.find(function(plan) {{ return plan.date === select.value; }}) || null;
  select.classList.toggle("comparing", !!comparePlan);
  // Keep the choice in the address, so a reload or a shared link shows the same comparison
  try {{
    var url = new URL(location.href);
    url.searchParams.set("compare", select.value || "none");
    history.replaceState(null, "", url);
  }} catch (e) {{}}
  applyComparison();
  applyFilters();
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
      var icon = image ? "<img class='type-icon" + (task.is_space ? " space-avatar" : "") + "' src='" + image + "' alt=''>" :
        task.icon ? "<span class='type-icon'>" + task.icon + "</span>" : "";
      // The badges come before the name, so a long name does not hide them
      return icon + (task.badges_html || "") +
        "<span>" + task.text + "</span>";
    }} }},
  {{name: "status", label: "Status", align: "left", width: 120, resize: true}},
  {{name: "points", label: "Points", align: "center", width: 70, resize: true,
    template: function(task) {{
      if (!task.points) return "";
      // Orange like the impl badge when the story is longer in implementation than its estimate
      var over = task.implementation_days != null && task.implementation_days > task.estimate_days;
      return "<span class='points-badge" + (over ? " points-over" : "") + "'>" + task.points + "</span>";
    }} }},
  {{name: "sprint", label: "Sprint", align: "center", width: 70, resize: true,
    template: function(task) {{
      return task.sprint ? "<span title='" + task.sprint_name.replace(/'/g, "&#39;") + "'>" + task.sprint + "</span>" : "";
    }} }}
];
// Shown while a plan is compared
var movedColumn = {{name: "moved", label: "Moved", align: "center", width: 70, resize: true, template: movedText}};
gantt.templates.tooltip_text = function(start, end, task) {{
  if (!task.text) return "";
  var fmt = gantt.date.date_to_str("%d %M %Y");
  var text = "<b>" + task.text + "</b><br/>" + fmt(start) + " – " + fmt(gantt.date.add(end, -1, "day"));
  if (task.roadmap) text += "<br/>No stories yet. The bar shows the start and due date of the epic in Jira.";
  if (task.unestimated) {{
    text += task.type === "project" || gantt.hasChild(task.id)
      ? "<br/>Some stories have no estimate in Jira, so the default hours are used for them"
      : "<br/>No estimate in Jira, so the default hours are used";
  }}
  if (task.jira_start_date) {{
    text += "<br/>Start date in Jira: " + fmt(gantt.date.str_to_date("%Y-%m-%d")(task.jira_start_date));
  }}
  if (task.due_date) {{
    var due = gantt.date.str_to_date("%Y-%m-%d")(task.due_date);
    text += "<br/>Due: " + fmt(due) + (end > gantt.date.add(due, 1, "day") ? " (planned to end later)" : "");
  }}
  if (!showChanges) return text;
  var since = comparePlan.date;
  if (task.is_new) text += "<br/>New since the plan of " + since;
  if (task.removed && task.parent) text += "<br/>In the plan of " + since + ", no longer in the plan";
  if (task.base_start) {{
    var parse = gantt.date.str_to_date("%Y-%m-%d");
    var baseEnd = gantt.date.add(parse(task.base_end), -1, "day");
    text += "<br/>Plan of " + since + ": " + fmt(parse(task.base_start)) + " – " + fmt(baseEnd);
    if (task.moved_days) {{
      text += " (<span class='" + (task.moved_days > 0 ? "delta-up" : "delta-down") + "'>" +
        signed(task.moved_days, " working days") + "</span>)";
    }}
    // A story done since that plan: when it finished against that plan, and how long it took against its
    // estimate. Not against the days of that plan, which only has the remaining work of a story in progress.
    if (task.progress >= 1 && task.base_open) {{
      var took = workingDaysBetween(task.start_date, task.end_date);
      var days = function(n) {{ return n === 1 ? "1 working day" : n + " working days"; }};
      text += "<br/>Done " + (task.moved_days < 0 ? days(-task.moved_days) + " earlier than planned"
                              : task.moved_days > 0 ? days(task.moved_days) + " later than planned" : "on the planned day") +
        ". It took " + days(took) + "; its estimate is " + days(task.estimate_days) +
        (task.points ? " (" + task.points + " SP)" : "") + ".";
    }}
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
// Holidays and days off in the date row of the header. At the day and week levels a day shows a flag on a
// holiday, and the initials of each team member who is off that day. A long absence is one bar across its days.
// At the month and quarter levels a week or month shows a flag when it has a holiday and the initials of the
// team members who are off in it. Only the team members that the Team member filter shows are marked.
var holidayDays = new Set({holidays_json});
var daysOff = {days_off_json};
var HOLIDAY_ICON = {holiday_icon_json};  // the mark of a holiday in the date header, or "none"
var offByDay = {{}};  // {{"YYYY-MM-DD": [{{member, run}}]}} for the days off of short absences
var barLanes = 0;
daysOff.forEach(function(member) {{
  member.runs.forEach(function(run) {{
    if (run.lane !== null) {{ barLanes = Math.max(barLanes, run.lane + 1); return; }}
    run.days.forEach(function(day) {{ (offByDay[day] = offByDay[day] || []).push({{ member: member, run: run }}); }});
  }});
}});
var TOP_ROW = 24, BAR_LANE = 11;
// Without holiday flags and days off there is nothing to mark, so the date row has only the date
var MARK_ROW = HOLIDAY_ICON !== "none" || daysOff.length > 0;
var DAY_ROW = MARK_ROW ? 34 + barLanes * BAR_LANE : 26;  // date, marks and the bars of long absences
var SUMMARY_ROW = MARK_ROW ? 34 : 26;
var MAX_AVATARS = 3;
var isoDay = gantt.date.date_to_str("%Y-%m-%d");
var shortDay = gantt.date.date_to_str("%d %M");
var parseDay = gantt.date.str_to_date("%Y-%m-%d");
var HOLIDAY_MARKS = {{
  flag: "<svg class='holiday-flag' viewBox='0 0 12 14' width='11' height='13' aria-hidden='true'>" +
    "<path d='M1.5 1v12' stroke='currentColor' stroke-width='1.5' stroke-linecap='round'/>" +
    "<path d='M2.5 1.5h8l-2 3 2 3h-8z' fill='currentColor'/></svg>",
  star: "<svg class='holiday-flag' viewBox='0 0 14 14' width='12' height='12' aria-hidden='true'>" +
    "<path d='M7 1l1.8 3.9 4.2.5-3.1 2.9.8 4.2L7 10.4l-3.7 2.1.8-4.2L1 5.4l4.2-.5z' fill='currentColor'/></svg>",
  calendar: "<svg class='holiday-flag' viewBox='0 0 12 13' width='11' height='12' aria-hidden='true'>" +
    "<rect x='0.75' y='1.75' width='10.5' height='10.5' rx='1.5' fill='#fff' stroke='currentColor' stroke-width='1.5'/>" +
    "<rect x='0.75' y='1.75' width='10.5' height='3' fill='currentColor'/>" +
    "<rect x='4.5' y='7' width='3' height='3' fill='currentColor'/></svg>",
  dot: "<svg class='holiday-flag' viewBox='0 0 10 10' width='8' height='8' aria-hidden='true'>" +
    "<circle cx='5' cy='5' r='4' fill='currentColor'/></svg>",
  text: "<span class='holiday-flag holiday-text'>HOL</span>"
}};
var HOLIDAY_MARK = HOLIDAY_MARKS[HOLIDAY_ICON] || "";
function escapeText(text) {{
  return String(text).replace(/[&<>"']/g, function(c) {{
    return {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}}[c];
  }});
}}
function isMemberMarked(member) {{
  return Object.keys(selectedMembers).length === 0 || !!selectedMembers[member.name];
}}
function isWeekend(date) {{
  return date.getDay() === 0 || date.getDay() === 6;
}}
function avatarHtml(member) {{
  return "<span class='off-avatar' style='background:" + member.color + "'>" + escapeText(member.initials) + "</span>";
}}
// The avatars of the members, at most MAX_AVATARS and then "+N"
function avatarStack(members) {{
  var shown = members.length > MAX_AVATARS + 1 ? members.slice(0, MAX_AVATARS) : members;
  var html = shown.map(avatarHtml).join("");
  if (shown.length < members.length) html += "<span class='off-avatar off-more'>+" + (members.length - shown.length) + "</span>";
  return html;
}}
// The cell of the date row: the date, a line with the marks and, under it, the bars. tip is the text of the popup.
function markedCell(label, marks, tip, bars) {{
  if (!MARK_ROW) return label;
  return "<div class='marked-cell'><div class='marked-label'>" + label + "</div>" +
    "<div class='marked-marks'" + (tip ? " data-mark-tip='" + escapeText(tip) + "'" : "") + ">" + marks + "</div>" +
    (bars || "") + "</div>";
}}
function runText(member, run) {{
  var n = run.days.length;
  return member.name + ": off " + (run.first === run.last ? shortDay(parseDay(run.first)) :
    shortDay(parseDay(run.first)) + " to " + shortDay(parseDay(run.last))) + " (" + n + " working day" + (n === 1 ? "" : "s") + ")";
}}
// The bars of the long absences that go over this day. The initials are on the first day and on each Monday.
function dayBars(date, key) {{
  var html = "";
  daysOff.forEach(function(member) {{
    if (!isMemberMarked(member)) return;
    member.runs.forEach(function(run) {{
      if (run.lane === null || key < run.first || key > run.last) return;
      var cls = "off-bar" + (key === run.first ? " bar-first" : "") + (key === run.last ? " bar-last" : "");
      var label = key === run.first || date.getDay() === 1 ? escapeText(member.initials) : "";
      html += "<div class='" + cls + "' style='background:" + member.color + ";bottom:" + (2 + run.lane * BAR_LANE) + "px'" +
        " data-mark-tip='" + escapeText(runText(member, run)) + "'>" + label + "</div>";
    }});
  }});
  return html;
}}
// A weekend or a holiday in the date row is grey, as in the timeline under it
function holidayCellClass(date) {{
  return isWeekend(date) || holidayDays.has(isoDay(date)) ? "weekend" : "";
}}
function dayCell(format) {{
  var label = gantt.date.date_to_str(format);
  return function(date) {{
    var key = isoDay(date);
    var marks = "", tip = "";
    if (holidayDays.has(key) && !isWeekend(date)) {{
      if (HOLIDAY_ICON !== "none") {{
        marks = HOLIDAY_MARK;
        tip = "Holiday";
      }}
    }} else {{
      var members = (offByDay[key] || []).filter(function(off) {{ return isMemberMarked(off.member); }});
      marks = avatarStack(members.map(function(off) {{ return off.member; }}));
      tip = members.map(function(off) {{ return runText(off.member, off.run); }}).join("\\n");
    }}
    return markedCell(label(date), marks, tip, dayBars(date, key));
  }};
}}
// The cell of a week or month: a flag with the number of holidays, and the members with days off in it
function summaryCell(format, unit) {{
  var label = gantt.date.date_to_str(format);
  return function(date) {{
    var end = gantt.date.add(date, 1, unit);
    var holidays = [], offDays = {{}};
    for (var d = new Date(date); d < end; d = gantt.date.add(d, 1, "day")) {{
      var key = isoDay(d);
      if (holidayDays.has(key) && !isWeekend(d)) holidays.push(shortDay(d));
    }}
    // Per member the number of days off in this week or month, and the periods they are in, cut to it
    var from = isoDay(date), to = isoDay(gantt.date.add(end, -1, "day"));
    var offPeriods = {{}};
    daysOff.forEach(function(member) {{
      if (!isMemberMarked(member)) return;
      member.runs.forEach(function(run) {{
        var days = run.days.filter(function(day) {{ return day >= from && day <= to; }});
        if (!days.length) return;
        offDays[member.name] = (offDays[member.name] || 0) + days.length;
        var first = shortDay(parseDay(days[0])), last = shortDay(parseDay(days[days.length - 1]));
        (offPeriods[member.name] = offPeriods[member.name] || []).push(first === last ? first : first + " to " + last);
      }});
    }});
    var members = daysOff.filter(function(member) {{ return offDays[member.name]; }});
    var marks = "", tips = [];
    if (holidays.length && HOLIDAY_ICON !== "none") {{
      marks += HOLIDAY_MARK + (holidays.length > 1 ? "<span class='holiday-count'>" + holidays.length + "</span>" : "");
      tips.push((holidays.length > 1 ? "Holidays: " : "Holiday: ") + holidays.join(", "));
    }}
    marks += avatarStack(members);
    members.forEach(function(member) {{
      var n = offDays[member.name];
      tips.push(member.name + ": " + n + " day" + (n === 1 ? "" : "s") + " off (" + offPeriods[member.name].join(", ") + ")");
    }});
    return markedCell(label(date), marks, tips.join("\\n"));
  }};
}}
gantt.ext.zoom.init({{
  levels: [
    {{
      name: "day",
      scale_height: DAY_ROW,
      min_column_width: 80,
      scales: [{{ unit: "day", step: 1, format: dayCell("%d %M"), sticky: false, css: holidayCellClass }}]
    }},
    {{
      name: "week",
      scale_height: TOP_ROW + DAY_ROW,
      min_column_width: 50,
      scales: [
        {{ unit: "week", step: 1, format: "Week %W" }},
        {{ unit: "day", step: 1, format: dayCell("%j %D"), sticky: false, css: holidayCellClass }}
      ]
    }},
    {{
      name: "month",
      scale_height: TOP_ROW + SUMMARY_ROW,
      min_column_width: 120,
      scales: [
        {{ unit: "month", step: 1, format: "%F %Y" }},
        {{ unit: "week", step: 1, format: summaryCell("W%W", "week"), sticky: false }}
      ]
    }},
    {{
      name: "quarter",
      scale_height: TOP_ROW + SUMMARY_ROW,
      min_column_width: 90,
      scales: [
        {{ unit: "year", step: 1, format: "%Y" }},
        {{ unit: "month", step: 1, format: summaryCell("%M", "month"), sticky: false }}
      ]
    }}
  ]
}});
gantt.ext.zoom.setLevel("week"); // initial level before fit
// Open or close every epic, so the chart shows all stories or only the epics.
// The spaces stay open, so their epics stay in sight.
function setAllOpen(open) {{
  gantt.batchUpdate(function() {{
    gantt.eachTask(function(task) {{
      if (!gantt.hasChild(task.id)) return;
      if (open || task.is_space) gantt.open(task.id);
      else gantt.close(task.id);
    }});
  }});
}}
function setZoom(level) {{
  gantt.ext.zoom.setLevel(level);
  makeLabelRoom(false);
  // Fit All picks a level itself, but its own button is the one to mark
  var active = chosenZoom || "fit all";
  document.querySelectorAll("#toolbar button").forEach(function(b) {{
    b.classList.toggle("active", b.textContent.toLowerCase() === active);
  }});
}}
var LABEL_ROOM = 300;  // pixels after the last bar, for the text next to it such as the assignee and the end date
// Sets the end of the timeline LABEL_ROOM pixels after the last shown bar, at the current scale. With onlyGrow,
// the end does not move earlier. Returns true when the end moved.
function makeLabelRoom(onlyGrow) {{
  var project = shownDates();
  if (!project.end_date) return false;
  var start = gantt.config.start_date;
  var barsMs = gantt.date.add(project.end_date, 1, "day") - start;
  var scale = gantt.getScale();
  var columnMs = gantt.date.add(start, scale.step || 1, scale.unit) - start;
  var pixelsPerMs = gantt.config.min_column_width / columnMs;
  var area = gantt.$task.offsetWidth;
  var totalMs = barsMs + LABEL_ROOM / pixelsPerMs;
  // A timeline narrower than the chart gets wider columns, so keep LABEL_ROOM pixels of the chart width free
  if (totalMs * pixelsPerMs < area && area > LABEL_ROOM) totalMs = barsMs * area / (area - LABEL_ROOM);
  var end = new Date(start.getTime() + totalMs);
  if (onlyGrow && end <= gantt.config.end_date) return false;
  if (Math.abs(end - gantt.config.end_date) < 3600 * 1000) return false;
  gantt.config.end_date = end;
  gantt.render();
  // dhtmlx rounds the timeline out to whole columns, which can make stretched columns narrower than computed
  var barsEnd = new Date(start.getTime() + barsMs);
  for (var i = 0; i < 2; i++) {{
    var missing = LABEL_ROOM - (gantt.getScale().full_width - gantt.posFromDate(barsEnd));
    if (missing < 1) break;
    var msPerPixel = (gantt.config.end_date - start) / gantt.posFromDate(gantt.config.end_date);
    gantt.config.end_date = new Date(gantt.config.end_date.getTime() + missing * msPerPixel);
    gantt.render();
  }}
  return true;
}}
var chosenZoom = null;  // the level picked with a zoom button, or null to fit the chart to the shown stories
function chooseZoom(level) {{
  chosenZoom = level;
  if (level) setZoom(level);
  else zoomToFit();
}}
var hideDone = false;
var selectedMembers = {{}};  // {{assignee: true}} for the team members whose stories are shown; empty shows all
var memberCounts = {{}};  // {{assignee: count element}} in the badges
var epicDates = {{}};  // {{epicId: {{start_date, end_date}}}} with the dates from all stories
var epicTotals = {{}};  // {{epicId: {{effort, points}}}} with the totals of all stories
function isFiltering() {{
  return hideDone || Object.keys(selectedMembers).length > 0;
}}
function isStoryShown(task) {{
  if (hideDone && task.progress >= 1) return false;
  return Object.keys(selectedMembers).length === 0 || !!selectedMembers[task.assignee];
}}
function isTaskShown(task) {{
  if (task.removed) return showChanges && !!task.in_compare;
  // A search shows its matches and their epics, also when a filter hides them
  if (searchVisibleIds[task.id]) return true;
  return isTaskShownByFilters(task);
}}
// Whether the filters show a story or epic that is still in the plan, apart from a search.
function isTaskShownByFilters(task) {{
  if (!isFiltering()) return true;
  // An epic without stories has no team member
  if (task.roadmap) return Object.keys(selectedMembers).length === 0;
  // A space is shown while one of its epics is
  if (task.is_space) return gantt.getChildren(task.id).some(function(id) {{
    return isTaskShownByFilters(gantt.getTask(id));
  }});
  if (gantt.hasChild(task.id)) return !!shownStoryDates(task.id);
  return isStoryShown(task);
}}
// Earliest start and latest end of the stories of an epic that pass the filters, or null.
function shownStoryDates(epicId) {{
  var start = null, end = null;
  gantt.eachTask(function(child) {{
    if (child.is_epic || !isStoryShown(child)) return;
    if (!start || child.start_date < start) start = child.start_date;
    if (!end || child.end_date > end) end = child.end_date;
  }}, epicId);
  return start ? {{ start_date: start, end_date: end }} : null;
}}
// Earliest start and latest end of the epics of a space that pass the filters, or null.
function shownEpicDates(spaceId) {{
  var start = null, end = null;
  gantt.getChildren(spaceId).forEach(function(id) {{
    var epic = gantt.getTask(id);
    if (!isTaskShownByFilters(epic)) return;
    if (!start || epic.start_date < start) start = epic.start_date;
    if (!end || epic.end_date > end) end = epic.end_date;
  }});
  return start ? {{ start_date: start, end_date: end }} : null;
}}
gantt.attachEvent("onBeforeTaskDisplay", function(id, task) {{
  return isTaskShown(task);
}});
// A click on an epic in the Story column opens or closes it, instead of selecting it and scrolling to its bar.
// The arrow in front of the epic does this already, so a click on the arrow is left to dhtmlx.
gantt.attachEvent("onTaskClick", function(id, e) {{
  var target = e && e.target;
  if (!target || !target.closest(".gantt_grid_data") || target.closest(".gantt_tree_icon")) return true;
  if (!gantt.hasChild(id)) return true;
  if (gantt.getTask(id).$open) gantt.close(id);
  else gantt.open(id);
  return false;
}});
// Remaining hours and points of the stories of an epic or space that pass the filters.
// The hours get a "?" when one of those stories has no estimate.
function shownStoryTotals(epicId) {{
  var hours = 0, points = 0, guess = false;
  gantt.eachTask(function(child) {{
    if (child.is_epic || !isStoryShown(child)) return;
    hours += child.hours || 0;
    points += child.points_value || 0;
    if (child.unestimated) guess = true;
  }}, epicId);
  return {{
    effort: Math.round(hours * 10) / 10 + "h" + (guess ? "?" : ""),
    points: points ? String(Math.round(points * 100) / 100) : "",
  }};
}}
// Show in each badge the number of stories of that member that pass the done filter.
function updateMemberCounts() {{
  var counts = {{}};
  gantt.eachTask(function(task) {{
    if (!task.assignee || (hideDone && task.progress >= 1)) return;
    counts[task.assignee] = (counts[task.assignee] || 0) + 1;
  }});
  Object.keys(memberCounts).forEach(function(name) {{
    memberCounts[name].textContent = counts[name] || 0;
  }});
}}
// One badge per assignee of the stories, "Unassigned" last. A click selects or deselects that member.
function buildMemberFilter() {{
  var counts = {{}};
  gantt.eachTask(function(task) {{
    if (task.assignee) counts[task.assignee] = (counts[task.assignee] || 0) + 1;
  }});
  var names = Object.keys(counts).sort(function(a, b) {{
    if (a === "Unassigned" || b === "Unassigned") return a === "Unassigned" ? 1 : -1;
    return a.localeCompare(b);
  }});
  var bar = document.getElementById("member-filter");
  var label = document.createElement("span");
  label.className = "filter-label";
  label.textContent = "Team member:";
  bar.appendChild(label);
  names.forEach(function(name) {{
    var badge = document.createElement("button");
    badge.className = "member-badge";
    badge.textContent = name;
    var count = document.createElement("span");
    count.className = "count";
    memberCounts[name] = count;
    badge.appendChild(count);
    badge.onclick = function() {{
      if (selectedMembers[name]) delete selectedMembers[name]; else selectedMembers[name] = true;
      badge.classList.toggle("active", !!selectedMembers[name]);
      applyFilters();
    }};
    bar.appendChild(badge);
  }});
  var clear = document.createElement("button");
  clear.id = "member-clear";
  clear.textContent = "Show all";
  clear.onclick = function() {{
    selectedMembers = {{}};
    bar.querySelectorAll(".member-badge").forEach(function(b) {{ b.classList.remove("active"); }});
    applyFilters();
  }};
  bar.appendChild(clear);
}}
function applyFilters() {{
  hideDone = !document.getElementById("show-done").checked;
  document.getElementById("member-clear").style.display = Object.keys(selectedMembers).length ? "" : "none";
  // The epics first, as a space takes its dates from them
  gantt.eachTask(function(task) {{
    if (!task.is_epic || task.removed || task.roadmap) return;
    if (!epicDates[task.id]) epicDates[task.id] = {{ start_date: task.start_date, end_date: task.end_date }};
    var dates = isFiltering() ? shownStoryDates(task.id) : null;
    // A "project" task takes its dates from all its children, so use a plain task while stories are hidden.
    task.type = dates ? "task" : "project";
    task.start_date = (dates || epicDates[task.id]).start_date;
    task.end_date = (dates || epicDates[task.id]).end_date;
    // The totals of the epic row count only the stories that are shown
    if (!epicTotals[task.id]) epicTotals[task.id] = {{ effort: task.effort, points: task.points }};
    var totals = isFiltering() ? shownStoryTotals(task.id) : epicTotals[task.id];
    task.effort = totals.effort;
    task.points = totals.points;
  }});
  gantt.eachTask(function(task) {{
    if (!task.is_space) return;
    if (!epicDates[task.id]) epicDates[task.id] = {{ start_date: task.start_date, end_date: task.end_date }};
    var dates = isFiltering() ? shownEpicDates(task.id) : null;
    task.type = dates ? "task" : "project";
    task.start_date = (dates || epicDates[task.id]).start_date;
    task.end_date = (dates || epicDates[task.id]).end_date;
    if (!epicTotals[task.id]) epicTotals[task.id] = {{ effort: task.effort, points: task.points }};
    var totals = isFiltering() ? shownStoryTotals(task.id) : epicTotals[task.id];
    task.effort = totals.effort;
    task.points = totals.points;
  }});
  compareGroups();
  updateMemberCounts();
  showShownDates();
}}
// Keep the timeline at the chosen zoom level, or fit it to the shown stories when no level is chosen
function showShownDates() {{
  if (!chosenZoom) {{
    zoomToFit();
    gantt.render();
    return;
  }}
  // Keep the timeline and the day at its left edge. The timeline only grows, so stories that
  // the filter shows again, such as done stories, are on it.
  var leftDate = gantt.dateFromPos(gantt.getScrollState().x);
  growTimeline();
  gantt.render();
  gantt.scrollTo(gantt.posFromDate(leftDate), null);
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
  // The bars must fit in the space left after the text next to them
  var barsWidth = areaWidth - LABEL_ROOM;
  var levels = ["day", "week", "month", "quarter"];
  // Pick the most detailed level that fits, trying "day" first
  var best = "quarter";
  for (var i = 0; i < levels.length; i++) {{
    gantt.ext.zoom.setLevel(levels[i]);
    var cols = gantt.getScale().count;
    var minWidth = gantt.config.min_column_width || 50;
    if (cols * minWidth <= barsWidth) {{
      best = levels[i];
      break;
    }}
  }}
  setZoom(best);
  // A long range does not fit even at the widest level, so make its columns narrower
  for (var round = 0; round < 3; round++) {{
    var count = gantt.getScale().count;
    if (count * gantt.config.min_column_width <= areaWidth) break;
    gantt.config.min_column_width = Math.max(20, Math.floor(areaWidth / count));
    gantt.render();
    makeLabelRoom(false);
  }}
}}
var searchTerm = "";
var searchMatchIds = {{}};      // {{taskId: true}} for tasks whose own text matches
var searchVisibleIds = {{}};    // {{taskId: true}} for matches + their ancestors (kept un-dimmed)
function endsAfterDueDate(task) {{
  if (!task.due_date) return false;
  var dayAfterDue = gantt.date.add(gantt.date.str_to_date("%Y-%m-%d")(task.due_date), 1, "day");
  return task.end_date > dayAfterDue;
}}
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
  // Red for ending after the due date wins over yellow for being behind the baseline
  var pastDueDate = !task.removed && task.progress < 1 && endsAfterDueDate(task);
  var base = (task.progress >= 1 ? "clickable-task task-done" : "clickable-task") +
    (pastDueDate ? " due-late" : changeBarClass(task)) +
    (task.roadmap ? " roadmap-epic" : "") + (task.no_bar ? " no-bar" : "") + (task.is_space ? " space-bar" : "");
  if (!searchTerm || searchVisibleIds[task.id]) return base;
  return base + " search-dim";
}};
var statusClasses = {status_classes_json};  // {{lowercase status: CSS class}} for the row background
function statusRowClass(task) {{
  return statusClasses[(task.status || "").toLowerCase()] || "";
}}
function epicRowClass(task) {{
  // Not task.type: the filters turn an epic into a plain task while some of its stories are hidden
  return task.is_epic ? "epic-row" : task.is_space ? "space-row" : "";
}}
gantt.templates.task_row_class = function(start, end, task) {{
  return joinClasses(epicRowClass(task), statusRowClass(task), changeRowClass(task),
    searchTerm && searchMatchIds[task.id] ? "search-match-row" : "");
}};
gantt.templates.grid_row_class = function(start, end, task) {{
  var base = joinClasses(epicRowClass(task), task.progress >= 1 ? "task-done-row" : "",
    statusRowClass(task), changeRowClass(task), task.stripe_class || "");
  if (!searchTerm) return base;
  if (searchMatchIds[task.id]) return (base + " search-match-row").trim();
  if (searchVisibleIds[task.id]) return base;
  return (base + " search-dim-row").trim();
}};
function onSearchInput() {{
  var raw = document.getElementById("search-input").value.trim().toLowerCase();
  searchTerm = raw;
  document.getElementById("search-input").classList.toggle("searching", !!raw);
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
  var hiddenCount = 0;
  var opened = {{}};
  // Each gantt.open() redraws the chart, so open the epics of all matches in one batch
  gantt.batchUpdate(function() {{
    gantt.eachTask(function(task) {{
      if (task.$search_text === undefined) {{
        task.$search_text = ((task.text || "") + " " + (task.story_key || "") + " " +
          (task.badges_text || "")).toLowerCase();
      }}
      if (task.$search_text.indexOf(raw) === -1) return;
      searchMatchIds[task.id] = true;
      searchVisibleIds[task.id] = true;
      matchCount++;
      if (!task.removed && !isTaskShownByFilters(task)) hiddenCount++;
      if (firstMatchId === null) firstMatchId = task.id;
      var p = gantt.getParent(task.id);
      while (p && p !== gantt.config.root_id && !opened[p]) {{
        opened[p] = true;
        searchVisibleIds[p] = true;
        gantt.open(p);
        p = gantt.getParent(p);
      }}
    }});
  }});
  countEl.textContent = (matchCount === 1 ? "1 match" : matchCount + " matches") +
    (hiddenCount ? " (" + hiddenCount + " hidden by filters)" : "");
  gantt.refreshData();
  // A match that a filter hides can be outside the timeline, for example a done story
  if (hiddenCount && growTimeline()) gantt.render();
  if (firstMatchId !== null) gantt.showTask(firstMatchId);
}}
// Make the timeline as wide as the shown stories, without making it smaller. Return true when it grew.
function growTimeline() {{
  var project = shownDates();
  var grown = false;
  if (project.start_date && project.start_date < gantt.config.start_date) {{
    gantt.config.start_date = project.start_date;
    grown = true;
  }}
  if (project.end_date) {{
    var end = gantt.date.add(project.end_date, 1, "day");
    if (end > gantt.config.end_date) {{
      gantt.config.end_date = end;
      grown = true;
    }}
  }}
  if (grown) gantt.render();
  if (makeLabelRoom(true)) grown = true;
  return grown;
}}
// Search when the typing pauses, not on every key
var searchTimer = null;
function onSearchTyping() {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(onSearchInput, 150);
}}
gantt.templates.task_text = function(start, end, task) {{
  if (task.removed || task.roadmap) return task.text;
  return task.text + " " + Math.round(task.progress * 100) + "%";
}};
gantt.templates.rightside_text = function(start, end, task) {{
  var parts = [];
  if (task.assignee) {{
    var name = task.assignee;
    if (task.auto_assigned) {{
      name += " <span class='auto-badge' title='Not assigned in Jira: the planner picked the team member who is free first'>auto</span>";
    }}
    parts.push(name);
  }}
  if (task.age_days !== null && task.age_days !== undefined) {{
    var created = gantt.date.date_to_str("%d %M %Y")(gantt.date.str_to_date("%Y-%m-%d")(task.created));
    var days = task.age_days === 1 ? "1 day" : task.age_days + " days";
    parts[parts.length - 1] = (parts[parts.length - 1] || "") +
      " <span class='age-badge' title='Created " + created + ", " + days + " ago'>" + task.age_days + "d</span>";
  }}
  if (task.implementation_days !== null && task.implementation_days !== undefined) {{
    var moved = gantt.date.date_to_str("%d %M %Y")(gantt.date.str_to_date("%Y-%m-%d")(task.implementation_date));
    var plural = function(n) {{ return n === 1 ? "1 working day" : n + " working days"; }};
    var diff = Math.round((task.implementation_days - task.estimate_days) * 10) / 10;
    var verdict = diff > 0 ? plural(diff) + " over the estimate"
                : diff < 0 ? plural(-diff) + " left in the estimate" : "exactly the estimate";
    var title = "Started in " + task.implementation_status + " on " + moved + ". " +
      plural(task.implementation_days) + " in " + task.implementation_status + " or a later status" +
      (task.implementation_parked ? ", not counting the days back in an earlier status" : "") +
      ". Estimate: " + plural(task.estimate_days) +
      (task.points ? " (" + task.points + " SP)" : "") + ", so " + verdict + ".";
    parts[parts.length - 1] = (parts[parts.length - 1] || "") +
      " <span class='impl-badge" + (diff > 0 ? " impl-over" : "") + "' title='" + title + "'>impl " +
      task.implementation_days + "/" + task.estimate_days + "d</span>";
  }}
  // end is the day after the last day of the bar, as in the info popup
  parts.push(gantt.date.date_to_str("%d %M")(gantt.date.add(end, -1, "day")));
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
// Thin striped lines at the dates of the compared plan, under the bars, in the dark shade of the bar color.
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
    if (!task.base_start || task.no_bar || !gantt.isTaskVisible(task.id)) return;
    var pos = gantt.getTaskPosition(task, parse(task.base_start), parse(task.base_end));
    var bar = document.createElement("div");
    bar.className = "baseline-bar";
    // The same shade as the done part of a bar: the bar color under 30% black
    if (task.color) bar.style.backgroundColor = "color-mix(in srgb, " + task.color + " 70%, black)";
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
    // The ruler scrolls with the timeline, so its position counts from the start of the timeline, not the visible edge
    ruler.style.left = (e.clientX - rect.left + taskArea.scrollLeft) + "px";
    ruler.style.display = "block";
  }});
  taskArea.addEventListener("mouseleave", function() {{
    ruler.style.display = "none";
  }});
}})();
// The info popup of a story or epic. It shows over a bar and over the title in the Story column, but not
// over the text next to a bar or over a badge, which have their own tooltips. It is placed under the row
// and follows the mouse along the row.
(function() {{
  var container = document.getElementById("gantt_here");
  var popup = document.createElement("div");
  popup.id = "task-popup";
  popup.className = "gantt_tooltip";
  document.body.appendChild(popup);
  var shownId = null;
  function hide() {{
    popup.classList.remove("shown");
    shownId = null;
  }}
  // A mark of a holiday or of days off in the date header: its text, under the header
  function showMark(mark, e) {{
    var tip = mark.getAttribute("data-mark-tip");
    if (shownId !== tip) {{
      popup.innerHTML = "<div>" + escapeText(tip).replace(/\\n/g, "<br/>") + "</div>";
      popup.classList.add("shown");
      shownId = tip;
    }}
    var scaleBox = gantt.$task_scale.getBoundingClientRect();
    var left = Math.min(e.clientX + 12, window.innerWidth - popup.offsetWidth - 8);
    popup.style.left = Math.max(8, left) + "px";
    popup.style.top = scaleBox.bottom + 4 + "px";
  }}
  container.addEventListener("mousemove", function(e) {{
    var mark = e.target.closest("[data-mark-tip]");
    if (mark && mark.getAttribute("data-mark-tip")) return showMark(mark, e);
    var spot = e.target.closest(".gantt_task_line, .gantt_grid_data .gantt_cell[data-column-name='text']");
    if (!spot || e.target.closest(".gantt_side_content, .field-badge, .tree-buttons")) return hide();
    var id = spot.closest("[data-task-id]").getAttribute("data-task-id");
    if (!gantt.isTaskExists(id)) return hide();
    if (id !== shownId) {{
      var task = gantt.getTask(id);
      var html = gantt.templates.tooltip_text(task.start_date, task.end_date, task);
      if (!html) return hide();
      // One block, so that the column layout of gantt_tooltip does not turn each <br/> into a line of its own
      popup.innerHTML = "<div>" + html + "</div>";
      popup.classList.add("shown");
      shownId = id;
    }}
    var row = container.querySelector('.gantt_row[data-task-id="' + id + '"]') || spot;
    var rowBox = row.getBoundingClientRect();
    var gap = 4;
    var top = rowBox.bottom + gap;
    // Above the row when there is no room under it
    if (top + popup.offsetHeight > window.innerHeight) top = rowBox.top - gap - popup.offsetHeight;
    var left = Math.min(e.clientX + 12, window.innerWidth - popup.offsetWidth - 8);
    popup.style.left = Math.max(8, left) + "px";
    popup.style.top = top + "px";
  }});
  container.addEventListener("mouseleave", hide);
  gantt.attachEvent("onGanttScroll", hide);
}})();
// Mark both halves of the row under the mouse, the grid row and the timeline row, with row-hover
(function() {{
  var container = document.getElementById("gantt_here");
  var hoverId = null;
  function markRow(id) {{
    hoverId = id;
    container.querySelectorAll(".row-hover").forEach(function(el) {{ el.classList.remove("row-hover"); }});
    if (id === null) return;
    container.querySelectorAll('.gantt_row[data-task-id="' + id + '"], .gantt_task_row[data-task-id="' + id + '"]')
      .forEach(function(el) {{ el.classList.add("row-hover"); }});
  }}
  container.addEventListener("mouseover", function(e) {{
    var el = e.target.closest("[data-task-id]");
    var id = el ? el.getAttribute("data-task-id") : null;
    if (id !== hoverId) markRow(id);
  }});
  container.addEventListener("mouseleave", function() {{ markRow(null); }});
  // A redraw makes new row elements, so mark the row under the mouse again
  gantt.attachEvent("onDataRender", function() {{ if (hoverId !== null) markRow(hoverId); }});
}})();
gantt.parse({{ data: {tasks_json}, links: {links_json} }});
initComparePlan();
buildMemberFilter();
applyFilters();
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
  // The labels next to a bar are inside its element, but only the bar itself opens the story
  if (!bar || e.target.closest(".gantt_side_content")) return;
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

    # The Jira field list gives the id of the Start date field: an open story or epic with a Start date
    # is not planned before it. It also gives the names of the badge fields.
    jira_fields = fetch_jira_fields()
    start_date_field = find_start_date_field(jira_fields)

    # Fetch the stories of the epics, the single stories and the sprint stories, PARALLEL_CALLS calls
    # at a time. The results are added below in this order, as when they were fetched one by one.
    include_sprints = config.include_open_sprints or config.include_future_sprints
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        if config.epics:
            print(f"Fetching the stories of {len(config.epics)} "
                  f"{'epic' if len(config.epics) == 1 else 'epics'}...", file=sys.stderr)
        epic_results = pool.map(lambda key: fetch_stories_for_epic(key, start_date_field), config.epics)
        if config.stories:
            print(f"Fetching {len(config.stories)} individual stories...", file=sys.stderr)
        individual_result = pool.submit(fetch_stories_by_keys, config.stories,
                                                start_date_field=start_date_field) if config.stories else None
        if include_sprints:
            print("Fetching sprint stories...", file=sys.stderr)
        sprint_result = (pool.submit(fetch_sprint_stories, config.include_open_sprints,
                                     config.include_future_sprints, start_date_field)
                         if include_sprints else None)
        epic_results = list(epic_results)

    # Expand epics into stories
    ordered_keys = []
    story_data = {}
    empty_epics = []
    for epic_key, epic_issues in zip(config.epics, epic_results):
        if not epic_issues:
            empty_epics.append(epic_key)
            continue
        print(f"  Found {len(epic_issues)} stories in {epic_key}", file=sys.stderr)
        for issue in epic_issues:
            key = issue["key"]
            if key not in story_data:
                ordered_keys.append(key)
                story_data[key] = issue

    # Add individual stories
    if individual_result:
        individual = individual_result.result()
        for key in config.stories:
            if key in individual and key not in story_data:
                ordered_keys.append(key)
                story_data[key] = individual[key]

    # Add stories in open and future sprints
    if sprint_result:
        added = 0
        for issue in sprint_result.result():
            key = issue["key"]
            if key not in story_data:
                ordered_keys.append(key)
                story_data[key] = issue
                added += 1
        print(f"  Added {added} stories from sprints", file=sys.stderr)

    # Leave out the stories of the exclude list
    if config.exclude:
        excluded = [key for key in ordered_keys if is_excluded(key, config.exclude)]
        for key in excluded:
            ordered_keys.remove(key)
            del story_data[key]
        if excluded:
            print(f"Excluded {len(excluded)} {'story' if len(excluded) == 1 else 'stories'}: "
                  f"{', '.join(excluded)}", file=sys.stderr)

    # Fetch any dependency targets not already in the scheduled set
    dep_keys_needed = set()
    for deps in config.dependencies.values():
        for dep_key in deps:
            if dep_key not in story_data and not is_excluded(dep_key, config.exclude):
                dep_keys_needed.add(dep_key)
    if dep_keys_needed:
        print(f"Fetching {len(dep_keys_needed)} dependency stories...", file=sys.stderr)
        dep_issues = fetch_stories_by_keys(list(dep_keys_needed), start_date_field=start_date_field)
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

    # Add the dependencies from Jira links to those of the configuration file
    if config.dependency_links:
        linked = link_dependencies(list(story_data.values()), config.dependency_links)
        count = sum(len(firsts) for firsts in linked.values())
        if count:
            print(f"Found {count} {'dependency' if count == 1 else 'dependencies'} in Jira links", file=sys.stderr)
        for key, firsts in linked.items():
            current = config.dependencies.get(key, [])
            config.dependencies[key] = current + sorted(firsts - set(current))

    # A story does not wait for an excluded story
    if config.exclude:
        config.dependencies = {key: [dep for dep in deps if not is_excluded(dep, config.exclude)]
                               for key, deps in config.dependencies.items()}

    # Resolve epics and attach to issue dicts
    epic_cache = {}
    for issue in story_data.values():
        issue["_epic"] = resolve_epic_for_issue(issue, epic_cache)
        parent = issue["fields"].get("parent")
        issue["_epic_key"] = (parent.get("key", "") if parent
                              else issue["fields"].get("customfield_10014", ""))

    # Read the start and due dates of the epics. An epic without stories is shown with these dates.
    epic_keys = list(dict.fromkeys(list(config.epics) + [issue["_epic_key"] for issue in story_data.values()
                                                         if issue.get("_epic_key")]))
    epics_info = {}
    if epic_keys:
        print(f"Fetching the dates of {len(epic_keys)} epics...", file=sys.stderr)
        field_names = {field["id"]: field.get("name", field["id"]) for field in jira_fields if field.get("id")}
        for badge in config.badges:
            if jira_fields and badge["field"] not in field_names:
                print(f"Warning: badge field {badge['field']} is not a field in Jira, "
                      f"so no epic gets this badge", file=sys.stderr)
        epics_info = fetch_epics(epic_keys, start_date_field, config.badges, field_names)

    # An open story is not planned before its Start date in Jira, or without one, that of its epic
    for issue in story_data.values():
        story_start = (issue["fields"].get(start_date_field) or "")[:10] if start_date_field else ""
        epic_start = epics_info.get(issue.get("_epic_key", ""), {}).get("start", "")
        issue["_start_date"] = story_start or epic_start

    # The status history is needed for the done stories, and for the open stories that are at the
    # implementation status or a later one: only those can have spent time there
    def progress_of(key):
        return config.statuses.get(story_data[key]["fields"].get("status", {}).get("name", "").lower(), 0)

    done_keys = [key for key in ordered_keys if progress_of(key) >= 1.0]
    open_keys = []
    if config.implementation_status:
        impl_progress = config.statuses[config.implementation_status.lower()]
        open_keys = [key for key in ordered_keys if impl_progress <= progress_of(key) < 1.0]
    status_changes = {}
    if done_keys or open_keys:
        print(f"Fetching the status history of {len(done_keys)} done and {len(open_keys)} open "
              f"{'story' if len(done_keys) + len(open_keys) == 1 else 'stories'}...", file=sys.stderr)
        status_changes = fetch_all_status_changes([story_data[key] for key in done_keys + open_keys])

    # Place done stories on the days they were worked on, from their status history
    done_dates = {}
    for key in done_keys:
        dates = work_dates(status_changes[key], config.statuses, config.work_start_progress)
        if dates:
            done_dates[key] = dates
    missing = len(done_keys) - len(done_dates)
    if missing:
        print(f"  {missing} done {'story has' if missing == 1 else 'stories have'} no change to a done status "
              f"in the history, so the plan start is used", file=sys.stderr)

    # Find the stretches open stories spent in the implementation status or a later one
    for key in open_keys:
        stretches = implementation_stretches(status_changes[key], config.implementation_status, config.statuses)
        if stretches:
            story_data[key]["_implementation_date"] = stretches[0][0].isoformat()
            story_data[key]["_implementation_stretches"] = [
                [start.isoformat(), end.isoformat() if end else ""] for start, end in stretches]

    # The planner takes stories in this order
    schedule_keys = ordered_keys
    if config.use_backlog_order and ordered_keys:
        print("Fetching the backlog order...", file=sys.stderr)
        positions = fetch_backlog_order(ordered_keys)
        schedule_keys = sorted(ordered_keys, key=lambda key: positions.get(key, len(positions)))

    scheduled = schedule_stories(config, story_data, schedule_keys, done_dates)
    # The story rows follow the Jira rank with use_backlog_order, else the configuration file
    row_position = {key: i for i, key in enumerate(schedule_keys)}
    scheduled.sort(key=lambda s: row_position[s["key"]])
    print(f"Scheduled {len(scheduled)} stories.", file=sys.stderr)
    compare_date = None
    snapshots = []
    if history_dir:
        today = date.today()
        current = plan_snapshot(scheduled, config.statuses, today)
        snapshots = [snap for snap in load_snapshots(history_dir) if snap["date"] != current["date"]]
        snapshots.append(current)
        if since:
            baseline = pick_baseline(snapshots, today, None if since == "last" else since)
            if baseline:
                compare_date = baseline["date"]
                print(f"Comparing with the snapshot of {compare_date}.", file=sys.stderr)
            else:
                print(f"No earlier snapshot in {history_dir}, so there are no changes to show.", file=sys.stderr)
        os.makedirs(history_dir, exist_ok=True)
        snapshot_path = os.path.join(history_dir, f"plan-{current['date']}.json")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=1)
        print(f"Wrote {snapshot_path}", file=sys.stderr)

    # Stories that left the plan come from older snapshots, which may not have their issue type
    removed = removed_stories(snapshots, {s["key"] for s in scheduled}, date.today())
    if removed:
        untyped = [r for r in removed if not r.get("issue_type_icon_url")]
        if untyped:
            found = fetch_stories_by_keys([r["key"] for r in untyped], warn_missing=False)
            for r in untyped:
                if r["key"] not in found:
                    print(f"Warning: story {r['key']} is no longer in Jira, so it has no type icon", file=sys.stderr)
                issue_type = (found.get(r["key"], {}).get("fields", {}).get("issuetype")) or {}
                r["issue_type"] = issue_type.get("name", "")
                r["issue_type_icon_url"] = issue_type.get("iconUrl", "")

    roadmap_epics = []
    for key in empty_epics:
        info = epics_info.get(key)
        if not info or not (info["start"] or info["due"]):
            print(f"Warning: epic {key} has no stories and no start or due date, skipping", file=sys.stderr)
            continue
        if info["start"] and info["due"] and info["start"] > info["due"]:
            print(f"Warning: epic {key} starts after its due date, showing only its due date", file=sys.stderr)
            info = dict(info, start=info["due"])
        print(f"  Epic {key} has no stories yet, showing its dates from Jira", file=sys.stderr)
        roadmap_epics.append(info)

    # The epic rows follow the configuration file, or with use_backlog_order their Jira rank. With
    # group_by_space, they are sorted by the name of their Jira space first, so each space has its epics together.
    epic_order = list(config.epics)
    if config.use_backlog_order and epic_keys:
        print("Fetching the rank of the epics...", file=sys.stderr)
        epic_positions = fetch_backlog_order(epic_keys)

        def epic_sort_key(key):
            rank = epic_positions.get(key, len(epic_positions))
            if not config.group_by_space:
                return rank
            space = epics_info.get(key, {}).get("space", "")
            return (space == "", space.lower(), rank)
        epic_order = sorted(epic_keys, key=epic_sort_key)

    # Put the Jira issue type icons into the page, so it shows them without a Jira login
    epic_icon_url = find_epic_icon_url(story_data.values())
    icon_urls = [s["issue_type_icon_url"] for s in scheduled if s["issue_type_icon_url"]]
    icon_urls += [r["issue_type_icon_url"] for r in removed if r.get("issue_type_icon_url")]
    if epic_icon_url:
        icon_urls.append(epic_icon_url)
    # The avatars of the spaces, for their header rows
    if config.use_backlog_order and config.group_by_space:
        icon_urls += [info["space_avatar"] for info in epics_info.values() if info.get("space_avatar")]
    icon_images = {}
    if icon_urls:
        print(f"Downloading {len(set(icon_urls))} issue type icons...", file=sys.stderr)
        icon_images = fetch_icon_images(icon_urls)

    write_gantt_html(scheduled, config.statuses, config.dependencies, config.holidays, config.status_colors,
                     compare_date, snapshots, removed, icon_images, epic_icon_url, epics_info, roadmap_epics,
                     epic_order,
                     config.visuals, group_by_space=config.use_backlog_order and config.group_by_space,
                     keep_story_order=config.use_backlog_order, team=config.team)


def run_velocity(config_path, weeks):
    """Print the hours per story point of the team in each of the last full weeks, and in total.

    The hours are the team hours from the configuration file, without days off and holidays.
    The points are those of stories assigned to a team member that moved to a done status.
    A story without points counts as default_hours divided by the configured velocity.
    """
    config = load_gantt_config(config_path)
    end = date.today() - timedelta(days=date.today().weekday())  # Monday of the current week
    start = end - timedelta(weeks=weeks)

    assumed_points = config.default_hours / config.velocity
    print(f"Fetching the stories finished from {start} to {end - timedelta(days=1)}...", file=sys.stderr)
    week_starts = [start + timedelta(weeks=i) for i in range(weeks)]
    points = {w: 0.0 for w in week_starts}
    pointed = {w: 0 for w in week_starts}
    unpointed = {w: 0 for w in week_starts}
    for issue in fetch_finished_stories(start, end):
        fields = issue["fields"]
        if get_assignee(fields) not in config.team:
            continue
        done = date.fromisoformat((fields.get("statuscategorychangedate") or "")[:10] or start.isoformat())
        week = done - timedelta(days=done.weekday())
        if week not in points:
            continue
        story_points = get_story_points_numeric(fields)
        if story_points is None:
            unpointed[week] += 1
            story_points = assumed_points
        else:
            pointed[week] += 1
        points[week] += story_points

    def team_hours(first_day):
        return sum(hours_on(first_day + timedelta(days=i), avail)
                   for avail in config.team.values() for i in range(7))

    def ratio(hours, pts):
        return f"{hours / pts:.1f}" if pts else "-"

    print(f"{'Week':<10}  {'Hours':>6}  {'Stories':>7}  {'Points':>6}  {'Hours/point':>11}  {'No points':>9}")
    total_hours = total_points = total_pointed = total_unpointed = 0
    for week in week_starts:
        hours = team_hours(week)
        iso_year, iso_week, _ = week.isocalendar()
        label = f"{iso_year}-W{iso_week:02d}"
        print(f"{label:<10}  {hours:>6g}  {pointed[week]:>7}  {points[week]:>6.1f}  "
              f"{ratio(hours, points[week]):>11}  {unpointed[week]:>9}")
        total_hours += hours
        total_points += points[week]
        total_pointed += pointed[week]
        total_unpointed += unpointed[week]
    print(f"{'Total':<10}  {total_hours:>6g}  {total_pointed:>7}  {total_points:>6.1f}  "
          f"{ratio(total_hours, total_points):>11}  {total_unpointed:>9}")
    print()
    if total_points:
        print(f"Velocity over the last {weeks} weeks: {total_hours / total_points:.1f} hours per story point. "
              f"{config_path} uses {config.velocity}.")
    else:
        print(f"No stories with story points were finished in the last {weeks} weeks.")
    if total_unpointed:
        stories = "story had" if total_unpointed == 1 else "stories had"
        print(f"{total_unpointed} finished {stories} no story points. Each counts as {assumed_points:.1f} points: "
              f"default_hours {config.default_hours:g} divided by velocity {config.velocity:g}.")


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


class _Tee(io.TextIOBase):
    """Write to several streams at once."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def build_gantt_page(config_path, history_dir=None, since=None):
    """Run the gantt command and return (HTML of the chart, None), or (None, error message)."""
    page, log = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(page), contextlib.redirect_stderr(_Tee(sys.stderr, log)):
            run_gantt(config_path, history_dir, since)
    except (Exception, SystemExit) as e:
        # Errors in the configuration print a message and exit; Jira errors raise an exception
        lines = [line for line in log.getvalue().splitlines() if line.strip()]
        message = lines[-1] if isinstance(e, SystemExit) and lines else f"{type(e).__name__}: {e}"
        print(f"Refresh failed: {message}", file=sys.stderr)
        return None, message
    return page.getvalue(), None


def run_serve(config_path, history_dir, since, port, open_browser):
    """Serve the Gantt chart on localhost. Its refresh button fetches the stories from Jira again.

    With history_dir, each refresh also stores a plan snapshot and the chart as
    gantt-YYYY-MM-DD.html there, like generate-gantt.sh does.
    """
    state = {"page": None}
    lock = threading.Lock()

    def refresh():
        page, error = build_gantt_page(config_path, history_dir, since)
        if page is None:
            return error
        state["page"] = page
        if history_dir:
            path = os.path.join(history_dir, f"gantt-{date.today()}.html")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(page)
            print(f"Wrote {path}", file=sys.stderr)
        return None

    class Handler(http.server.BaseHTTPRequestHandler):
        def send_text(self, status, text, content_type="text/plain"):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path not in ("/", "/index.html"):
                self.send_text(404, "Not found")
            elif state["page"] is None:
                self.send_text(503, "The chart is not ready. See the terminal for the error, "
                                    "then click refresh or restart the server.")
            else:
                self.send_text(200, state["page"], "text/html")

        def do_POST(self):
            # The custom header keeps other web sites from starting a refresh: browsers do not
            # send it across sites without a CORS answer, which this server never gives.
            if self.path != "/refresh" or self.headers.get("X-Jira-Smurf") != "refresh":
                self.send_text(404, "Not found")
                return
            if not lock.acquire(blocking=False):
                # A refresh is already running; wait for it instead of starting another one
                with lock:
                    pass
                self.send_text(200, "ok")
                return
            try:
                error = refresh()
            finally:
                lock.release()
            if error:
                self.send_text(500, error)
            else:
                self.send_text(200, "ok")

        def log_message(self, format, *args):
            pass  # keep the terminal for the progress of the refresh

    if history_dir:
        os.makedirs(history_dir, exist_ok=True)
    print("Building the chart...", file=sys.stderr)
    refresh()

    # Only this computer can open the chart: it shows Jira data, fetched with your token
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}/"
    print(f"Serving the Gantt chart at {url} — press Ctrl+C to stop", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        server.server_close()


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
                              help="compare with the latest snapshot on or before this date when the chart opens; "
                                   "without a date, with the latest snapshot before the current day")

    serve_parser = subparsers.add_parser("serve", help="serve the Gantt chart on localhost, with a button to refresh it from Jira")
    serve_parser.add_argument("-c", "--config", metavar="FILE", default="gantt.toml",
                              help="planning configuration (default: gantt.toml)")
    serve_parser.add_argument("--history", metavar="DIR",
                              help="on each refresh, store a snapshot of the plan and the chart in DIR, and show "
                                   "the changes and the trend from the snapshots there")
    serve_parser.add_argument("--since", metavar="YYYY-MM-DD", nargs="?", const="last", type=parse_since,
                              help="compare with the latest snapshot on or before this date when the chart opens; "
                                   "without a date, with the latest snapshot before the current day")
    serve_parser.add_argument("--port", metavar="N", type=int, default=8000, help="port to listen on (default: 8000)")
    serve_parser.add_argument("--no-open", action="store_true", help="do not open the chart in the browser")

    velocity_parser = subparsers.add_parser("velocity", help="measure the hours per story point of the last weeks")
    velocity_parser.add_argument("-c", "--config", metavar="FILE", default="gantt.toml",
                                 help="planning configuration with the team (default: gantt.toml)")
    velocity_parser.add_argument("--weeks", metavar="N", type=int, default=8,
                                 help="number of full weeks to look back, not counting the current week (default: 8)")

    subparsers.add_parser("fields", help="list the numeric fields of one story, to find the story points field")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "since", None) and not args.history:
        parser.error("--since needs --history")
    if args.command == "velocity" and args.weeks < 1:
        parser.error("--weeks must be 1 or more")
    validate_config()

    if args.command == "fields":
        discover_fields()
        return
    if args.command == "velocity":
        run_velocity(args.config, args.weeks)
        return
    if args.command == "serve":
        run_serve(args.config, args.history, args.since, args.port, not args.no_open)
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
