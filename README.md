# Calendar-Obfuscator

### Description


### Setup
Create a `contants.py` file in the root directory of the project.

To use the iCloud API, you will need to generate an app-specific password from Apple:

[Apple Account Management Page](https://account.apple.com/account/manage) > App-Specific Passwords > New password

In the `contants.py` file, add the following:
`ICLOUD_USERNAME = "Your Apple ID Email"`
`ICLOUD_PASSWORD = "Newly Generated App Password"`

In the `contants.py` file, set the `GOOGLE_CALENDAR_ID` to the ID of the Google Calendar you want to use. You can find the ID by going to the Google Calendar settings and scrolling down to the "Integrate Calendar" section.

Create a Google Cloud Platform project and enable the Google Calendar API. Create a service account and download the JSON key file.
https://console.cloud.google.com/

Create `CALENDARS_TO_SKIP` and `CALENDARS_ALLOW_FULL_DAY_EVENTS` list in the `contants.py` file. Add the names of the calendars you want to skip and the names of the calendars that should have full-day events.