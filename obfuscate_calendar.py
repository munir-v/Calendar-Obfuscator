import os
import pickle
import time
import logging
from datetime import datetime, timedelta
import pytz
import caldav

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request

import constants

logging.getLogger("root").setLevel(logging.ERROR)

# --- Global Constants and Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ICLOUD_CALENDARS_TO_SKIP = constants.ICLOUD_CALENDARS_TO_SKIP
ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS = constants.ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS
GOOGLE_CALENDARS_TO_SKIP_DELETION = constants.GOOGLE_CALENDARS_TO_SKIP_DELETION
GOOGLE_CALENDAR_ID = constants.GOOGLE_CALENDAR_ID

DAYS_TO_SYNC = 31

TOKEN_FILE = os.path.join(BASE_DIR, "token.pickle")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

ICLOUD_CLIENT = caldav.DAVClient(
    url="https://caldav.icloud.com/",
    username=constants.ICLOUD_USERNAME,
    password=constants.ICLOUD_PASSWORD,
)


def authenticate_google():
    """
    Authenticate with Google OAuth2 and return the credentials object.

    If valid credentials exist in the token file, use them. Otherwise, prompt the user
    to log in through OAuth. On success, save the credentials to the token file.
    """
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
                # Try to refresh existing credentials
                try:
                    creds.refresh(Request())
                except Exception as e:
                    print(f"Error refreshing token: {e}. Re-authentication required.")
                    os.remove(TOKEN_FILE)
                    creds = None

            if not creds:
                # No valid credentials, prompt user
                print("Authenticating with Google OAuth2...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    os.path.join(BASE_DIR, "credentials.json"), SCOPES
                )
                creds = flow.run_local_server(port=0)

                # Save the credentials for next time
                with open(TOKEN_FILE, "wb") as token:
                    pickle.dump(creds, token)
    except Exception as e:
        print(f"Failed to authenticate with Google: {e}")
        raise SystemExit("Exiting script due to authentication failure.")

    return creds


def delete_all_events_from_google(service, google_calendar_id):
    """
    Delete all existing events from a specified Google Calendar.

    :param service: Authenticated Google Calendar API service object.
    :param google_calendar_id: The ID of the Google Calendar to clear.
    """
    print("Deleting all existing events from Google Calendar...")
    page_token = None
    while True:
        events_result = service.events().list(
            calendarId=google_calendar_id,
            pageToken=page_token,
            maxResults=2500
        ).execute()
        events = events_result.get("items", [])

        for event in events:
            # Skip birthday or read-only events
            if event.get("eventType") == "birthday":
                continue
            try:
                service.events().delete(
                    calendarId=google_calendar_id,
                    eventId=event["id"]
                ).execute()
            except HttpError as e:
                # Error 410 means it's already gone
                if e.resp.status == 410:
                    print(f"Event {event['id']} is already deleted, skipping.")
                else:
                    raise

        page_token = events_result.get("nextPageToken")
        if not page_token:
            break

    print("All existing events deleted.")


def convert_recurrence(vevent):
    """
    Convert an iCloud event's recurrence (RRULE and EXDATE) to a
    Google API-compatible recurrence list.
    """
    recurrence = []

    # 1. Handle the main recurrence rule (RRULE)
    if hasattr(vevent, "rrule"):
        rrule = vevent.rrule.value
        recurrence.append(f"RRULE:{rrule}")

    # 2. Handle EXDATE
    if hasattr(vevent, "exdate"):
        exdates = vevent.exdate
        if not isinstance(exdates, list):
            exdates = [exdates]  # put it in a list for uniform handling

        for exd in exdates:
            if isinstance(exd.value, list):
                for dt in exd.value:
                    exdate_str = _format_exdate(dt, exd.params)
                    recurrence.append(exdate_str)
            else:
                dt = exd.value
                exdate_str = _format_exdate(dt, exd.params)
                recurrence.append(exdate_str)

    return recurrence


def _format_exdate(dt, exdate_params):
    """
    Format the EXDATE for Google. 
    exdate_params can contain a TZID, etc. 
    For all-day vs. date-time events, you'll need to handle carefully.
    """
    # If it's a date (no time), format as EXDATE;VALUE=DATE:YYYYMMDD
    # If it's a datetime, format as EXDATE;VALUE=DATE-TIME:YYYYMMDDTHHMMSSZ, with or without TZID
    # You can rely on iCal's default formatting or manually build them.

    # Example minimal approach (you may want to refine):
    if isinstance(dt, datetime):
        # If there's a timezone in exdate_params, include it
        tzid = exdate_params.get('TZID')
        # If tzid is present, you'll do something like:
        # "EXDATE;TZID=America/Los_Angeles:20230616T090000"
        if tzid:
            return f"EXDATE;TZID={tzid}:{dt.strftime('%Y%m%dT%H%M%S')}"
        else:
            # Use UTC (Z) if it’s UTC or naive
            # dt.isoformat() might produce microseconds, so strip them
            return f"EXDATE:{dt.strftime('%Y%m%dT%H%M%SZ')}"
    else:
        # it's a date
        return f"EXDATE;VALUE=DATE:{dt.strftime('%Y%m%d')}"


