#!/usr/bin/env python3
"""
sync_linear_to_gcal.py

- Récupère issues et projects depuis Linear (GraphQL)
- Crée ou met à jour des événements Google Calendar en évitant les doublons
- Support: GOOGLE_APPLICATION_CREDENTIALS (path) ou GOOGLE_SERVICE_ACCOUNT_JSON (content)
- Secrets attendus: LINEAR_API_KEY, either GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_SERVICE_ACCOUNT_JSON
- Optional: GCAL_CALENDAR_ID, TIMEZONE
"""

import os
import json
import requests
from datetime import datetime, timedelta, date
from dateutil import parser
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configuration via env
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY")
GCAL_CALENDAR_ID = os.environ.get("GCAL_CALENDAR_ID", "primary")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
# fenêtre de recherche (en jours) autour de la date cible pour la recherche d'événements
SEARCH_WINDOW_DAYS = int(os.environ.get("SEARCH_WINDOW_DAYS", "365"))

if not LINEAR_API_KEY:
    raise SystemExit("Missing environment variable: LINEAR_API_KEY is required")

def build_gcal_service():
    credentials = None
    # prefer file path
    if GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(GOOGLE_APPLICATION_CREDENTIALS):
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_APPLICATION_CREDENTIALS, scopes=["https://www.googleapis.com/auth/calendar"]
        )
    else:
        raw = GOOGLE_SERVICE_ACCOUNT_JSON
        if raw:
            try:
                info = json.loads(raw)
            except json.JSONDecodeError as e:
                raise SystemExit(f"Invalid GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/calendar"]
            )
    if not credentials:
        raise SystemExit("Missing Google credentials: set GOOGLE_APPLICATION_CREDENTIALS (path) or GOOGLE_SERVICE_ACCOUNT_JSON (content)")
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return service

def linear_query(query, variables=None):
    headers = {
    "Authorization": LINEAR_API_KEY,
    "Content-Type": "application/json"
    }
    payload = {"query": query, "variables": variables or {}}
    resp = requests.post(LINEAR_GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        print("Linear API request failed")
        print("Status:", resp.status_code)
        print("Response body:", resp.text)
        resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        print("Linear GraphQL errors:", json.dumps(data["errors"], indent=2))
        raise RuntimeError("Linear GraphQL returned errors")
    return data

def get_issues_with_due(limit=200):
    query = """
    query($limit:Int) {
      issues(first:$limit) {
        nodes {
          id
          title
          description
          url
          dueDate
          project { name }
        }
      }
    }
    """
    res = linear_query(query, {"limit": limit})
    return res.get("data", {}).get("issues", {}).get("nodes", [])

def get_projects_with_target(limit=100):
    query = """
    query($limit:Int) {
      projects(first:$limit) {
        nodes {
          id
          name
          description
          url
          targetDate
        }
      }
    }
    """
    res = linear_query(query, {"limit": limit})
    return res.get("data", {}).get("projects", {}).get("nodes", [])

def to_rfc3339(dt: datetime):
    return dt.astimezone(pytz.UTC).isoformat()

def make_search_window_for_date(target_iso: str, days=SEARCH_WINDOW_DAYS):
    """Return (timeMin, timeMax) RFC3339 around target_iso"""
    # parse date-only or datetime
    if "T" in target_iso:
        t = parser.isoparse(target_iso)
        time_min = t - timedelta(days=days)
        time_max = t + timedelta(days=days)
    else:
        d = parser.isoparse(target_iso).date()
        # use midday UTC to cover timezone shifts
        time_min = datetime.combine(d - timedelta(days=days), datetime.min.time()).replace(tzinfo=pytz.UTC)
        time_max = datetime.combine(d + timedelta(days=days), datetime.max.time()).replace(tzinfo=pytz.UTC)
    return to_rfc3339(time_min), to_rfc3339(time_max)

def find_event_by_linear_id(service, calendar_id, linear_id, target_date_iso=None):
    """
    Uses privateExtendedProperty to find an event with linear_id.
    If target_date_iso provided, we restrict the time window around it for efficiency.
    Returns the first matching event or None.
    """
    # If we have a date, build a tight window; otherwise use +/- SEARCH_WINDOW_DAYS from today
    if target_date_iso:
        time_min, time_max = make_search_window_for_date(target_date_iso)
    else:
        now = datetime.utcnow()
        time_min = to_rfc3339(now - timedelta(days=SEARCH_WINDOW_DAYS))
        time_max = to_rfc3339(now + timedelta(days=SEARCH_WINDOW_DAYS))
    page_token = None
    while True:
        try:
            resp = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                privateExtendedProperty=f"linear_id={linear_id}",
                singleEvents=True,
                pageToken=page_token,
                maxResults=250
            ).execute()
        except HttpError as e:
            print("Error while searching events:", e)
            raise
        items = resp.get("items", [])
        if items:
            return items[0]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return None

