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

DAYS_TO_SYNC = 31

TOKEN_FILE = os.path.join(BASE_DIR, "token.pickle")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

ICLOUD_CLIENT = caldav.DAVClient(
    url="https://caldav.icloud.com/",
    username=constants.ICLOUD_USERNAME,
    password=constants.ICLOUD_PASSWORD,
)


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
                    print(f"Error refreshing token: {e}. Re-authentication required.")
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


def create_new_google_calendar(service):
    print("Creating new Google calendar 'Obfuscated iCloud Calendar'...")
    calendar_body = {
        "summary": "Obfuscated iCloud Calendar",
    }
    created_calendar = service.calendars().insert(body=calendar_body).execute()
    new_calendar_id = created_calendar["id"]
    print(f"Created new calendar with ID: {new_calendar_id}")
    return new_calendar_id


def delete_calendar(service, calendar_id):
    try:
        service.calendars().delete(calendarId=calendar_id).execute()
        print(f"Deleted calendar: {calendar_id}")
    except HttpError as e:
        # If it's a 403 'cannotDeletePrimaryCalendar', we clear instead
        if e.resp.status == 403 and "cannotDeletePrimaryCalendar" in str(e):
            print(f"Skipping primary calendar: {calendar_id}")
        else:
            print(f"Error deleting calendar {calendar_id}: {e}")


def convert_master_recurrence(vevent):
    recurrence = []
    if hasattr(vevent, "rrule"):
        rrule = vevent.rrule.value
        recurrence.append(f"RRULE:{rrule}")

    if hasattr(vevent, "exdate"):
        exdates = vevent.exdate
        if not isinstance(exdates, list):
            exdates = [exdates]

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
    if isinstance(dt, datetime):
        tzid = exdate_params.get('TZID')
        if tzid:
            return f"EXDATE;TZID={tzid}:{dt.strftime('%Y%m%dT%H%M%S')}"
        else:
            return f"EXDATE:{dt.strftime('%Y%m%dT%H%M%SZ')}"
    else:
        return f"EXDATE;VALUE=DATE:{dt.strftime('%Y%m%d')}"


def obfuscate_vevent(vevent, etag):
    default_timezone = "America/Los_Angeles"
    start_dt = vevent.dtstart.value
    end_dt = vevent.dtend.value if hasattr(vevent, "dtend") else None

    if not end_dt:
        if isinstance(start_dt, datetime):
            end_dt = start_dt + timedelta(hours=1)
        else:
            end_dt = start_dt

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
        "summary": "Busy",
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
    if not master_rrule_list:
        master_rrule_list = []
    for override_vevent in overrides:
        if hasattr(override_vevent, "recurrence_id"):
            override_dt = override_vevent.recurrence_id.value
            override_params = getattr(override_vevent.recurrence_id, "params", {})
            exdate_str = _format_exdate(override_dt, override_params)
            master_rrule_list.append(exdate_str)
    return master_rrule_list


def is_all_day_vevent(vevent):
    dtstart = vevent.dtstart.value
    return not isinstance(dtstart, datetime)


def fetch_icloud_events():
    calendars_events = {}
    principal = ICLOUD_CLIENT.principal()
    calendars = principal.calendars()

    now_utc = datetime.now(pytz.timezone("UTC"))
    future_date = now_utc + timedelta(days=31)

    print("Fetching iCloud events...")
    for calendar in calendars:
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


def add_icloud_events_to_google(service, calendars_events, target_calendar_id):
    print("Adding iCloud events to Google Calendar...")

    for calendar_name, events in calendars_events.items():
        print(f"Processing calendar: {calendar_name}")

        for event in events:
            vevents = [c for c in event.vobject_instance.components() if c.name == "VEVENT"]
            if not vevents:
                continue

            master_vevent = None
            override_vevents = []

            for vevent in vevents:
                if hasattr(vevent, "recurrence_id"):
                    override_vevents.append(vevent)
                else:
                    master_vevent = vevent

            # If there's no master, fallback to the single VEVENT
            if not master_vevent and len(override_vevents) == 1:
                master_vevent = override_vevents[0]
                override_vevents = []

            # Handle master
            if master_vevent:
                if is_all_day_vevent(master_vevent) and calendar_name not in ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS:
                    print(f"Ignoring all-day recurring event in calendar {calendar_name}")
                    continue

                master_body = obfuscate_vevent(master_vevent, event.etag)
                master_rrule_list = convert_master_recurrence(master_vevent)
                master_rrule_list = convert_overrides_to_exdates(master_rrule_list, override_vevents)
                if master_rrule_list:
                    master_body["recurrence"] = master_rrule_list

                try:
                    service.events().insert(calendarId=target_calendar_id, body=master_body).execute()
                except Exception as e:
                    print(f"Error creating master recurring event with UID {master_vevent.uid.value}: {e}")

            # Handle overrides
            for ov_vevent in override_vevents:
                if is_all_day_vevent(ov_vevent) and calendar_name not in ICLOUD_CALENDARS_ALLOW_FULL_DAY_EVENTS:
                    print(f"Ignoring all-day override event in calendar {calendar_name}")
                    continue

                single_body = obfuscate_vevent(ov_vevent, event.etag)
                try:
                    service.events().insert(calendarId=target_calendar_id, body=single_body).execute()
                except Exception as e:
                    print(f"Error creating override event with UID {ov_vevent.uid.value}: {e}")


def main():
    start_time = time.time()

    # 1) Authenticate with Google
    creds = authenticate_google()
    service = build("calendar", "v3", credentials=creds)

    # 2) Fetch iCloud events
    calendars_events = fetch_icloud_events()

    # 3) Create new Google calendar for obfuscated events
    new_calendar_id = create_new_google_calendar(service)

    # 4) Insert iCloud events into the new calendar
    add_icloud_events_to_google(service, calendars_events, new_calendar_id)

    # 5) Fetch all user calendars
    user_calendars = service.calendarList().list().execute().get("items", [])

    for cal in user_calendars:
        cal_id = cal["id"]
        if cal_id in GOOGLE_CALENDARS_TO_SKIP_DELETION:
            print(f"Skipping deletion for calendar {cal_id} (in skip-deletion list).")
            continue
        if cal_id == new_calendar_id:
            print(f"Skipping newly created calendar {cal_id}.")
            continue

        delete_calendar(service, cal_id)

    elapsed_time = time.time() - start_time
    print(f"Script took {elapsed_time:.2f} seconds to run.")


if __name__ == "__main__":
    main()