import caldav
from google.oauth2 import credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from datetime import datetime, timedelta

# Constants for iCloud authentication
import constants  # Contains ICLOUD_USERNAME and ICLOUD_PASSWORD

# Connect to iCloud via CalDAV
client = caldav.DAVClient(
    url='https://caldav.icloud.com/',
    username=constants.ICLOUD_USERNAME,
    password=constants.ICLOUD_PASSWORD
)
principal = client.principal()
calendars = principal.calendars()

# Authenticate with Google Calendar API
SCOPES = ['https://www.googleapis.com/auth/calendar']
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=0)
service = build('calendar', 'v3', credentials=creds)

# Define the Google Calendar ID (adjust as needed, 'primary' is the default)
GOOGLE_CALENDAR_ID = 'primary'


# Function to obfuscate event details
def obfuscate_event(event):
    return {
        'summary': 'Busy',
        'start': {'dateTime': event.vobject_instance.vevent.dtstart.value.isoformat()},
        'end': {'dateTime': event.vobject_instance.vevent.dtend.value.isoformat()},
        'transparency': 'opaque',
        # Store the iCloud event UID in extended properties
        'extendedProperties': {
            'private': {
                'icloud_uid': event.vobject_instance.vevent.uid.value
            }
        }
    }


# Build a map of iCloud UIDs to Google Event IDs
def build_google_event_map():
    page_token = None
    google_events = {}
    while True:
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            pageToken=page_token,
            privateExtendedProperty='icloud_uid=*'
        ).execute()
        for event in events_result.get('items', []):
            icloud_uid = event['extendedProperties']['private'].get('icloud_uid')
            if icloud_uid:
                google_events[icloud_uid] = event['id']
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break
    return google_events


# Process all iCloud calendars
for calendar in calendars:
    print(f"Processing calendar: {calendar.name}")
    try:
        events = calendar.events()
    except Exception as e:
        print(f"Could not fetch events for calendar {calendar.name}: {e}")
        continue

    google_event_map = build_google_event_map()

    # Process each iCloud event
    for event in events:
        icloud_uid = event.vobject_instance.vevent.uid.value
        obfuscated_event = obfuscate_event(event)
        if icloud_uid in google_event_map:
            # Update existing event in Google Calendar
            event_id = google_event_map[icloud_uid]
            service.events().update(
                calendarId=GOOGLE_CALENDAR_ID,
                eventId=event_id,
                body=obfuscated_event
            ).execute()
            del google_event_map[icloud_uid]
        else:
            # Create new event in Google Calendar
            service.events().insert(
                calendarId=GOOGLE_CALENDAR_ID,
                body=obfuscated_event
            ).execute()

    # Delete events from Google Calendar that no longer exist in iCloud
    for icloud_uid, event_id in google_event_map.items():
        service.events().delete(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id
        ).execute()

print("Synchronization complete.")
