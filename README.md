# Handball calendar UHC Gänserndorf

`gaenserndorf_calendar.py` reads the matches of selected UHC Gänserndorf youth
teams from the ÖHB nuLiga portal and writes them to `gaenserndorf.ics`.
A GitHub Action runs the script twice a day and commits the file when a match changes.

Subscribe in Google Calendar (*Other calendars → From URL*):

    https://raw.githubusercontent.com/pglira/handball-calendar/main/gaenserndorf.ics
