# Calendar-Obfuscator

### Description
Integrating your calendar with services like Calendly or Zoom requires granting full access to read, write, and delete your calendar events. This script creates an obfuscated copy of your iCloud calendar, sharing it with a Google Calendar account, which can then be shared with third-party services. 

The script copies all events from your iCloud calendar from the present to a future date to your Google Calendar, deleting all events before the present. The script also obfuscates the event titles and descriptions.

You can run the script manually or set up a cron job to run it automatically at a set interval.

### Google Calendar API Setup
Create a Google Cloud Platform project and enable the Google Calendar API. Create a service account and download the JSON key file:
- Open the (Google Cloud Console)[https://console.cloud.google.com/]:
- Create a New Project.
- Enable the Google Calendar API (APIs & Services > Library).
- Search for Google Calendar API in the search bar.
- Set Up OAuth Consent Screen:
- In the left-hand menu, go to APIs & Services > OAuth consent screen.
- Choose External. Under Scopes, you can add the required scope for Google Calendar: https://www.googleapis.com/auth/calendar
- Go to APIs & Services > Credentials.
- Click Create Credentials and select OAuth 2.0 Client IDs.
- Choose Application Type as Desktop App.
- Download the JSON Credentials File. Name it `credentials.json` and move the file to your script's directory.

### Setup `constants.py`
Create a `constants.py` file in the root directory of the project.

In the `constants.py` file, add the following:

`ICLOUD_USERNAME = "Your Apple ID Email"`

`ICLOUD_PASSWORD = "Newly Generated App Password"`

To generate an app-specific password from Apple (for iCloud calendar API access):
- Go to the [Apple Account Management Page](https://account.apple.com/account/manage) > App-Specific Passwords > New password

In the `constants.py` file, set the `GOOGLE_CALENDAR_ID` to the ID of the [Google Calendar](calendar.google.com) you want to use. You can find the ID by going to the settings for that calendar and scrolling down to the "Integrate Calendar" section.

Create `CALENDARS_TO_SKIP` and `CALENDARS_ALLOW_FULL_DAY_EVENTS` lists in the `constants.py` file. Add the names of the iCloud calendars you want the script to ignore to the first list, and the names of the calendars that should add full-day events to your Google Calendar in the second.

### Example `constants.py` file (found in the `example_constants.py` file):
```python
ICLOUD_USERNAME = "example@icloud.com"
ICLOUD_PASSWORD = "Newly Generated App Password"
GOOGLE_CALENDAR_ID = "example@gmail.com"
CALENDARS_TO_SKIP = ["Calendar 1", "Calendar 2"]
CALENDARS_ALLOW_FULL_DAY_EVENTS = ["Calendar 3"]
```