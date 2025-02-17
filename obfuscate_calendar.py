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


def convert_master_recurrence(vevent):
    """
    Convert a master (non-override) iCloud VEVENT's recurrence (RRULE & EXDATE) 
    to a Google API-compatible recurrence list.
    """
    recurrence = []

    # 1. Handle the main recurrence rule (RRULE)
    if hasattr(vevent, "rrule"):
        rrule = vevent.rrule.value
        recurrence.append(f"RRULE:{rrule}")

    # 2. Handle EXDATE (if any on the master)
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
    if isinstance(dt, datetime):
        tzid = exdate_params.get('TZID')
        if tzid:
            # e.g. EXDATE;TZID=America/Los_Angeles:20250124T143000
            return f"EXDATE;TZID={tzid}:{dt.strftime('%Y%m%dT%H%M%S')}"
        else:
            # Use UTC (Z) if it’s UTC or naive
            return f"EXDATE:{dt.strftime('%Y%m%dT%H%M%SZ')}"
    else:
        # It's a date
        return f"EXDATE;VALUE=DATE:{dt.strftime('%Y%m%d')}"


def obfuscate_vevent(vevent, etag):
    """
    Create an obfuscated event body from a single VEVENT (master or override).
    """
    default_timezone = "America/Los_Angeles"
    start_dt = vevent.dtstart.value
    end_dt = vevent.dtend.value if hasattr(vevent, "dtend") else None

    # Safeguard if dtend is missing, just add 1 hour
    if not end_dt:
        if isinstance(start_dt, datetime):
            end_dt = start_dt + timedelta(hours=1)
        else:
            end_dt = start_dt

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

    # Distinguish between datetime vs. date (all-day)
    if isinstance(start_dt, datetime):
        start_timezone = get_timezone_name(start_dt.tzinfo)
        start_time_iso = start_dt.isoformat()
    else:
        start_timezone = None
        start_time_iso = start_dt.isoformat()

    if isinstance(end_dt, datetime):
        end_timezone = get_timezone_name(end_dt.tzinfo)
        end_time_iso = end_dt.isoformat()
    else:
        end_timezone = None
        end_time_iso = end_dt.isoformat()

    event_body = {
        "summary": "Busy",  # obfuscated summary
        "start": (
            {
                "dateTime": start_time_iso,
                "timeZone": start_timezone or default_timezone,
            }
            if start_timezone
            else {"date": start_time_iso}
        ),
        "end": (
            {
                "dateTime": end_time_iso,
                "timeZone": end_timezone or default_timezone,
            }
            if end_timezone
            else {"date": end_time_iso}
        ),
        "transparency": "opaque",
        "extendedProperties": {
            "private": {
                "icloud_uid": vevent.uid.value,
                "icloud_etag": etag,
            }
        },
    }

    return event_body


def convert_overrides_to_exdates(master_rrule_list, overrides):
    """
    Take a master event's recurrence list (already containing RRULE & any EXDATE)
    and append additional EXDATE lines for each override's RECURRENCE-ID date.

    Returns the updated recurrence list.
    """
    if not master_rrule_list:
        master_rrule_list = []

    for override_vevent in overrides:
        # 'recurrence_id' is the *original* date/time for that instance
        if hasattr(override_vevent, "recurrence_id"):
            override_dt = override_vevent.recurrence_id.value
            override_params = getattr(override_vevent.recurrence_id, "params", {})
            exdate_str = _format_exdate(override_dt, override_params)
            master_rrule_list.append(exdate_str)

    return master_rrule_list


def is_all_day_vevent(vevent):
    """
    Check if the VEVENT is all-day by examining its dtstart value.
    """
    dtstart = vevent.dtstart.value
    return not isinstance(dtstart, datetime)


