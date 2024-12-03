import os
import pickle
import caldav
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import constants  # Contains ICLOUD_USERNAME and ICLOUD_PASSWORD
from google.auth.transport.requests import Request
import pickle
import os
from google_auth_oauthlib.flow import InstalledAppFlow


# Connect to iCloud via CalDAV
client = caldav.DAVClient(
    url="https://caldav.icloud.com/",
    username=constants.ICLOUD_USERNAME,
    password=constants.ICLOUD_PASSWORD,
)
principal = client.principal()
calendars = principal.calendars()


TOKEN_FILE = "token.pickle"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def authenticate_google():
    creds = None
    # Load credentials from the token file if it exists
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)
    # If no valid credentials, authenticate and save them
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)
    return creds


# Authenticate and build the Google Calendar service
creds = authenticate_google()
service = build("calendar", "v3", credentials=creds)

# Define the Google Calendar ID (adjust as needed, 'primary' is the default)
GOOGLE_CALENDAR_ID = constants.GOOGLE_CALENDAR_ID


# Function to convert iCloud recurrence rules to Google Calendar format
def convert_recurrence(event):
    recurrence = []
    if hasattr(event, "rrule"):
        rrule = event.rrule.to_ical().decode("utf-8").strip()
        recurrence.append(f"RRULE:{rrule}")
    return recurrence


# Function to obfuscate event details
def obfuscate_event(event):
    event_body = {
        "summary": "Busy",
        "start": {"dateTime": event.vobject_instance.vevent.dtstart.value.isoformat()},
        "end": {"dateTime": event.vobject_instance.vevent.dtend.value.isoformat()},
        "transparency": "opaque",
        # Store the iCloud event UID in extended properties
        "extendedProperties": {
            "private": {"icloud_uid": event.vobject_instance.vevent.uid.value}
        },
    }
    # Add recurrence if available
    if hasattr(event.vobject_instance.vevent, "rrule"):
        event_body["recurrence"] = convert_recurrence(event.vobject_instance.vevent)
    return event_body


# Build a map of iCloud UIDs to Google Event IDs
def build_google_event_map():
    page_token = None
    google_events = {}
    while True:
        events_result = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                pageToken=page_token,
                privateExtendedProperty="icloud_uid=*",
            )
            .execute()
        )
        for event in events_result.get("items", []):
            icloud_uid = event["extendedProperties"]["private"].get("icloud_uid")
            if icloud_uid:
                google_events[icloud_uid] = event["id"]
        page_token = events_result.get("nextPageToken")
        if not page_token:
            break
    return google_events


# Process all iCloud calendars
for calendar in calendars:
    if calendar.name.startswith("Reminders"):
        continue
    elif not calendar.name.startswith("Deep"):
        continue
    print(f"Processing calendar: {calendar.name}")
    try:
        events = calendar.events()
        print(f"Retrieved {len(events)} events from iCloud calendar {calendar.name}")
    except Exception as e:
        print(f"Could not fetch events for calendar {calendar.name}: {e}")
        continue

    google_event_map = build_google_event_map()

    # Process each iCloud event
    for event in events:
        # print(f"Processing iCloud event: {event.vobject_instance.vevent.summary.value}")
        icloud_uid = event.vobject_instance.vevent.uid.value
        obfuscated_event = obfuscate_event(event)
        # print(f"Obfuscated event: {obfuscated_event}")
        if icloud_uid in google_event_map:
            # Update existing event in Google Calendar
            event_id = google_event_map[icloud_uid]
            service.events().update(
                calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=obfuscated_event
            ).execute()
            del google_event_map[icloud_uid]
        else:
            # Create new event in Google Calendar
            created_event = (
                service.events()
                .insert(calendarId=GOOGLE_CALENDAR_ID, body=obfuscated_event)
                .execute()
            )
            print(f"Created event in Google Calendar: {created_event}")

    # Delete events from Google Calendar that no longer exist in iCloud
    for icloud_uid, event_id in google_event_map.items():
        service.events().delete(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()

print("Synchronization complete.")
