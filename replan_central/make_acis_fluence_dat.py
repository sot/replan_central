"""Make an ACIS current.dat file from ACE.h5 data for testing purposes only.

This generates a file with the specific line structure expected by
make_timeline.get_fluence(), derived from historical ACE.h5 data rather than
from the official /proj/web-cxc/htdocs/acis/Fluence/current.dat. It is
intended solely for historical replay testing (e.g. via
run_historical_timeline.py) and should NOT be used as a substitute for the
real current.dat in production.
"""

import argparse
from pathlib import Path

import numpy as np
import tables
from cheta import fetch
from cxotime import CxoTime

CHANNELS = ["de1", "de4", "p1", "p3", "p5", "p6", "p7"]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create an ACIS current.dat from ACE.h5 for testing"
    )
    parser.add_argument(
        "--ace-h5",
        required=True,
        help="Path to ACE.h5 file",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output path for current.dat",
    )
    parser.add_argument(
        "--date-now",
        help="Target date; select latest ACE sample with time <= this date",
    )
    parser.add_argument(
        "--fluence-scale",
        type=float,
        default=1.0,
        help="Scale factor applied to integrated channel fluence values",
    )
    return parser.parse_args()


def get_target_index(times, date_now):
    if date_now:
        target_secs = CxoTime(date_now).secs
        idx = int(np.searchsorted(times, target_secs, side="right") - 1)
        if idx < 0:
            raise ValueError("No ACE samples are available at or before --date-now")
    else:
        idx = len(times) - 1
    return idx


def make_flux_line(row):
    year = int(row["year"])
    month = int(row["month"])
    dom = int(row["dom"])
    hhmm = int(row["hhmm"])
    mjd = float(row["mjd"])
    sod = float(row["secs"])
    vals = [float(row[ch]) for ch in CHANNELS]
    anis_idx = float(row["anis_idx"])

    return (
        f"{year:4d} {month:2d} {dom:2d} {hhmm:04d} {mjd:6.0f} {sod:6.0f} "
        f"{vals[0]:11.2e} {vals[1]:10.2e} {vals[2]:10.2e} {vals[3]:10.2e} "
        f"{vals[4]:10.2e} {vals[5]:10.2e} {vals[6]:10.2e} {anis_idx:7.2f}"
    )


def get_fluence_start(date_now_secs):
    """Return CXO seconds of the most recent 55,000 km outbound altitude crossing.

    Fetches Dist_SatEarth (metres) via cheta over the preceding 10 days at
    5-minute cadence and finds the last upward crossing of 55,000 km before
    date_now_secs.
    """
    start = CxoTime(date_now_secs - 10 * 86400)
    stop = CxoTime(date_now_secs)
    msid = fetch.Msid("Dist_SatEarth", start.date, stop.date, stat="5min")
    times = msid.times  # mid-point of each 5-min bin, CXO seconds
    dist = msid.vals

    # Find upward crossings: dist[i-1] < threshold <= dist[i]
    # Dist_SatEarth is in metres; 55,000 km = 55_000e3 m
    below = dist < 55_000e3
    crossings = np.where(~below[1:] & below[:-1])[0] + 1  # index of first sample above
    crossings = crossings[times[crossings] < date_now_secs]

    if len(crossings) == 0:
        raise ValueError(
            "no 55,000 km outbound crossing found in Dist_SatEarth"
            f" for the 10 days before {CxoTime(date_now_secs).date}"
        )

    crossing_secs = float(times[crossings[-1]])
    print(f"  Using Dist_SatEarth 55,000 km crossing: {CxoTime(crossing_secs).date}")
    return crossing_secs


def get_fluence_values(table, idx, scale, fluence_start_secs):
    times = table.col("time")[: idx + 1]
    if len(times) < 2:
        return [0.0 for _ in CHANNELS]

    # Only integrate from the last radzone exit, matching real current.dat behaviour
    ok = times >= fluence_start_secs
    if not np.any(ok):
        print("  WARNING: no ACE samples after fluence_start; fluence will be 0")
        return [0.0 for _ in CHANNELS]

    pstat = table.col("pstat")[: idx + 1][ok]
    destat = table.col("destat")[: idx + 1][ok]
    t_window = times[ok]
    fluences = []
    integ = np.trapezoid if hasattr(np, "trapezoid") else np.trapz
    for ch in CHANNELS:
        vals = table.col(ch)[: idx + 1][ok]
        # Use the appropriate status mask: destat for electron channels,
        # pstat for proton channels.  Status == 0 means good data.
        stat = destat if ch in ("de1", "de4") else pstat
        good = stat == 0
        if good.sum() < 2:
            fluences.append(0.0)
            continue
        flu = float(integ(vals[good], t_window[good])) * scale
        fluences.append(max(flu, 0.0))
    return fluences


def make_fluence_line(cxo_time, fluences):
    date = cxo_time.date
    year = int(date[0:4])
    doy = int(date[5:8])
    hh = int(date[9:11])
    mm = int(date[12:14])
    ss = int(date[15:17])
    dt = cxo_time.datetime
    month = dt.month
    dom = dt.day

    hhmm = hh * 100 + mm
    sod = hh * 3600 + mm * 60 + ss
    return (
        f"{year:4d} {month:2d} {dom:2d} {hhmm:04d} {doy:6d} {sod:6d} "
        f"{fluences[0]:11.2e} {fluences[1]:10.2e} {fluences[2]:10.2e} {fluences[3]:10.2e} "
        f"{fluences[4]:10.2e} {fluences[5]:10.2e} {fluences[6]:10.2e}"
    )


def build_current_dat_text(flux_line, fluence_line):
    return "\n".join(
        [
            "TABLE 2: ACIS FLUX AND FLUENCE BASED ON ACE DATA",
            "Latest valid ACIS flux and fluence data...",
            "# UT Date   Time  Julian  of the  --- Electron keV ---"
            "   -------------------- Protons keV ------------------  Anis.",
            "# YR MO DA  HHMM    Day    Secs        38-53   175-315"
            "      56-78    112-187   337-594   761-1220 1073-1802  Index",
            "#-------------------------------------------------------------------------------",
            flux_line,
            "ACIS Fluence data...Start DOY,SOD",
            fluence_line,
            "",
        ]
    )


def main():
    args = parse_args()
    ace_h5 = Path(args.ace_h5)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tables.open_file(str(ace_h5)) as h5:
        table = h5.root.data
        times = table.col("time")
        idx = get_target_index(times, args.date_now)
        row = table[idx]
        cxo_time = CxoTime(float(row["time"]))

        fluence_start_secs = get_fluence_start(cxo_time.secs)
        flux_line = make_flux_line(row)
        fluence_values = get_fluence_values(
            table, idx, args.fluence_scale, fluence_start_secs
        )
        fluence_line = make_fluence_line(cxo_time, fluence_values)

    text = build_current_dat_text(flux_line, fluence_line)
    out_path.write_text(text)

    print(f"Wrote current.dat: {out_path}")
    print(f"Sample time: {cxo_time.date}")


if __name__ == "__main__":
    main()