def build_event_body_from_linear(item, kind="issue"):
    """
    Build Google event body from Linear item. Handles date-only and dateTime values.
    Returns None if no date present.
    """
    linear_id = item.get("id")
    title = item.get("title") or item.get("name") or "No title"
    description = item.get("description") or ""
    url = item.get("url") or ""
    date_field = item.get("dueDate") if kind == "issue" else item.get("targetDate")
    if not date_field:
        return None

    # If date-time present
    if "T" in date_field:
        start_dt = parser.isoparse(date_field)
        # default duration: 1 hour
        end_dt = start_dt + timedelta(hours=1)
        start = {"dateTime": to_rfc3339(start_dt), "timeZone": TIMEZONE}
        end = {"dateTime": to_rfc3339(end_dt), "timeZone": TIMEZONE}
    else:
        # all-day event: Google expects end = next day (exclusive)
        d = parser.isoparse(date_field).date()
        start = {"date": d.isoformat()}
        end = {"date": (d + timedelta(days=1)).isoformat()}

    body = {
        "summary": title,
        "description": f"{description}\n\n{url}",
        "start": start,
        "end": end,
        "extendedProperties": {
            "private": {
                "linear_id": linear_id,
                "linear_kind": kind,
                "linear_url": url
            }
        }
    }
    return body

def upsert_event_for_linear_item(service, calendar_id, item, kind="issue"):
    body = build_event_body_from_linear(item, kind=kind)
    if not body:
        print(f"Skipping {kind} {item.get('id')} — no date present")
        return None
    linear_id = item.get("id")
    date_field = item.get("dueDate") if kind == "issue" else item.get("targetDate")
    existing = find_event_by_linear_id(service, calendar_id, linear_id, target_date_iso=date_field)
    if existing:
        event_id = existing["id"]
        try:
            updated = service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
            print(f"Updated event {event_id} for linear {linear_id}")
            return updated
        except HttpError as e:
            print(f"Failed to update event {event_id}: {e}")
            raise
    else:
        try:
            created = service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"Created event {created.get('id')} for linear {linear_id}")
            return created
        except HttpError as e:
            print(f"Failed to create event for linear {linear_id}: {e}")
            raise

def main():
    service = build_gcal_service()

    print("Fetching Linear issues...")
    try:
        issues = get_issues_with_due(limit=200)
    except Exception as e:
        print("Error fetching issues:", e)
        issues = []
    print(f"Found {len(issues)} issues returned by Linear")

    for i in issues:
        try:
            if i.get("dueDate"):
                upsert_event_for_linear_item(service, GCAL_CALENDAR_ID, i, kind="issue")
        except Exception as e:
            print(f"Error processing issue {i.get('id')}: {e}")

    print("Fetching Linear projects...")
    try:
        projects = get_projects_with_target(limit=100)
    except Exception as e:
        print("Error fetching projects:", e)
        projects = []
    print(f"Found {len(projects)} projects returned by Linear")

    for p in projects:
        try:
            if p.get("targetDate"):
                upsert_event_for_linear_item(service, GCAL_CALENDAR_ID, p, kind="project")
        except Exception as e:
            print(f"Error processing project {p.get('id')}: {e}")

if __name__ == "__main__":
    main()
