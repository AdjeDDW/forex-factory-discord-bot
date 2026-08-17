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
Deze vier opties raken state.json niet aan (behalve het normale opschonen), dus ze verstoren
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

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

# Hoelang bewaren we "al gemeld" event-ids voor we ze opruimen (voorkomt dat state.json
# eindeloos blijft groeien).
STATE_RETENTION_DAYS = 3


# ---------------------------------------------------------------------------
# State (onthouden wat al gestuurd is, zodat we niet dubbel melden)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_summary_date": None, "notified_events": {}}


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
def build_summary_message(events_for_day: list, label: str) -> str:
    if events_for_day:
        lines = [f"📅 **Red Folder (High Impact) events {label}:**"]
        for e in sorted(events_for_day, key=parse_event_time):
            t = parse_event_time(e)
            lines.append(f"🔴 `{t.strftime('%H:%M')}` — **{e.get('country')}** — {e.get('title')}")
    else:
        lines = [f"📅 Geen Red Folder (High Impact) events {label}."]
    return "\n".join(lines)


def build_reminder_message(event: dict, minutes_until: float) -> str:
    t = parse_event_time(event)
    return (
        f"⚠️ **Red Folder event over {int(round(minutes_until))} minuten!**\n"
        f"🔴 `{t.strftime('%H:%M')}` — **{event.get('country')}** — {event.get('title')}\n"
        f"Forecast: {event.get('forecast') or '—'} | Vorige waarde: {event.get('previous') or '—'}"
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def send_discord_message(content: str) -> None:
    if not WEBHOOK_URL:
        print("WAARSCHUWING: geen DISCORD_WEBHOOK_URL ingesteld in .env. Bericht niet verstuurd:")
        print(content)
        return
    resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=15)
    if resp.status_code >= 300:
        print(f"Fout bij versturen naar Discord ({resp.status_code}): {resp.text}")
    else:
        print("Bericht verstuurd naar Discord.")


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
    send_discord_message(build_summary_message(todays_red, "vandaag"))


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
    send_discord_message(build_reminder_message(e, minutes_until))


# ---------------------------------------------------------------------------
# Hoofdlogica (normale, automatische run)
# ---------------------------------------------------------------------------
def main() -> None:
    now = datetime.now(LOCAL_TZ)
    state = load_state()
    prune_notified_events(state, now)

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
            send_discord_message(build_summary_message(todays_red, "vandaag"))
            state["last_summary_date"] = today_str
            save_state(state)

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
            send_discord_message(build_reminder_message(e, minutes_until))
            notified[eid] = now.isoformat()
            changed = True

    if changed:
        state["notified_events"] = notified
        save_state(state)
    else:
        # Zorg dat prune-resultaat ook bewaard blijft ook als er niets nieuws was
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
    else:
        main()