def obfuscate_event(event):
    """
    Create an obfuscated event body suitable for Google Calendar insertion.

    :param event: A caldav Event object with vobject instance data.
    :return: Dictionary representing the event body for the Google Calendar API.
    """
    default_timezone = "America/Los_Angeles"
    start_dt = event.vobject_instance.vevent.dtstart.value
    end_dt = event.vobject_instance.vevent.dtend.value

    def get_timezone_name(tzinfo):
        """
        Extract a time zone name from the provided tzinfo. Falls back to default_timezone
        if none can be determined.
        """
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
        start_time_iso = start_dt.isoformat()
    else:
        # All-day (date only)
        start_timezone = None
        start_time_iso = start_dt.isoformat()

    if isinstance(end_dt, datetime):
        end_timezone = get_timezone_name(end_dt.tzinfo)
        end_time_iso = end_dt.isoformat()
    else:
        # All-day (date only)
        end_timezone = None
        end_time_iso = end_dt.isoformat()

    event_body = {
        "summary": "Busy",
        "start": (
            {
                "dateTime": start_time_iso,
                "timeZone": start_timezone if start_timezone else default_timezone,
            }
            if start_timezone
            else {"date": start_time_iso}
        ),
        "end": (
            {
                "dateTime": end_time_iso,
                "timeZone": end_timezone if end_timezone else default_timezone,
            }
            if end_timezone
            else {"date": end_time_iso}
        ),
        "transparency": "opaque",
        "extendedProperties": {
            "private": {
                "icloud_uid": event.vobject_instance.vevent.uid.value,
                "icloud_etag": event.etag,
            }
        },
    }

    # Handle recurrence
    if hasattr(event.vobject_instance.vevent, "rrule"):
        event_body["recurrence"] = convert_recurrence(event.vobject_instance.vevent)

    return event_body


def is_all_day_event(event):
    """
    Determine if the iCloud event is an all-day event.

    :param event: A caldav Event object with vobject instance data.
    :return: True if the event has a date-only start time, otherwise False.
    """
    start_dt = event.vobject_instance.vevent.dtstart.value
    return not isinstance(start_dt, datetime)


def fetch_icloud_events():
    """
    Fetch events from iCloud calendars (skipping those in the skip list), within the next
    DAYS_TO_SYNC days. Event objects have their etag loaded for use in extended properties
    when creating Google events.

    :return: Dictionary mapping calendar names to lists of caldav Event objects.
    """
    calendars_events = {}
    principal = ICLOUD_CLIENT.principal()
    calendars = principal.calendars()

    now_utc = datetime.now(pytz.timezone("UTC"))
    future_date = now_utc + timedelta(days=DAYS_TO_SYNC)

    print("Fetching iCloud events.")
    for calendar in calendars:
        # Skip reminders and calendars in the skip list
        if calendar.name.startswith("Reminders"):
            continue
        if calendar.name in ICLOUD_CALENDARS_TO_SKIP:
            print(f"Skipping iCloud calendar: {calendar.name}")
            continue

        try:
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

    return calendars_events


def add_icloud_events_to_google(service, calendars_events):
    """
    Insert obfuscated iCloud events into the Google Calendar.

    :param service: Authenticated Google Calendar API service object.
    :param calendars_events: Dictionary of {calendar_name: [caldav Event, ...]}.
    """
    print("Adding iCloud events to Google Calendar...")
    for calendar_name, events in calendars_events.items():
        print(f"Processing calendar: {calendar_name}")
        for event in events:
            # Skip all-day events if the calendar is not allowed to have them
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
                uid = event.vobject_instance.vevent.uid.value
                print(f"Error creating event with UID {uid}: {e}")


def main():
    """
    Main script entry point. Authenticates with Google, deletes existing events (if allowed),
    fetches iCloud events, and adds them to the Google Calendar with obfuscation.
    """
    start_time = time.time()

    # Authenticate with Google
    creds = authenticate_google()
    service = build("calendar", "v3", credentials=creds)

    # 1) Optionally delete all events from the Google Calendar
    if GOOGLE_CALENDAR_ID not in GOOGLE_CALENDARS_TO_SKIP_DELETION:
        delete_all_events_from_google(service, GOOGLE_CALENDAR_ID)
    else:
        print(f"Skipping deletion for {GOOGLE_CALENDAR_ID} (in skip-deletion list).")

    # 2) Fetch iCloud events
    calendars_events = fetch_icloud_events()

    # 3) Add iCloud events to Google Calendar
    add_icloud_events_to_google(service, calendars_events)

    # Wrap up timing
    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Script took {elapsed_time:.2f} seconds to run.")


if __name__ == "__main__":
    main()