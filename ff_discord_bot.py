"""
Forex Factory "Red Folder" (High Impact) Discord notifier.

Wat doet dit script?
- Haalt de wekelijkse economische kalender van Forex Factory op (gratis, publieke JSON-feed).
- Filtert op "Red Folder" / High Impact events (optioneel ook op valuta).
- Stuurt via een Discord webhook:
    1) Elke dag rond een ingesteld tijdstip een overzicht van alle red folder events van die dag.
    2) Een losse waarschuwing X minuten voordat een red folder event plaatsvindt.

Dit script is bedoeld om periodiek te draaien (bv. elke 5 minuten) via cron (Mac/Linux)
of Taakplanner (Windows). Zie README.md voor instructies.

Handige test-commando's (los van de normale automatische planning):
    python ff_discord_bot.py --test             Stuurt een simpel testbericht, checkt alleen de webhook.
    python ff_discord_bot.py --show              Toont de eerstkomende red folder events, stuurt niets.
    python ff_discord_bot.py --force-summary      Stuurt het dagoverzicht opnieuw (ongeacht of dat al gebeurd is).
    python ff_discord_bot.py --force-reminder     Stuurt de reminder voor het eerstvolgende red folder event, nu meteen.
    python ff_discord_bot.py --force-summary-tomorrow   Stuurt het dagoverzicht van morgen (testdoeleinden).
Deze opties raken state.json niet aan (behalve het normale opschonen), dus ze verstoren
de normale automatische planning niet.
"""

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

# Laad .env expliciet vanuit de map van dit script (niet vanuit de "huidige map"),
# zodat het ook goed werkt als Taakplanner/cron dit script vanuit een andere map start.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# ---------------------------------------------------------------------------
# Configuratie (via .env, zie .env.example)
# ---------------------------------------------------------------------------
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES_BEFORE", "30"))
DAILY_SUMMARY_TIME = os.getenv("DAILY_SUMMARY_TIME", "08:00").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Amsterdam").strip()
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

_currencies_raw = os.getenv("CURRENCIES", "").strip()
CURRENCIES = (
    [c.strip().upper() for c in _currencies_raw.split(",") if c.strip()]
    if _currencies_raw
    else None
)  # None betekent: alle valuta

# Rol die getagd (gepingd) wordt bij het dagoverzicht en bij losse reminders.
# Leeg laten (of weglaten uit .env) betekent: geen rol taggen.
ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID", "").strip()


def admin_mention() -> str:
    return f"<@&{ADMIN_ROLE_ID}>" if ADMIN_ROLE_ID else ""


CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

# Hoelang bewaren we "al gemeld" event-ids voor we ze opruimen (voorkomt dat state.json
# eindeloos blijft groeien).
STATE_RETENTION_DAYS = 3

# Na hoeveel uur een verstuurd Discord-bericht (dagoverzicht of reminder) automatisch
# weer verwijderd wordt uit het kanaal.
MESSAGE_DELETE_AFTER_HOURS = 0.25  # TIJDELIJK voor test (15 min) -- normaal 24


# ---------------------------------------------------------------------------
# State (onthouden wat al gestuurd is, zodat we niet dubbel melden)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_summary_date": None, "notified_events": {}, "sent_messages": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def prune_notified_events(state: dict, now: datetime) -> None:
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    notified = state.get("notified_events", {})
    kept = {}
    for eid, added_iso in notified.items():
        try:
            added = datetime.fromisoformat(added_iso)
        except ValueError:
            continue
        if added >= cutoff:
            kept[eid] = added_iso
    state["notified_events"] = kept


def track_sent_message(state: dict, message_id: str, now: datetime) -> None:
    """Onthoudt een verstuurd bericht (met tijdstip), zodat het later automatisch
    verwijderd kan worden."""
    if not message_id:
        return
    state.setdefault("sent_messages", []).append({"id": message_id, "posted_at": now.isoformat()})


