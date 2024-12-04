from datetime import datetime, timedelta
from pytz import timezone  # For handling time zones
import os
import pickle
import caldav
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import constants  # Contains ICLOUD_USERNAME, ICLOUD_PASSWORD, and GOOGLE_CALENDAR_ID
from google.auth.transport.requests import Request
import time
import logging

CALENDARS_TO_SKIP = constants.CALENDARS_TO_SKIP
CALENDARS_ALLOW_FULL_DAY_EVENTS = constants.CALENDARS_ALLOW_FULL_DAY_EVENTS

# Suppress warnings from the vobject library
logging.getLogger("root").setLevel(logging.ERROR)

start_time = time.time()

# Cache settings
CACHE_EXPIRATION_SECONDS = 3  # 5 minutes
ICLOUD_EVENTS_CACHE = "icloud_events_cache.pkl"
GOOGLE_EVENTS_CACHE = "google_event_map_cache.pkl"


def load_cache(cache_file, expiration_seconds):
    if os.path.exists(cache_file):
        cache_mtime = os.path.getmtime(cache_file)
        if time.time() - cache_mtime < expiration_seconds:
            with open(cache_file, "rb") as f:
                return pickle.load(f)
    return None


def save_cache(cache_file, data):
    with open(cache_file, "wb") as f:
        pickle.dump(data, f)


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


# Function to obfuscate event details and include iCloud ETag
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
                if hasattr(tzinfo, "zone"):
                    return tzinfo.zone
                # Handle non-standard timezones (e.g., _tzicalvtz)
                return (
                    str(tzinfo).split("'")[1]
                    if "'" in str(tzinfo)
                    else default_timezone
                )
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
                "icloud_etag": event.etag,  # Store the ETag
            }
        },
    }

    # Add recurrence if available
    if hasattr(event.vobject_instance.vevent, "rrule"):
        event_body["recurrence"] = convert_recurrence(event.vobject_instance.vevent)

    return event_body


# Function to check if an event is an all-day event
def is_all_day_event(event):
    start_dt = event.vobject_instance.vevent.dtstart.value
    return not isinstance(start_dt, datetime)


# Build a map of iCloud UIDs to Google Event IDs, including extendedProperties
def build_google_event_map():
    page_token = None
    google_events = {}
    now = datetime.utcnow().isoformat() + "Z"  # 'Z' indicates UTC time
    while True:
        events_result = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                pageToken=page_token,
                singleEvents=False,
                maxResults=2500,
                timeMin=now,  # Only get events starting from the current date
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
            else:
                # Optionally log events without icloud_uid for debugging
                pass
                # print(f"Event without icloud_uid: {event['id']} - {event.get('summary')}")
        page_token = events_result.get("nextPageToken")
        if not page_token:
            break
    return google_events


# Load or build the Google event map
google_event_map = load_cache(GOOGLE_EVENTS_CACHE, CACHE_EXPIRATION_SECONDS)
if google_event_map is not None:
    print("Using cached Google events.")
else:
    print("Fetching Google events.")
    google_event_map = build_google_event_map()
    save_cache(GOOGLE_EVENTS_CACHE, google_event_map)

# Load or fetch iCloud events
icloud_events_cache = load_cache(ICLOUD_EVENTS_CACHE, CACHE_EXPIRATION_SECONDS)
if icloud_events_cache is not None:
    print("Using cached iCloud events.")
    calendars_events = icloud_events_cache
else:
    print("Fetching iCloud events.")
    calendars_events = {}
    now = datetime.now(timezone("UTC"))
    future_date = now + timedelta(days=90)  # Adjust as needed
    for calendar in calendars:
        if calendar.name.startswith("Reminders"):
            continue
        if calendar.name in CALENDARS_TO_SKIP:
            # print(f"Skipping calendar: {calendar.name}")
            continue
        try:
            # Fetch events starting from the current date
            events = calendar.date_search(start=now, end=future_date)
            print(
                f"Processing calendar {calendar.name}: Retrieved {len(events)} events."
            )
            # Get ETag for each event
            for event in events:
                # Ensure the event is loaded
                event.load()
                # Fetch the ETag property
                props = event.get_properties([caldav.dav.GetEtag()])
                event.etag = props.get("{DAV:}getetag", None)
            calendars_events[calendar.name] = events
        except Exception as e:
            print(f"Could not fetch events for calendar {calendar.name}: {e}")
            continue
    save_cache(ICLOUD_EVENTS_CACHE, calendars_events)

# Keep track of iCloud UIDs processed
processed_icloud_uids = set()

# Process all iCloud calendars and their events
for calendar_name, events in calendars_events.items():
    print(f"Processing calendar: {calendar_name}")
    # Process each iCloud event
    for event in events:
        # Check if the event is an all-day event
        if is_all_day_event(event):
            # It's an all-day event
            if calendar_name not in CALENDARS_ALLOW_FULL_DAY_EVENTS:
                # Ignore the event
                print(f"Ignoring all-day event in calendar {calendar_name}")
                continue  # Skip to the next event
        # Proceed with processing the event
        icloud_uid = event.vobject_instance.vevent.uid.value
        icloud_etag = event.etag
        print(f"Processing iCloud event UID: {icloud_uid}")
        obfuscated_event = obfuscate_event(event)
        processed_icloud_uids.add(icloud_uid)
        if icloud_uid in google_event_map:
            # Retrieve the stored ETag from Google Calendar event
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

            # Update existing event in Google Calendar
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
                # Update google_event_map with new extendedProperties
                google_event_map[icloud_uid] = {
                    "id": updated_event["id"],
                    "extendedProperties": updated_event.get("extendedProperties"),
                }
            except Exception as e:
                print(f"Error updating event {google_event['id']}: {e}")
        else:
            # Create new event in Google Calendar
            print(f"Creating new event with icloud_uid: {icloud_uid}")
            try:
                created_event = (
                    service.events()
                    .insert(calendarId=GOOGLE_CALENDAR_ID, body=obfuscated_event)
                    .execute()
                )
                # Add to google_event_map
                google_event_map[icloud_uid] = {
                    "id": created_event["id"],
                    "extendedProperties": created_event.get("extendedProperties"),
                }
            except Exception as e:
                print(f"Error creating event with icloud_uid {icloud_uid}: {e}")

# Delete events from Google Calendar that no longer exist in iCloud
icloud_uids_in_google = set(google_event_map.keys())
icloud_uids_not_in_icloud = icloud_uids_in_google - processed_icloud_uids

for icloud_uid in icloud_uids_not_in_icloud:
    event_info = google_event_map[icloud_uid]
    event_id = event_info["id"]
    print(f"Deleting event with icloud_uid: {icloud_uid}")
    try:
        service.events().delete(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()
        # Remove from google_event_map
        del google_event_map[icloud_uid]
    except Exception as e:
        print(f"Error deleting event {event_id}: {e}")

# Save the updated Google event map
save_cache(GOOGLE_EVENTS_CACHE, google_event_map)

print("Synchronization complete.")

end_time = time.time()  # Record the end time
elapsed_time = end_time - start_time  # Calculate the elapsed time

print(f"Script took {elapsed_time:.2f} seconds to run.")
