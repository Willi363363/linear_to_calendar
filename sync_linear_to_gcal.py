#!/usr/bin/env python3
"""
sync_linear_to_gcal.py

- Récupère les issues depuis Linear (GraphQL) avec métadonnées enrichies
- Crée ou met à jour des événements Google Calendar en évitant les doublons
- Support: GOOGLE_APPLICATION_CREDENTIALS (path) ou GOOGLE_SERVICE_ACCOUNT_JSON (content)
- Secrets attendus: LINEAR_API_KEY, either GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_SERVICE_ACCOUNT_JSON
- Optional: GCAL_CALENDAR_ID, TIMEZONE
"""

import os
import json
import requests
from datetime import datetime, timedelta, date, time as dt_time
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
SEARCH_WINDOW_DAYS = int(os.environ.get("SEARCH_WINDOW_DAYS", "365"))

if not LINEAR_API_KEY:
    raise SystemExit("Missing environment variable: LINEAR_API_KEY is required")

def build_gcal_service():
    credentials = None
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

def get_issues_with_metadata(limit=200):
    """
    Récupère les issues avec toutes les métadonnées enrichies:
    - description de l'issue
    - projet (nom + description)
    - parent issue
    - sub-issues (children)
    - labels
    - dueDate (date de livraison)
    """
    query = """
    query($limit: Int) {
      issues(first: $limit) {
        nodes {
          id
          title
          description
          url
          dueDate
          createdAt
          startedAt
          completedAt
          project {
            id
            name
            description
            url
            targetDate
          }
          parent {
            id
            title
            url
          }
          children {
            nodes {
              id
              title
              url
            }
          }
          labels {
            nodes {
              id
              name
              color
            }
          }
        }
      }
    }
    """
    res = linear_query(query, {"limit": limit})
    return res.get("data", {}).get("issues", {}).get("nodes", [])

