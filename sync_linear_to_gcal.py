#!/usr/bin/env python3
"""
sync_linear_to_gcal.py

- Récupère issues et projects depuis Linear (GraphQL)
- Crée ou met à jour des événements Google Calendar en évitant les doublons
- Utilise un Service Account Google passé via la variable d'environnement GOOGLE_SERVICE_ACCOUNT_JSON
- Secrets attendus: LINEAR_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON, optional GCAL_CALENDAR_ID
"""

import os
import json
import requests
from datetime import datetime, timedelta
from dateutil import parser
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configuration via env
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GCAL_CALENDAR_ID = os.environ.get("GCAL_CALENDAR_ID", "primary")

if not LINEAR_API_KEY or not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise SystemExit("Missing environment variables: LINEAR_API_KEY and GOOGLE_SERVICE_ACCOUNT_JSON are required")

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
# fenêtre de recherche d'événements (aujourd'hui -> +2 ans)
SEARCH_WINDOW_YEARS = 2

def linear_query(query, variables=None):
    headers = {
        "Authorization": f"Bearer {LINEAR_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"query": query, "variables": variables or {}}
    resp = requests.post(LINEAR_GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Linear API errors: {data['errors']}")
    return data

def get_issues_with_due(limit=100):
    query = """
    query($limit:Int) {
      issues(first:$limit filter:{dueDate:{notIn:[]}}) {
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

def get_projects_with_target(limit=50):
    query = """
    query($limit:Int) {
      projects(first:$limit filter:{targetDate:{notIn:[]}}) {
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

def build_gcal_service():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return service

def list_events_in_window(service, time_min, time_max):
    all_events = []
    page_token = None
    while True:
        try:
            resp = service.events().list(
                calendarId=GCAL_CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                maxResults=2500,
                pageToken=page_token
            ).execute()
        except HttpError as e:
            raise
        items = resp.get("items", [])
        all_events.extend(items)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return all_events

def find_event_by_linear_id(service, linear_id):
    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    future = (now + timedelta(days=365*SEARCH_WINDOW_YEARS)).isoformat() + "Z"
    events = list_events_in_window(service, time_min, future)
    for ev in events:
        ext = ev.get("extendedProperties", {}).get("private", {})
        if ext.get("linear_id") == linear_id:
            return ev
    return None

def date_iso_from_iso_or_datevalue(value):
    # accepte ISO datetime ou date string -> retourne date string YYYY-MM-DD
    if not value:
        return None
    dt = parser.isoparse(value)
    return dt.date().isoformat()

def upsert_issue_event(service, issue):
    linear_id = issue.get("id")
    if not linear_id:
        return
    if not issue.get("dueDate"):
        return
    existing = find_event_by_linear_id(service, linear_id)
    project_name = issue.get("project", {}).get("name") or "NoProject"
    title = f"[{project_name}] - {issue.get('title')}"
    start_date = date_iso_from_iso_or_datevalue(issue["dueDate"])
    body = {
        "summary": title,
        "description": (issue.get("description") or "") + "\n\n" + (issue.get("url") or ""),
        "start": {"date": start_date},
        "end": {"date": start_date},
        "extendedProperties": {
            "private": {"linear_id": linear_id, "linear_url": issue.get("url", "")}
        }
    }
    if existing:
        try:
            service.events().patch(calendarId=GCAL_CALENDAR_ID, eventId=existing["id"], body=body).execute()
            print(f"Updated event for issue {linear_id}")
        except HttpError as e:
            print(f"Failed to update event {existing['id']}: {e}")
    else:
        try:
            service.events().insert(calendarId=GCAL_CALENDAR_ID, body=body).execute()
            print(f"Created event for issue {linear_id}")
        except HttpError as e:
            print(f"Failed to create event for issue {linear_id}: {e}")

def upsert_project_event(service, project):
    linear_id = project.get("id")
    if not linear_id:
        return
    if not project.get("targetDate"):
        return
    existing = find_event_by_linear_id(service, linear_id)
    title = f"Project Deadline: {project.get('name')}"
    start_date = date_iso_from_iso_or_datevalue(project["targetDate"])
    body = {
        "summary": title,
        "description": (project.get("description") or "") + "\n\n" + (project.get("url") or ""),
        "start": {"date": start_date},
        "end": {"date": start_date},
        "extendedProperties": {
            "private": {"linear_id": linear_id, "linear_url": project.get("url", "")}
        }
    }
    if existing:
        try:
            service.events().patch(calendarId=GCAL_CALENDAR_ID, eventId=existing["id"], body=body).execute()
            print(f"Updated project event {linear_id}")
        except HttpError as e:
            print(f"Failed to update project event {existing['id']}: {e}")
    else:
        try:
            service.events().insert(calendarId=GCAL_CALENDAR_ID, body=body).execute()
            print(f"Created project event {linear_id}")
        except HttpError as e:
            print(f"Failed to create project event {linear_id}: {e}")

def main():
    service = build_gcal_service()
    print("Fetching Linear issues...")
    try:
        issues = get_issues_with_due(limit=200)
    except Exception as e:
        print(f"Error fetching issues: {e}")
        issues = []
    for issue in issues:
        try:
            upsert_issue_event(service, issue)
        except Exception as e:
            print(f"Error upserting issue {issue.get('id')}: {e}")

    print("Fetching Linear projects...")
    try:
        projects = get_projects_with_target(limit=100)
    except Exception as e:
        print(f"Error fetching projects: {e}")
        projects = []
    for project in projects:
        try:
            upsert_project_event(service, project)
        except Exception as e:
            print(f"Error upserting project {project.get('id')}: {e}")

if __name__ == "__main__":
    main()
