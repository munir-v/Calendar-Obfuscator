# Calendar-Obfuscator

### Description


### Setup
Create a `contants.py` file in the root directory of the project.

To use the iCloud API, you will need to generate an app-specific password from Apple:
- [Apple Account Management Page](https://account.apple.com/account/manage) > App-Specific Passwords > New password

In the `contants.py` file, add the following:
`ICLOUD_USERNAME = "Your Apple ID Email"`
`ICLOUD_PASSWORD = "Newly Generated App Password"`

In the `contants.py` file, set the `GOOGLE_CALENDAR_ID` to the ID of the Google Calendar you want to use. You can find the ID by going to the Google Calendar settings and scrolling down to the "Integrate Calendar" section.

Create a Google Cloud Platform project and enable the Google Calendar API. Create a service account and download the JSON key file:
- Open the (Google Cloud Console)[https://console.cloud.google.com/]:
- Create a New Project.
- Enable the Google Calendar API (APIs & Services > Library).
- Search for Google Calendar API in the search bar.
- Set Up OAuth Consent Screen:
- In the left-hand menu, go to APIs & Services > OAuth consent screen.
- Choose External. Under Scopes, you can add the required scope for Google Calendar:
- https://www.googleapis.com/auth/calendar
- Go to APIs & Services > Credentials.
- Click Create Credentials and select OAuth 2.0 Client IDs.
- Choose Application Type as Desktop App.
- Download the JSON Credentials File. Name it `credentials.json` and move the file to your script's directory.

Create `CALENDARS_TO_SKIP` and `CALENDARS_ALLOW_FULL_DAY_EVENTS` list in the `contants.py` file. Add the names of the calendars you want to skip and the names of the calendars that should have full-day events.