def fetch_icloud_events():
    """
    Fetch events from iCloud calendars (skipping those in the skip list), within the next
    DAYS_TO_SYNC days. Each event object may have multiple VEVENT components if it’s recurring
    with overrides.

    :return: Dictionary mapping calendar names to lists of caldav.Event objects.
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
    Insert obfuscated iCloud events into the Google Calendar, properly handling:
      - Master recurring VEVENT
      - Override VEVENTs with RECURRENCE-ID (converted to EXDATE + separate single events)
      - Skips all-day events in calendars not whitelisted for all-day.
    """
    print("Adding iCloud events to Google Calendar...")

    for calendar_name, events in calendars_events.items():
        print(f"Processing calendar: {calendar_name}")

        for event in events:
            # Split into sub-components: master vs. overrides
            vevents = [c for c in event.vobject_instance.components() if c.name == "VEVENT"]
            if not vevents:
                continue

            # Identify the master VEVENT (no RECURRENCE-ID) and any overrides
            master_vevent = None
            override_vevents = []

            for vevent in vevents:
                if hasattr(vevent, "recurrence_id"):
                    override_vevents.append(vevent)
                else:
                    master_vevent = vevent

            # If there's no master, it might be a single non-recurring event
            # or an odd case where everything is an override. Handle gracefully:
            if not master_vevent and len(override_vevents) == 1:
                # Possibly a single VEVENT that’s actually an override
                master_vevent = override_vevents[0]
                override_vevents = []

            # Obfuscate each sub-VEVENT accordingly
            if master_vevent:
                # Skip if master is an all-day in a non-whitelisted calendar
                if is_all_day_vevent(master_vevent) and calendar_name not in ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS:
                    print(f"Ignoring all-day recurring event in calendar {calendar_name}")
                    continue

                # Build the recurring event from the master
                master_body = obfuscate_vevent(master_vevent, event.etag)

                # If the master has an RRULE, convert it to a Google recurrence
                # plus incorporate exdates from the master:
                master_rrule_list = convert_master_recurrence(master_vevent)

                # Now add EXDATE for each override's original date/time
                master_rrule_list = convert_overrides_to_exdates(master_rrule_list, override_vevents)
                if master_rrule_list:
                    master_body["recurrence"] = master_rrule_list

                # Insert the recurring event
                try:
                    service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=master_body).execute()
                except Exception as e:
                    print(f"Error creating master recurring event with UID {master_vevent.uid.value}: {e}")

            # For each override, create a separate single event to reflect the changed times
            for ov_vevent in override_vevents:
                # Possibly skip if override is an all-day but not allowed
                if is_all_day_vevent(ov_vevent) and calendar_name not in ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS:
                    print(f"Ignoring all-day override event in calendar {calendar_name}")
                    continue

                single_body = obfuscate_vevent(ov_vevent, event.etag)
                # This single event is not recurring (it’s just one day)
                # so no 'recurrence' property is needed here.

                try:
                    service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=single_body).execute()
                except Exception as e:
                    print(f"Error creating override event with UID {ov_vevent.uid.value}: {e}")


def main():
    """
    Main script entry point. Authenticates with Google, fetches iCloud events first,
    then deletes existing Google events (if allowed), and adds iCloud events to Google.
    """
    start_time = time.time()

    # 1) Authenticate with Google
    creds = authenticate_google()
    service = build("calendar", "v3", credentials=creds)

    # 2) Fetch iCloud events BEFORE deleting Google events
    calendars_events = fetch_icloud_events()

    # 3) Optionally delete all events from the Google Calendar
    if GOOGLE_CALENDAR_ID not in GOOGLE_CALENDARS_TO_SKIP_DELETION:
        delete_all_events_from_google(service, GOOGLE_CALENDAR_ID)
    else:
        print(f"Skipping deletion for {GOOGLE_CALENDAR_ID} (in skip-deletion list).")

    # 4) Add iCloud events to Google Calendar
    add_icloud_events_to_google(service, calendars_events)

    # Wrap up timing
    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Script took {elapsed_time:.2f} seconds to run.")


if __name__ == "__main__":
    main()
