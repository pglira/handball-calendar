#!/usr/bin/env python3
"""Build an iCalendar (.ics) file with the handball matches of selected
UHC Gänserndorf youth teams.

Data source: the ÖHB nuLiga portal (oehb-handball.liga.nu).

- League and ÖHB Elite Cup matches come from the club match list
  ("clubMeetings") of the current season (1 July to 30 June).
- U9 teams play tournaments. Their dates come from the group page
  "NÖ U9x alle Termine und Turnierleiter". A tournament belongs to the team
  when its group letter ("Pflichtgruppe B") matches the group of the team.
- Venue addresses come from the court pages and go into LOCATION.

Each event has a stable UID, so a new import updates events that already
exist instead of adding duplicates.

Usage:
    ./gaenserndorf_calendar.py                      # writes gaenserndorf.ics
    ./gaenserndorf_calendar.py -o handball.ics
    ./gaenserndorf_calendar.py --teams U12W U14M
    ./gaenserndorf_calendar.py --split              # one .ics per team
    ./gaenserndorf_calendar.py --no-elite-cup
"""

import argparse
import datetime as dt
import hashlib
import html
import re
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://oehb-handball.liga.nu"
WA = BASE + "/cgi-bin/WebObjects/nuLigaHBAT.woa/wa"
CLUB_ID = 67  # UHC Gänserndorf
CLUB_NAME_PATTERN = re.compile(r"G[äa]e?nserndorf", re.I)
TZ = ZoneInfo("Europe/Vienna")

DEFAULT_TEAMS = ["U7W", "U9W", "U12W", "U10M", "U11M", "U14M", "U16M"]

# Duration of a timed event, per age class. Other classes use DEFAULT_DURATION.
DURATION = {"U7": 180, "U9": 180, "U10": 90, "U11": 90, "U12": 90}
DEFAULT_DURATION = 120  # minutes


def fetch(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def text(fragment):
    s = re.sub(r"<[^>]+>", " ", fragment)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def table_rows(page):
    """Yield the <td> cell fragments of every row of the result tables."""
    for table in re.findall(r'(?s)<table class="result-set".*?</table>', page):
        for row in re.findall(r"(?s)<tr.*?</tr>", table):
            cells = re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)
            if cells:
                yield cells


def team_key(league):
    """Map a league name to a team key, for example 'NÖ U09W' -> 'U9W'.

    'ÖHB Elite Cup MU14' -> 'U14M', 'ÖHB Elite Cup WU14' -> 'U14W'.
    """
    m = re.search(r"\bU0?(\d+)\s*([MW])\b", league)
    if m:
        return f"U{m.group(1)}{m.group(2)}"
    m = re.search(r"\b([MW])U0?(\d+)\b", league)
    if m:
        return f"U{m.group(2)}{m.group(1)}"
    return None


# --------------------------------------------------------------------------
# Venues
# --------------------------------------------------------------------------

_venues = {}


def venue(court_cell):
    """Return 'Hall name, street, zip city' for a court cell of a table."""
    name_m = re.search(r'title="([^"]*)"', court_cell)
    name = html.unescape(name_m.group(1)) if name_m else text(court_cell)
    link_m = re.search(r'href="([^"]*courtInfo[^"]*)"', court_cell)
    if not link_m:
        return name
    url = BASE + html.unescape(link_m.group(1))
    loc = re.search(r"location=(\d+)", url)
    key = loc.group(1) if loc else url
    if key not in _venues:
        address = ""
        try:
            page = fetch(url)
            m = re.search(r"(?s)Hallenadresse</h2>(.*?)(?:<a |</p>)", page)
            if m:
                parts = [text(p) for p in re.split(r"<br\s*/?>", m.group(1))]
                address = ", ".join(
                    p for p in parts if p and not re.match(r"(Tel|Fax|E-?Mail)", p, re.I)
                )
        except OSError as exc:
            print(f"warning: no address for {name}: {exc}", file=sys.stderr)
        _venues[key] = f"{name}, {address}" if address else name
    return _venues[key]


