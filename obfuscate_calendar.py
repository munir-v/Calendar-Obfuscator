from datetime import datetime, date
from pytz import timezone  # For handling time zones
import os
import pickle
import caldav
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import constants  # Contains ICLOUD_USERNAME and ICLOUD_PASSWORD
from google.auth.transport.requests import Request
import time

start_time = time.time()

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

# Define the Google Calendar ID (adjust as needed)
GOOGLE_CALENDAR_ID = constants.GOOGLE_CALENDAR_ID

# Function to convert iCloud recurrence rules to Google Calendar format
def convert_recurrence(event):
    recurrence = []
    if hasattr(event, "rrule"):
        # Access the recurrence rule directly
        rrule = event.rrule.value
        # Convert the rule to a string format acceptable by Google Calendar
        recurrence.append(f"RRULE:{rrule}")
    return recurrence

# Function to obfuscate event details
def obfuscate_event(event):
    # Default to PST if no timezone is found
    default_timezone = "America/Los_Angeles"  # PST timezone identifier
    start_dt = event.vobject_instance.vevent.dtstart.value
    end_dt = event.vobject_instance.vevent.dtend.value

    # Function to extract timezone name
    def get_timezone_name(tzinfo):
        if tzinfo:
            try:
                # Extract standard timezone name
                if hasattr(tzinfo, 'zone'):
                    return tzinfo.zone
                # Handle non-standard timezones (e.g., _tzicalvtz)
                return str(tzinfo).split("'")[1] if "'" in str(tzinfo) else default_timezone
            except (AttributeError, IndexError):
                pass
        return default_timezone

    # Handle start and end times
    if isinstance(start_dt, datetime):
        start_timezone = get_timezone_name(start_dt.tzinfo)
        start_time = start_dt.isoformat()
    else:
        start_timezone = None  # No timezone for all-day events
        start_time = start_dt.isoformat()

    if isinstance(end_dt, datetime):
        end_timezone = get_timezone_name(end_dt.tzinfo)
        end_time = end_dt.isoformat()
    else:
        end_timezone = None  # No timezone for all-day events
        end_time = end_dt.isoformat()

    event_body = {
        "summary": "Busy",
        "start": {
            "dateTime": start_time,
            "timeZone": start_timezone if start_timezone else default_timezone,
        } if start_timezone else {"date": start_time},
        "end": {
            "dateTime": end_time,
            "timeZone": end_timezone if end_timezone else default_timezone,
        } if end_timezone else {"date": end_time},
        "transparency": "opaque",
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
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            pageToken=page_token,
            singleEvents=False
        ).execute()
        for event in events_result.get("items", []):
            icloud_uid = event.get("extendedProperties", {}).get("private", {}).get("icloud_uid")
            if icloud_uid:
                google_events[icloud_uid] = event["id"]
            else:
                # Optionally log events without icloud_uid for debugging
                print(f"Event without icloud_uid: {event['id']} - {event.get('summary')}")
        page_token = events_result.get("nextPageToken")
        if not page_token:
            break
    return google_events

# Build the Google event map once before processing calendars
google_event_map = build_google_event_map()

# Keep track of iCloud UIDs processed
processed_icloud_uids = set()

# Process all iCloud calendars
for calendar in calendars:
    if calendar.name.startswith("Reminders"):
        continue
    print(f"Processing calendar: {calendar.name}")
    try:
        events = calendar.events()
        print(f"Retrieved {len(events)} events from iCloud calendar {calendar.name}")
    except Exception as e:
        print(f"Could not fetch events for calendar {calendar.name}: {e}")
        continue

    # Process each iCloud event
    for event in events:
        icloud_uid = event.vobject_instance.vevent.uid.value
        print(f"Processing iCloud event UID: {icloud_uid}")
        obfuscated_event = obfuscate_event(event)
        processed_icloud_uids.add(icloud_uid)
        if icloud_uid in google_event_map:
            # Update existing event in Google Calendar
            event_id = google_event_map[icloud_uid]
            print(f"Updating event: {event_id} with icloud_uid: {icloud_uid}")
            try:
                service.events().update(
                    calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=obfuscated_event
                ).execute()
                # Remove from map to avoid deletion later
                del google_event_map[icloud_uid]
            except Exception as e:
                print(f"Error updating event {event_id}: {e}")
        else:
            # Create new event in Google Calendar
            print(f"Creating new event with icloud_uid: {icloud_uid}")
            try:
                created_event = service.events().insert(
                    calendarId=GOOGLE_CALENDAR_ID, body=obfuscated_event
                ).execute()
            except Exception as e:
                print(f"Error creating event with icloud_uid {icloud_uid}: {e}")

# Delete events from Google Calendar that no longer exist in iCloud
for icloud_uid, event_id in google_event_map.items():
    print(f"Deleting event with icloud_uid: {icloud_uid}")
    try:
        service.events().delete(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()
    except Exception as e:
        print(f"Error deleting event {event_id}: {e}")

print("Synchronization complete.")

end_time = time.time()  # Record the end time
elapsed_time = end_time - start_time  # Calculate the elapsed time

print(f"Script took {elapsed_time:.2f} seconds to run.")
