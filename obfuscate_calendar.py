from datetime import datetime, timedelta, timezone
import pytz
import os
import pickle
import caldav
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import constants
from google.auth.transport.requests import Request
import time
import logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ICLOUD_CALENDARS_TO_SKIP = constants.ICLOUD_CALENDARS_TO_SKIP
ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS = constants.ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS
GOOGLE_CALENDARS_TO_SKIP_DELETION = constants.GOOGLE_CALENDARS_TO_SKIP_DELETION

DAYS_TO_SYNC = 31

logging.getLogger("root").setLevel(logging.ERROR)

start_time = time.time()

client = caldav.DAVClient(
    url="https://caldav.icloud.com/",
    username=constants.ICLOUD_USERNAME,
    password=constants.ICLOUD_PASSWORD,
)
principal = client.principal()
calendars = principal.calendars()

TOKEN_FILE = os.path.join(BASE_DIR, "token.pickle")
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def authenticate_google():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "rb") as token:
                creds = pickle.load(token)
        except Exception as e:
            print(f"Error loading token file: {e}. Deleting invalid token file.")
            os.remove(TOKEN_FILE)
            creds = None

    try:
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    print(f"Error refreshing token: {e}. Deleting token file to reauthenticate.")
                    os.remove(TOKEN_FILE)
                    creds = None
            if not creds:
                print("Authenticating with Google OAuth2...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    os.path.join(BASE_DIR, "credentials.json"), SCOPES
                )
                creds = flow.run_local_server(port=0)
                with open(TOKEN_FILE, "wb") as token:
                    pickle.dump(creds, token)
    except Exception as e:
        print(f"Failed to authenticate with Google: {e}")
        raise SystemExit("Exiting script due to authentication failure.")

    return creds


creds = authenticate_google()
service = build("calendar", "v3", credentials=creds)

GOOGLE_CALENDAR_ID = constants.GOOGLE_CALENDAR_ID

# 1) Delete ALL events from the Google Calendar unless it's in the skip list.
if GOOGLE_CALENDAR_ID not in GOOGLE_CALENDARS_TO_SKIP_DELETION:
    print("Deleting all existing events from Google Calendar...")
    page_token = None
    while True:
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            pageToken=page_token,
            maxResults=2500
        ).execute()
        events = events_result.get("items", [])
        for event in events:
            if event.get("eventType") == "birthday":
                continue
            service.events().delete(
                calendarId=GOOGLE_CALENDAR_ID, eventId=event["id"]
            ).execute()
        page_token = events_result.get("nextPageToken")
        if not page_token:
            break
    print("All existing events deleted.")
else:
    print(f"Skipping deletion for {GOOGLE_CALENDAR_ID} (in skip-deletion list).")


def convert_recurrence(event):
    recurrence = []
    if hasattr(event, "rrule"):
        rrule = event.rrule.value
        recurrence.append(f"RRULE:{rrule}")
    return recurrence


def obfuscate_event(event):
    default_timezone = "America/Los_Angeles"
    start_dt = event.vobject_instance.vevent.dtstart.value
    end_dt = event.vobject_instance.vevent.dtend.value

    def get_timezone_name(tzinfo):
        if tzinfo:
            try:
                if hasattr(tzinfo, "zone"):
                    return tzinfo.zone
                return (
                    str(tzinfo).split("'")[1]
                    if "'" in str(tzinfo)
                    else default_timezone
                )
            except (AttributeError, IndexError):
                pass
        return default_timezone

    if isinstance(start_dt, datetime):
        start_timezone = get_timezone_name(start_dt.tzinfo)
        start_time = start_dt.isoformat()
    else:
        # All-day (date only)
        start_timezone = None
        start_time = start_dt.isoformat()

    if isinstance(end_dt, datetime):
        end_timezone = get_timezone_name(end_dt.tzinfo)
        end_time = end_dt.isoformat()
    else:
        # All-day (date only)
        end_timezone = None
        end_time = end_dt.isoformat()

    event_body = {
        "summary": "Busy",
        "start": (
            {
                "dateTime": start_time,
                "timeZone": start_timezone if start_timezone else default_timezone,
            }
            if start_timezone
            else {"date": start_time}
        ),
        "end": (
            {
                "dateTime": end_time,
                "timeZone": end_timezone if end_timezone else default_timezone,
            }
            if end_timezone
            else {"date": end_time}
        ),
        "transparency": "opaque",
        "extendedProperties": {
            "private": {
                "icloud_uid": event.vobject_instance.vevent.uid.value,
                "icloud_etag": event.etag,
            }
        },
    }

    if hasattr(event.vobject_instance.vevent, "rrule"):
        event_body["recurrence"] = convert_recurrence(event.vobject_instance.vevent)

    return event_body


def is_all_day_event(event):
    start_dt = event.vobject_instance.vevent.dtstart.value
    return not isinstance(start_dt, datetime)


print("Fetching iCloud events.")
calendars_events = {}
now_utc = datetime.now(pytz.timezone("UTC"))
future_date = now_utc + timedelta(days=DAYS_TO_SYNC)

for calendar in calendars:
    if calendar.name.startswith("Reminders"):
        continue
    if calendar.name in ICLOUD_CALENDARS_TO_SKIP:
        print(f"Skipping iCloud calendar: {calendar.name}")
        continue
    try:
        # Fetch events from iCloud
        events = calendar.date_search(start=now_utc, end=future_date)
        print(f"Processing calendar {calendar.name}: Retrieved {len(events)} events.")
        for event in events:
            event.load()
            props = event.get_properties([caldav.dav.GetEtag()])
            event.etag = props.get("{DAV:}getetag", None)
        calendars_events[calendar.name] = events
    except Exception as e:
        print(f"Could not fetch events for calendar {calendar.name}: {e}")
        continue

print("Adding iCloud events to Google Calendar...")
for calendar_name, events in calendars_events.items():
    print(f"Processing calendar: {calendar_name}")
    for event in events:
        if is_all_day_event(event) and calendar_name not in ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS:
            print(f"Ignoring all-day event in calendar {calendar_name}")
            continue
        obfuscated_event = obfuscate_event(event)
        try:
            service.events().insert(
                calendarId=GOOGLE_CALENDAR_ID,
                body=obfuscated_event
            ).execute()
        except Exception as e:
            print(f"Error creating event with UID {event.vobject_instance.vevent.uid.value}: {e}")

print("Synchronization complete.")

end_time = time.time()
elapsed_time = end_time - start_time
print(f"Script took {elapsed_time:.2f} seconds to run.")