# --------------------------------------------------------------------------
# Club match list
# --------------------------------------------------------------------------


def season_span(today=None):
    """Return (first day, last day) of the season that contains today.

    A season runs from 1 July to 30 June. The season filters of the portal
    can hide matches of one competition (the ÖMS filter shows only the
    matches of its calendar year), so the search uses this date range.
    """
    today = today or dt.date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return dt.date(start, 7, 1), dt.date(start + 1, 6, 30)


def club_matches():
    first, last = season_span()
    season = f"{first.year}/{str(last.year)[2:]}"
    page = fetch(
        f"{WA}/clubMeetings",
        {
            "club": CLUB_ID,
            "searchType": "1",
            "searchTimeRangeFrom": f"{first:%d.%m.%Y}",
            "searchTimeRangeTo": f"{last:%d.%m.%Y}",
            "selectedTeamId": "WONoSelectionString",
            "searchMeetings": "Suchen",
        },
    )
    date = None
    for cells in table_rows(page):
        if len(cells) < 9:
            continue
        day = text(cells[1])
        if re.fullmatch(r"\d\d\.\d\d\.\d{4}", day):
            date = day
        if not date:
            continue
        yield {
            "season": season,
            "date": date,
            "time": text(cells[2]),
            "court": cells[3],
            "number": text(cells[4]),
            "league": text(cells[5]),
            "home": text(cells[6]),
            "guest": text(cells[7]),
            "result": text(cells[8]),
        }


# --------------------------------------------------------------------------
# U9 tournaments
# --------------------------------------------------------------------------


def tournament_groups():
    """Return {team key: (schedule group URL, group letter)} for U9 teams."""
    page = fetch(f"{WA}/clubTeams?club={CLUB_ID}")
    links = {
        text(label): html.unescape(href)
        for href, label in re.findall(
            r'(?s)<a [^>]*href="([^"]*groupPage[^"]*)"[^>]*>(.*?)</a>', page
        )
    }
    result = {}
    for label, href in links.items():
        m = re.match(r"NÖ (U\d+[MW]) alle Termine", label)
        if not m:
            continue
        key = m.group(1)
        letter = next(
            (
                g.group(1)
                for lab in links
                if (g := re.fullmatch(rf"NÖ {key} Gruppe (\w)", lab))
            ),
            None,
        )
        if letter:
            result[key] = (BASE + href, letter)
    return result


def tournaments(key, url, letter):
    page = fetch(url + "&displayTyp=gesamt&displayDetail=meetings")
    date = None
    for cells in table_rows(page):
        if len(cells) < 7:
            continue
        day = text(cells[1])
        if re.fullmatch(r"\d\d\.\d\d\.\d{4}", day):
            date = day
        group = text(cells[6])
        if not date or not group.lower().endswith(f"gruppe {letter.lower()}"):
            continue
        yield {
            "season": "",
            "date": date,
            "time": text(cells[2]),
            "court": cells[3],
            "number": "",
            "league": f"NÖ {key} Turnier",
            "home": text(cells[5]),
            "guest": group,
            "result": "",
            "tournament": True,
        }


# --------------------------------------------------------------------------
# iCalendar output
# --------------------------------------------------------------------------