def cleanup_old_messages(state: dict, now: datetime) -> None:
    """Verwijdert Discord-berichten die de bot eerder heeft gestuurd en die ouder zijn
    dan MESSAGE_DELETE_AFTER_HOURS (dus het event/dagoverzicht is dan al lang geweest)."""
    sent = state.get("sent_messages", [])
    cutoff = now - timedelta(hours=MESSAGE_DELETE_AFTER_HOURS)
    remaining = []
    for m in sent:
        try:
            posted = datetime.fromisoformat(m["posted_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if posted <= cutoff:
            delete_discord_message(m.get("id"))
        else:
            remaining.append(m)
    state["sent_messages"] = remaining


# ---------------------------------------------------------------------------
# Kalender ophalen en filteren
# ---------------------------------------------------------------------------
def fetch_calendar() -> list:
    resp = requests.get(CALENDAR_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_event_time(event: dict) -> datetime:
    # Voorbeeld: "2026-08-17T13:30:00-04:00" (Amerikaanse Oostkust-tijd, incl. offset)
    dt = datetime.fromisoformat(event["date"])
    return dt.astimezone(LOCAL_TZ)


def is_red_folder(event: dict) -> bool:
    return str(event.get("impact", "")).strip().lower() == "high"


def currency_allowed(event: dict) -> bool:
    if CURRENCIES is None:
        return True
    return str(event.get("country", "")).upper() in CURRENCIES


def event_id(event: dict) -> str:
    return f"{event.get('country')}|{event.get('title')}|{event.get('date')}"


def get_red_events() -> list:
    raw_events = fetch_calendar()
    return [e for e in raw_events if is_red_folder(e) and currency_allowed(e)]


# ---------------------------------------------------------------------------
# Berichten opbouwen
# ---------------------------------------------------------------------------
def discord_timestamp(t: datetime, style: str = "R") -> str:
    """Discord's eigen tijdstip-markup (<t:UNIX:STYLE>). Discord rendert dit zelf,
    live bijgewerkt en automatisch in de tijdzone van elke lezer.
    Stijl "R" = relatief, bv. "over 12 minuten" — telt vanzelf af zonder dat de bot iets hoeft te doen."""
    return f"<t:{int(t.timestamp())}:{style}>"


def build_summary_message(events_for_day: list, label: str) -> str:
    lines = []
    mention = admin_mention()
    if mention:
        lines.append(mention)
    if events_for_day:
        lines.append(f"\U0001F4C5 **Red Folder (High Impact) events {label}:**")
        for e in sorted(events_for_day, key=parse_event_time):
            t = parse_event_time(e)
            lines.append(
                f"\U0001F534 `{t.strftime('%H:%M')}` — **{e.get('country')}** — {e.get('title')} "
                f"({discord_timestamp(t, 'R')})"
            )
    else:
        lines.append(f"\U0001F4C5 Geen Red Folder (High Impact) events {label}.")
    return "\n".join(lines)


def build_reminder_message(event: dict, minutes_until: float) -> str:
    t = parse_event_time(event)
    lines = []
    mention = admin_mention()
    if mention:
        lines.append(mention)
    lines.append(f"⚠️ **Red Folder event over {int(round(minutes_until))} minuten!**")
    lines.append(f"\U0001F534 `{t.strftime('%H:%M')}` — **{event.get('country')}** — {event.get('title')}")
    # Live countdown: Discord telt dit zelf af (en toont het in ieders eigen tijdzone),
    # zonder dat de bot opnieuw hoeft te versturen.
    lines.append(f"⏳ Start {discord_timestamp(t, 'R')} ({discord_timestamp(t, 't')})")
    lines.append(f"Forecast: {event.get('forecast') or '—'} | Vorige waarde: {event.get('previous') or '—'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def send_discord_message(content: str):
    """Stuurt een bericht naar Discord. Geeft het Discord message-ID terug bij succes
    (nodig om het bericht later automatisch te kunnen verwijderen), of None bij mislukking.
    De aanroeper (main) mag dus gewoon `if send_discord_message(...):` blijven doen."""
    if not WEBHOOK_URL:
        print("WAARSCHUWING: geen DISCORD_WEBHOOK_URL ingesteld in .env. Bericht niet verstuurd:")
        print(content)
        return None
    try:
        resp = requests.post(
            f"{WEBHOOK_URL}?wait=true",  # ?wait=true zodat Discord het bericht (incl. id) teruggeeft
            json={"content": content, "allowed_mentions": {"parse": ["roles"]}},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"Netwerkfout bij versturen naar Discord: {exc}")
        return None
    if resp.status_code >= 300:
        print(f"Fout bij versturen naar Discord ({resp.status_code}): {resp.text}")
        return None
    print("Bericht verstuurd naar Discord.")
    try:
        return resp.json().get("id")
    except ValueError:
        return None


def delete_discord_message(message_id) -> bool:
    """Verwijdert een eerder door deze webhook verstuurd bericht. 404 (al weg) telt ook als oke."""
    if not WEBHOOK_URL or not message_id:
        return False
    try:
        resp = requests.delete(f"{WEBHOOK_URL}/messages/{message_id}", timeout=15)
    except requests.RequestException as exc:
        print(f"Netwerkfout bij verwijderen van bericht {message_id}: {exc}")
        return False
    if resp.status_code not in (200, 204, 404):
        print(f"Fout bij verwijderen van bericht {message_id} ({resp.status_code}): {resp.text}")
        return False
    print(f"Oud bericht {message_id} verwijderd (ouder dan {MESSAGE_DELETE_AFTER_HOURS} uur).")
    return True


# ---------------------------------------------------------------------------
# Test-commando's (raken state.json niet aan, verstoren de planning dus niet)
# ---------------------------------------------------------------------------
def run_test() -> None:
    now = datetime.now(LOCAL_TZ)
    send_discord_message(
        f"✅ **Testbericht van je Forex Factory bot** — de webhook werkt! "
        f"({now.strftime('%d-%m-%Y %H:%M')} {TIMEZONE_NAME})"
    )


def run_show() -> None:
    now = datetime.now(LOCAL_TZ)
    try:
        red_events = get_red_events()
    except Exception as exc:
        print(f"Kon Forex Factory kalender niet ophalen: {exc}")
        return

    upcoming = sorted(
        (e for e in red_events if parse_event_time(e) >= now - timedelta(hours=1)),
        key=parse_event_time,
    )
    if not upcoming:
        print("Geen (aankomende) red folder events gevonden in de huidige kalenderfeed.")
        return

    print(f"Eerstkomende red folder (High Impact) events (nu: {now.strftime('%d-%m %H:%M')} {TIMEZONE_NAME}):")
    for e in upcoming[:20]:
        t = parse_event_time(e)
        minutes_until = int((t - now).total_seconds() / 60)
        wanneer = f"over {minutes_until} min" if minutes_until >= 0 else f"{-minutes_until} min geleden"
        print(f"  {t.strftime('%a %d-%m %H:%M')} — {e.get('country')} — {e.get('title')} ({wanneer})")


def run_force_summary() -> None:
    now = datetime.now(LOCAL_TZ)
    today_str = now.strftime("%Y-%m-%d")
    try:
        red_events = get_red_events()
    except Exception as exc:
        print(f"Kon Forex Factory kalender niet ophalen: {exc}")
        return
    todays_red = [e for e in red_events if parse_event_time(e).strftime("%Y-%m-%d") == today_str]
message_id =     send_discord_message(build_summary_message(todays_red, "vandaag"))
if message_id:
    state = load_state()
    track_sent_message(state, message_id, now)
    save_state(state)


def run_force_summary_tomorrow() -> None:
    """Test: stuurt het dagoverzicht alsof het morgen is (voor events van morgen)."""
    now = datetime.now(LOCAL_TZ)
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        red_events = get_red_events()
    except Exception as exc:
        print(f"Kon Forex Factory kalender niet ophalen: {exc}")
        return
    tomorrows_red = [e for e in red_events if parse_event_time(e).strftime("%Y-%m-%d") == tomorrow_str]
message_id =     send_discord_message(build_summary_message(tomorrows_red, "morgen"))
if message_id:
    state = load_state()
    track_sent_message(state, message_id, now)
    save_state(state)


def run_force_reminder() -> None:
    now = datetime.now(LOCAL_TZ)
    try:
        red_events = get_red_events()
    except Exception as exc:
        print(f"Kon Forex Factory kalender niet ophalen: {exc}")
        return
    upcoming = sorted(
        (e for e in red_events if parse_event_time(e) >= now),
        key=parse_event_time,
    )
    if not upcoming:
        print("Geen aankomend red folder event gevonden om een testreminder voor te sturen.")
        return
    e = upcoming[0]
    t = parse_event_time(e)
    minutes_until = (t - now).total_seconds() / 60
        message_id = send_discord_message(build_reminder_message(e, minutes_until)) 
    if message_id:
        state = load_state()
        track_sent_message(state, message_id, now)
        save_state(state)



# ---------------------------------------------------------------------------
# Hoofdlogica (normale, automatische run)
# ---------------------------------------------------------------------------
def main() -> None:
    now = datetime.now(LOCAL_TZ)
    state = load_state()
    prune_notified_events(state, now)
    cleanup_old_messages(state, now)

    try:
        raw_events = fetch_calendar()
    except Exception as exc:  # netwerkfout, timeout, etc.
        print(f"Kon Forex Factory kalender niet ophalen: {exc}")
        return

    red_events = [e for e in raw_events if is_red_folder(e) and currency_allowed(e)]

    # --- 1) Dagelijks overzicht ---
    today_str = now.strftime("%Y-%m-%d")
    if state.get("last_summary_date") != today_str:
        try:
            summary_hh, summary_mm = (int(x) for x in DAILY_SUMMARY_TIME.split(":"))
        except ValueError:
            summary_hh, summary_mm = 8, 0
        target = now.replace(hour=summary_hh, minute=summary_mm, second=0, microsecond=0)

        if now >= target:
            todays_red = [
                e for e in red_events
                if parse_event_time(e).strftime("%Y-%m-%d") == today_str
            ]
            message_id = send_discord_message(build_summary_message(todays_red, "vandaag"))
            if message_id:
                state["last_summary_date"] = today_str
                track_sent_message(state, message_id, now)
                save_state(state)
            else:
                print("Dagoverzicht is NIET gelukt te versturen; wordt bij de volgende run opnieuw geprobeerd.")


    # --- 2) Losse reminders vlak voor elk event ---
    notified = state.get("notified_events", {})
    changed = False
    for e in red_events:
        eid = event_id(e)
        if eid in notified:
            continue
        t = parse_event_time(e)
        minutes_until = (t - now).total_seconds() / 60
        if 0 <= minutes_until <= REMINDER_MINUTES:
            reminder_msg_id = send_discord_message(build_reminder_message(e, minutes_until))
            if reminder_msg_id:
                notified[eid] = now.isoformat()
                track_sent_message(state, reminder_msg_id, now)
                changed = True
            else:
                print(f"Reminder voor {eid} is NIET gelukt te versturen; wordt bij de volgende run opnieuw geprobeerd.")

    if changed:
        state["notified_events"] = notified
        save_state(state)
    else:
        # Zorg dat prune-/cleanup-resultaat ook bewaard blijft ook als er niets nieuws was
        save_state(state)

    print("Klaar (geen extra output hierboven betekent: niets nieuws te melden op dit moment).")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forex Factory Red Folder Discord notifier")
    parser.add_argument(
        "--test", action="store_true",
        help="Stuur een simpel testbericht naar Discord om te checken of de webhook werkt.",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Toon de eerstkomende red folder events in de terminal, zonder iets te versturen.",
    )
    parser.add_argument(
        "--force-summary", action="store_true",
        help="Stuur het dagoverzicht van vandaag opnieuw, ook als dat vandaag al gebeurd is.",
    )
    parser.add_argument(
        "--force-reminder", action="store_true",
        help="Stuur nu meteen de reminder voor het eerstvolgende red folder event.",
    )
    parser.add_argument(
        "--force-summary-tomorrow", action="store_true",
        help="Test: stuur het dagoverzicht van morgen (i.p.v. vandaag), ongeacht de tijd.",
    )
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    if args.test:
        run_test()
    elif args.show:
        run_show()
    elif args.force_summary:
        run_force_summary()
    elif args.force_reminder:
        run_force_reminder()
    elif args.force_summary_tomorrow:
        run_force_summary_tomorrow()
    else:
        main()