def to_rfc3339(dt: datetime):
    """
    Ensure datetime is timezone-aware and return RFC3339 string
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.UTC)
    return dt.astimezone(pytz.UTC).isoformat()

def make_search_window_for_date(target_iso: str, days=SEARCH_WINDOW_DAYS):
    """Return (timeMin, timeMax) RFC3339 around target_iso"""
    if not target_iso:
        now = datetime.utcnow().replace(tzinfo=pytz.UTC)
        return to_rfc3339(now - timedelta(days=days)), to_rfc3339(now + timedelta(days=days))
    if "T" in target_iso:
        t = parser.isoparse(target_iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=pytz.UTC)
        time_min = t - timedelta(days=days)
        time_max = t + timedelta(days=days)
    else:
        d = parser.isoparse(target_iso).date()
        time_min = datetime.combine(d - timedelta(days=days), dt_time.min).replace(tzinfo=pytz.UTC)
        time_max = datetime.combine(d + timedelta(days=days), dt_time.max).replace(tzinfo=pytz.UTC)
    return to_rfc3339(time_min), to_rfc3339(time_max)

def get_best_date_for_issue(issue):
    """
    Détermine la meilleure date à utiliser pour l'événement calendar.
    Ordre de priorité:
    1. dueDate (date d'échéance de l'issue)
    2. targetDate du projet associé
    3. completedAt (si l'issue est terminée)
    4. startedAt (si l'issue est en cours)
    5. createdAt (date de création en dernier recours)

    Retourne (date_iso_string, source_name) ou (None, None)
    """
    if not isinstance(issue, dict):
        return None, None

    if issue.get("dueDate"):
        return issue["dueDate"], "dueDate"

    project = issue.get("project") or {}
    if project.get("targetDate"):
        return project["targetDate"], "project_targetDate"

    if issue.get("completedAt"):
        return issue["completedAt"], "completedAt"

    if issue.get("startedAt"):
        return issue["startedAt"], "startedAt"

    if issue.get("createdAt"):
        return issue["createdAt"], "createdAt"

    return None, None

def find_event_by_linear_id(service, calendar_id, linear_id, target_date_iso=None):
    """
    Trouve un événement Google Calendar ayant privateExtendedProperty linear_id=<linear_id>.
    Si target_date_iso fourni, on restreint la fenêtre de recherche autour de cette date.
    Retourne l'objet événement trouvé ou None.
    """
    if not service or not calendar_id or not linear_id:
        return None

    if target_date_iso:
        time_min, time_max = make_search_window_for_date(target_date_iso)
    else:
        now = datetime.utcnow().replace(tzinfo=pytz.UTC)
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

        items = resp.get("items", []) or []
        if items:
            return items[0]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return None

def format_rich_description(issue):
    """
    Construit une description enrichie pour l'événement Google Calendar
    avec toutes les métadonnées de l'issue
    """
    parts = []

    # safe access
    issue = issue or {}
    description_text = issue.get("description") or ""
    if description_text:
        parts.append("📝 Description de l'issue:")
        parts.append(description_text)
        parts.append("")

    project = issue.get("project") or {}
    if project:
        parts.append("📁 Projet:")
        parts.append(f"  • {project.get('name', 'N/A')}")
        if project.get("description"):
            parts.append(f"  • Description: {project['description']}")
        if project.get("url"):
            parts.append(f"  • Lien: {project['url']}")
        parts.append("")

    parent = issue.get("parent") or {}
    if parent and parent.get("title"):
        parts.append("⬆️ Issue parente:")
        parts.append(f"  • {parent.get('title', 'N/A')}")
        if parent.get("url"):
            parts.append(f"  • Lien: {parent['url']}")
        parts.append("")

    children = (issue.get("children") or {}).get("nodes") or []
    if children:
        parts.append("⬇️ Sous-issues:")
        for child in children:
            child = child or {}
            parts.append(f"  • {child.get('title', 'N/A')}")
            if child.get("url"):
                parts.append(f"    {child['url']}")
        parts.append("")

    labels = (issue.get("labels") or {}).get("nodes") or []
    if labels:
        parts.append("🏷️ Labels:")
        for label in labels:
            label = label or {}
            label_text = f"  • {label.get('name', 'N/A')}"
            if label.get("color"):
                label_text += f" (#{label['color']})"
            parts.append(label_text)
        parts.append("")

    if issue.get("url"):
        parts.append("🔗 Lien Linear:")
        parts.append(issue["url"])

    return "\n".join(parts)

def build_event_body_from_issue(issue):
    """
    Build Google event body from Linear issue with enriched metadata.
    Uses dueDate primarily (consistent with original behavior).
    Returns None if no usable date present.
    """
    if not isinstance(issue, dict):
        return None

    linear_id = issue.get("id")
    title = issue.get("title") or "No title"
    due_date = issue.get("dueDate")

    if not due_date:
        return None

    description = format_rich_description(issue)

    # date/time handling
    if "T" in due_date:
        try:
            start_dt = parser.isoparse(due_date)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=pytz.UTC)
        except Exception:
            # fallback parse as date
            start_dt = datetime.utcnow().replace(tzinfo=pytz.UTC)
        end_dt = start_dt + timedelta(hours=1)
        start = {"dateTime": to_rfc3339(start_dt), "timeZone": TIMEZONE}
        end = {"dateTime": to_rfc3339(end_dt), "timeZone": TIMEZONE}
    else:
        d = parser.isoparse(due_date).date()
        start = {"date": d.isoformat()}
        end = {"date": (d + timedelta(days=1)).isoformat()}

    labels = (issue.get("labels") or {}).get("nodes") or []
    label_names = ",".join([l.get("name", "") for l in labels if isinstance(l, dict) and l.get("name")])

    project = issue.get("project") or {}
    parent = issue.get("parent") or {}

    body = {
        "summary": title,
        "description": description,
        "start": start,
        "end": end,
        "extendedProperties": {
            "private": {
                "linear_id": linear_id or "",
                "linear_kind": "issue",
                "linear_url": issue.get("url", ""),
                "project_id": project.get("id", ""),
                "project_name": project.get("name", ""),
                "parent_id": parent.get("id", ""),
                "labels": label_names
            }
        }
    }
    return body

def upsert_event_for_issue(service, calendar_id, issue):
    """
    Crée ou met à jour un événement Google Calendar pour une issue Linear.
    Utilise la dueDate de l'issue.
    """
    if not isinstance(issue, dict):
        return None

    linear_id = issue.get("id")
    title = issue.get("title", "Sans titre")
    due_date = issue.get("dueDate")

    if not due_date:
        print(f"⏭️  Skipping issue '{title}' (ID: {linear_id}) — pas de dueDate définie dans Linear")
        return None

    body = build_event_body_from_issue(issue)
    if not body:
        print(f"⚠️  Could not build event body for issue {linear_id}")
        return None

    existing = find_event_by_linear_id(service, calendar_id, linear_id, target_date_iso=due_date)

    if existing:
        event_id = existing.get("id")
        try:
            updated = service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
            print(f"✅ Updated: '{title}' (dueDate: {due_date})")
            return updated
        except HttpError as e:
            print(f"❌ Failed to update event {event_id}: {e}")
            raise
    else:
        try:
            created = service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"✨ Created: '{title}' (dueDate: {due_date})")
            return created
        except HttpError as e:
            print(f"❌ Failed to create event for issue {linear_id}: {e}")
            raise

def main():
    service = build_gcal_service()

    print("🔍 Fetching Linear issues with metadata...")
    try:
        issues = get_issues_with_metadata(limit=200)
    except Exception as e:
        print(f"❌ Error fetching issues: {e}")
        raise

    print(f"📊 Found {len(issues)} issues returned by Linear")

    synced_count = 0
    skipped_count = 0
    error_count = 0

    for issue in issues:
        try:
            result = upsert_event_for_issue(service, GCAL_CALENDAR_ID, issue)
            if result:
                synced_count += 1
            else:
                skipped_count += 1
        except Exception as e:
            error_count += 1
            issue_id = issue.get('id') if isinstance(issue, dict) else '<unknown>'
            print(f"❌ Error processing issue {issue_id}: {e}")

    print("\n" + "="*50)
    print("📈 Synchronization Summary:")
    print(f"  ✅ Synced: {synced_count}")
    print(f"  ⏭️  Skipped: {skipped_count}")
    print(f"  ❌ Errors: {error_count}")
    print("="*50)

if __name__ == "__main__":
    main()
