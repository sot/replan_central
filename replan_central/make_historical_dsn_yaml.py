"""Generate a dsn_summary.yaml file for a historical date range using kadi dsn_comms.

The output matches the format consumed by make_timeline.py's get_comms() and
draw_communication_passes(), so that run_historical_timeline.py can supply
historical comms passes instead of the live rolling file.

Usage::

    python make_historical_dsn_yaml.py --start 2026:093:00:00:00 \\
                                        --stop  2026:096:00:00:00 \\
                                        --out   /path/to/dsn_summary.yaml
"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from cxotime import CxoTime
from kadi import events

# EDT = UTC-4, EST = UTC-5.  Use a simple rule: EDT Mar–Nov, EST otherwise.
_EDT = timedelta(hours=-4)
_EST = timedelta(hours=-5)

_MONTHS_SHORT = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
_DAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _utc_to_eastern(dt_utc: datetime) -> tuple[datetime, str]:
    """Return (local_datetime, tz_label) converting UTC to EST/EDT."""
    # Simple DST rule: second Sunday in March → first Sunday in November
    year = dt_utc.year
    # Second Sunday in March
    dst_start = datetime(year, 3, 8, 2, tzinfo=timezone.utc)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    # First Sunday in November
    dst_end = datetime(year, 11, 1, 2, tzinfo=timezone.utc)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    offset = _EDT if dst_start <= dt_utc.replace(tzinfo=timezone.utc) < dst_end else _EST
    label = "EDT" if offset == _EDT else "EST"
    local = dt_utc + offset
    return local, label


def _track_local(bot_date: str, eot_date: str) -> str:
    """Build a track_local string like '2115-2215 EDT, Sat 20 Jun'."""
    bot_utc = CxoTime(bot_date).datetime
    eot_utc = CxoTime(eot_date).datetime
    bot_local, tz = _utc_to_eastern(bot_utc)
    eot_local, _ = _utc_to_eastern(eot_utc)
    bot_hhmm = bot_local.strftime("%H%M")
    eot_hhmm = eot_local.strftime("%H%M")
    dow = _DAYS_SHORT[bot_local.weekday()]
    day = f"{bot_local.day:02d}"
    mon = _MONTHS_SHORT[bot_local.month]
    return f"{bot_hhmm}-{eot_hhmm} {tz}, {dow} {day} {mon}"


def _year_day_frac(date: str) -> str:
    """Return 'YYYY DOY.frac' like '2026 172.052'."""
    t = CxoTime(date)
    dt = t.datetime
    year = dt.year
    # DOY (1-based) + fraction of day
    doy_start = datetime(year, 1, 1)
    delta = dt - doy_start
    doy_frac = delta.days + 1 + delta.seconds / 86400
    return f"{year} {doy_frac:.3f}"


def _field(label: str, value) -> dict:
    return {"label": label, "value": value}


def comm_to_dict(c) -> dict:
    """Convert a kadi dsn_comms event to the dsn_summary.yaml entry format."""
    bot_date = CxoTime(c.start).date
    eot_date = CxoTime(c.stop).date

    # sched_support_time: "DOY/HHMM-HHMM" in UTC
    bot_dt = CxoTime(c.start).datetime
    eot_dt = CxoTime(c.stop).datetime
    doy = int(bot_dt.strftime("%j"))
    sched = f"{doy:03d}/{bot_dt.strftime('%H%M')}-{eot_dt.strftime('%H%M')}"

    return {
        "activity":           _field("Activity",           c.activity),
        "bot":                _field("BOT",                bot_dt.strftime("%H%M")),
        "bot_date":           _field("BOT date",           bot_date),
        "bot_time":           _field("BOT time",           int(c.tstart)),
        "bot_year_day":       _field("BOT year",           _year_day_frac(bot_date)),
        "eot":                _field("EOT",                eot_dt.strftime("%H%M")),
        "eot_date":           _field("EOT date",           eot_date),
        "eot_time":           _field("EOT time",           int(c.tstop)),
        "eot_year_day":       _field("EOT year",           _year_day_frac(eot_date)),
        "lga":                _field("LGA",                ""),
        "sched_support_time": _field("Support (GMT)",      sched),
        "site":               _field("Site",               c.site),
        "soe":                _field("SOE",                c.soe),
        "station":            _field("Station",            c.station),
        "track_local":        _field("Track time (local)", _track_local(bot_date, eot_date)),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate dsn_summary.yaml for a historical date range from kadi"
    )
    parser.add_argument(
        "--start", required=True,
        help="Start date in CxoTime format, e.g. 2026:093:00:00:00"
    )
    parser.add_argument(
        "--stop", required=True,
        help="Stop date in CxoTime format, e.g. 2026:096:00:00:00"
    )
    parser.add_argument(
        "--out", required=True,
        help="Output path for dsn_summary.yaml"
    )
    args = parser.parse_args()

    comms = list(events.dsn_comms.filter(args.start, args.stop))
    print(f"Found {len(comms)} DSN comm passes between {args.start} and {args.stop}")

    entries = [comm_to_dict(c) for c in comms]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        yaml.dump(entries, f, default_flow_style=False, allow_unicode=True)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