def ics_escape(s):
    return (
        s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def fold(line):
    """Fold a content line to 75 octets, as RFC 5545 requires."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > 75:
            out.append(cur.decode())
            cur = b" "
        cur += b
    out.append(cur.decode())
    return "\r\n".join(out)


def event_lines(key, m, stamp):
    day = dt.datetime.strptime(m["date"], "%d.%m.%Y").date()
    tm = re.match(r"(\d\d):(\d\d)", m["time"])
    timed = tm and (tm.group(1), tm.group(2)) != ("00", "00")

    if m.get("tournament"):
        summary = f"[{key}] Turnier in {m['home']}"
    else:
        summary = f"[{key}] {m['home']} – {m['guest']}"
    if "abgesagt" in m["result"].lower():
        summary += " (abgesagt)"

    desc = [m["league"]]
    if m["season"]:
        desc.append(f"Saison: {m['season']}")
    if m["number"] and m["number"] != "0":
        desc.append(f"Spiel-Nr.: {m['number']}")
    if not timed:
        desc.append("Uhrzeit noch nicht bekannt")
    if re.fullmatch(r"\d+:\d+", m["result"] or "") and m["result"] != "0:0":
        desc.append(f"Ergebnis: {m['result']}")
    desc.append(f"Quelle: {WA}/clubMeetings?club={CLUB_ID}")

    uid_src = f"{key}|{m['league']}|{m['number']}|{m['home']}|{m['guest']}|{m['date']}"
    if m["number"] and m["number"] != "0":
        uid_src = f"{m['league']}|{m['number']}"
    uid = hashlib.sha1(uid_src.encode()).hexdigest()[:20] + "@gaenserndorf-handball"

    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}"]
    if timed:
        start = dt.datetime.combine(day, dt.time(int(tm.group(1)), int(tm.group(2))), TZ)
        age = re.match(r"U\d+", key).group(0)
        end = start + dt.timedelta(minutes=DURATION.get(age, DEFAULT_DURATION))
        fmt = "%Y%m%dT%H%M%SZ"
        lines += [
            f"DTSTART:{start.astimezone(dt.timezone.utc).strftime(fmt)}",
            f"DTEND:{end.astimezone(dt.timezone.utc).strftime(fmt)}",
        ]
    else:
        lines += [
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}",
        ]
    lines += [
        f"SUMMARY:{ics_escape(summary)}",
        f"LOCATION:{ics_escape(venue(m['court']))}",
        f"DESCRIPTION:{ics_escape(chr(10).join(desc))}",
        "END:VEVENT",
    ]
    return lines


def write_ics(path, name, events):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gaenserndorf-handball//nuLiga export//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:Europe/Vienna",
    ]
    for key, m in events:
        lines += event_lines(key, m, stamp)
    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(l) for l in lines) + "\r\n")


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--teams", nargs="+", default=DEFAULT_TEAMS, metavar="UxxM/W")
    ap.add_argument("-o", "--output", default="gaenserndorf.ics")
    ap.add_argument("--split", action="store_true", help="write one .ics file per team")
    ap.add_argument(
        "--no-elite-cup", action="store_true", help="skip ÖHB Elite Cup (ÖMS) matches"
    )
    args = ap.parse_args()
    wanted = [t.upper() for t in args.teams]

    events = []
    seen = set()
    for m in club_matches():
        key = team_key(m["league"])
        if key not in wanted or re.match(r"U9", key):
            continue
        if args.no_elite_cup and "Elite Cup" in m["league"]:
            continue
        if not (CLUB_NAME_PATTERN.search(m["home"]) or CLUB_NAME_PATTERN.search(m["guest"])):
            continue
        ident = (m["league"], m["number"], m["date"], m["home"], m["guest"])
        if ident in seen:
            continue
        seen.add(ident)
        events.append((key, m))

    for key, (url, letter) in tournament_groups().items():
        if key in wanted:
            events += [(key, m) for m in tournaments(key, url, letter)]

    def sort_key(e):
        d = dt.datetime.strptime(e[1]["date"], "%d.%m.%Y")
        return (d, e[1]["time"], e[0])

    events.sort(key=sort_key)

    found = {k for k, _ in events}
    for team in wanted:
        if team not in found:
            print(f"warning: no matches found for {team} on nuLiga", file=sys.stderr)

    if args.split:
        stem = re.sub(r"\.ics$", "", args.output)
        for team in sorted(found):
            path = f"{stem}_{team}.ics"
            sub = [e for e in events if e[0] == team]
            write_ics(path, f"Handball Gänserndorf {team}", sub)
            print(f"{path}: {len(sub)} events")
    else:
        write_ics(args.output, "Handball Gänserndorf", events)
        print(f"{args.output}: {len(events)} events")
        for team in wanted:
            n = sum(1 for k, _ in events if k == team)
            print(f"  {team:5} {n:3}")


if __name__ == "__main__":
    main()
