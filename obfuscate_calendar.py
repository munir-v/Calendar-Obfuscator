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

DAYS_TO_SYNC = 10

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

if GOOGLE_CALENDAR_ID not in GOOGLE_CALENDARS_TO_SKIP_DELETION:
    now = datetime.now(timezone.utc).isoformat()
    page_token = None
    while True:
        past_events = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMax=now,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            )
            .execute()
        )

        for event in past_events.get("items", []):
            service.events().delete(
                calendarId=GOOGLE_CALENDAR_ID, eventId=event["id"]
            ).execute()

        page_token = past_events.get("nextPageToken")
        if not page_token:
            break
else:
    print(f"Skipping deletion of past events for {GOOGLE_CALENDAR_ID} "
          f"because it is in GOOGLE_CALENDARS_TO_SKIP_DELETION.")


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
        start_timezone = None
        start_time = start_dt.isoformat()

    if isinstance(end_dt, datetime):
        end_timezone = get_timezone_name(end_dt.tzinfo)
        end_time = end_dt.isoformat()
    else:
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


def build_google_event_map():
    page_token = None
    google_events = {}
    now = datetime.now(timezone.utc).isoformat()
    while True:
        events_result = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                pageToken=page_token,
                singleEvents=False,
                maxResults=2500,
                timeMin=now,
            )
            .execute()
        )
        for event in events_result.get("items", []):
            icloud_uid = (
                event.get("extendedProperties", {}).get("private", {}).get("icloud_uid")
            )
            if icloud_uid:
                google_events[icloud_uid] = {
                    "id": event["id"],
                    "extendedProperties": event.get("extendedProperties"),
                }
        page_token = events_result.get("nextPageToken")
        if not page_token:
            break
    return google_events


print("Fetching Google events.")
google_event_map = build_google_event_map()

print("Fetching iCloud events.")
calendars_events = {}
now = datetime.now(pytz.timezone("UTC"))
future_date = now + timedelta(days=DAYS_TO_SYNC)
for calendar in calendars:
    if calendar.name.startswith("Reminders"):
        continue
    if calendar.name in ICLOUD_CALENDARS_TO_SKIP:
        print(f"Skipping iCloud calendar: {calendar.name}")
        continue
    try:
        events = calendar.date_search(start=now, end=future_date)
        print(f"Processing calendar {calendar.name}: Retrieved {len(events)} events.")
        for event in events:
            event.load()
            props = event.get_properties([caldav.dav.GetEtag()])
            event.etag = props.get("{DAV:}getetag", None)
        calendars_events[calendar.name] = events
    except Exception as e:
        print(f"Could not fetch events for calendar {calendar.name}: {e}")
        continue

processed_icloud_uids = set()

for calendar_name, events in calendars_events.items():
    print(f"Processing calendar: {calendar_name}")
    for event in events:
        if is_all_day_event(event):
            if calendar_name not in ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS:
                print(f"Ignoring all-day event in calendar {calendar_name}")
                continue
        icloud_uid = event.vobject_instance.vevent.uid.value
        icloud_etag = event.etag
        print(f"Processing iCloud event UID: {icloud_uid}")
        obfuscated_event = obfuscate_event(event)
        processed_icloud_uids.add(icloud_uid)
        if icloud_uid in google_event_map:
            google_event = google_event_map[icloud_uid]
            google_etag = (
                google_event.get("extendedProperties", {})
                .get("private", {})
                .get("icloud_etag")
            )

            if icloud_etag == google_etag:
                print(
                    f"No changes detected for event {google_event['id']}; skipping update."
                )
                continue

            print(f"Updating event: {google_event['id']} with icloud_uid: {icloud_uid}")
            try:
                updated_event = (
                    service.events()
                    .update(
                        calendarId=GOOGLE_CALENDAR_ID,
                        eventId=google_event["id"],
                        body=obfuscated_event,
                    )
                    .execute()
                )
                google_event_map[icloud_uid] = {
                    "id": updated_event["id"],
                    "extendedProperties": updated_event.get("extendedProperties"),
                }
            except Exception as e:
                print(f"Error updating event {google_event['id']}: {e}")
        else:
            print(f"Creating new event with icloud_uid: {icloud_uid}")
            try:
                created_event = (
                    service.events()
                    .insert(calendarId=GOOGLE_CALENDAR_ID, body=obfuscated_event)
                    .execute()
                )
                google_event_map[icloud_uid] = {
                    "id": created_event["id"],
                    "extendedProperties": created_event.get("extendedProperties"),
                }
            except Exception as e:
                print(f"Error creating event with icloud_uid {icloud_uid}: {e}")

icloud_uids_in_google = set(google_event_map.keys())
icloud_uids_not_in_icloud = icloud_uids_in_google - processed_icloud_uids

if GOOGLE_CALENDAR_ID not in GOOGLE_CALENDARS_TO_SKIP_DELETION:
    for icloud_uid in icloud_uids_not_in_icloud:
        event_info = google_event_map[icloud_uid]
        event_id = event_info["id"]
        print(f"Deleting event with icloud_uid: {icloud_uid}")
        try:
            service.events().delete(
                calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
            ).execute()
            del google_event_map[icloud_uid]
        except Exception as e:
            print(f"Error deleting event {event_id}: {e}")
else:
    print(f"Skipping deletion of orphan events for {GOOGLE_CALENDAR_ID} "
          f"because it is in GOOGLE_CALENDARS_TO_SKIP_DELETION.")

print("Synchronization complete.")

end_time = time.time()
elapsed_time = end_time - start_time
print(f"Script took {elapsed_time:.2f} seconds to run.")
