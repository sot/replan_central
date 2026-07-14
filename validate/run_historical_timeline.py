"""Run make_timeline.py for a historical date by truncating h5 data files.

This script creates a working directory with copies of the arc3 h5 data files
truncated to a given historical date, then runs make_timeline.py using that
directory as the data source. The result is the timeline output as it would
have appeared at that point in time.

Usage::

    python run_historical_timeline.py \\
        --date 2026:150:14:00:00 \\
        --arc-data-dir /proj/sot/ska/data/arc3 \\
        --out-dir /tmp/historical_timeline/2026-150

    # Or using the test environment:
    python run_historical_timeline.py \\
        --date 2026:150:14:00:00 \\
        --arc-data-dir /export/jeanconn/miniforge3/envs/arc-pip-test/data/arc3 \\
        --out-dir /tmp/historical_timeline/2026-150
"""

import argparse
import importlib.resources
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import tables
from astropy import units as u
from cxotime import CxoTime

H5_FILES = ["ACE.h5", "GOES_X.h5", "hrc_shield.h5"]


def truncate_h5(src: Path, dest: Path, cutoff_secs: float) -> None:
    """Copy an h5 file to dest, keeping only rows with time <= cutoff_secs."""
    with tables.open_file(str(src)) as h5_in:
        all_data = h5_in.root.data[:]
        filters = h5_in.root.data.filters

    ok = all_data["time"] <= cutoff_secs
    n_total = len(all_data)
    n_kept = int(np.sum(ok))
    print(f"  {src.name}: keeping {n_kept}/{n_total} rows up to cutoff")

    with tables.open_file(str(dest), "w") as h5_out:
        h5_out.create_table("/", "data", obj=all_data[ok], filters=filters)


def _compute_2hr_avg_p3(ace_h5: Path, cutoff_secs: float) -> float:
    """Return mean P3 flux over the 2 hours before cutoff_secs from ACE.h5.

    Returns -99999 (P3_BAD sentinel) if no valid samples are found.
    """
    with tables.open_file(str(ace_h5)) as h5:
        data = h5.root.data[:]
    times = data["time"]
    p3 = data["p3"]
    pstat = data["pstat"]
    window = (times >= cutoff_secs - 2 * 3600) & (times <= cutoff_secs)
    good = window & (pstat == 0)
    if not np.any(good):
        return -99999.0
    return float(np.mean(p3[good]))


def _write_ace_avg_file(dest: Path, p3_avg: float) -> None:
    """Write a minimal ace.html containing the AVERAGE line that get_avg_flux parses.

    Column order matches the real ace.html: DE1 DE4 P2 P3 ...
    Only the P3 value (column index 4 after split) is read by get_avg_flux.
    """
    with open(dest, "w") as f:
        f.write(f"AVERAGE           0.0     0.0   0.0   {p3_avg:.3f}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Run make_timeline.py for a historical date"
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Historical date in CxoTime format (e.g. 2026:150:14:00:00)",
    )
    parser.add_argument(
        "--arc-data-dir",
        required=True,
        help="Arc data directory containing the source h5 files",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Output directory for truncated h5 files and timeline products",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite out-dir if it already exists",
    )
    args = parser.parse_args()

    date = CxoTime(args.date)
    cutoff_secs = date.secs
    arc_data_dir = Path(args.arc_data_dir)
    out_dir = Path(args.out_dir)

    print(f"Historical date : {date.date}")
    print(f"Cutoff (secs)   : {cutoff_secs:.1f}")
    print(f"Source data dir : {arc_data_dir}")
    print(f"Output dir      : {out_dir}")

    if out_dir.exists():
        if args.overwrite:
            shutil.rmtree(out_dir)
        else:
            print(
                f"\nError: {out_dir} already exists. Use --overwrite to replace it.",
                file=sys.stderr,
            )
            sys.exit(1)
    out_dir.mkdir(parents=True)

    # Truncate each h5 file to the historical cutoff date
    print("\nTruncating h5 files...")
    for name in H5_FILES:
        src = arc_data_dir / name
        if not src.exists():
            print(f"  WARNING: {src} not found, skipping")
            continue
        truncate_h5(src, out_dir / name, cutoff_secs)

    # Build a mock ACIS current.dat from the truncated ACE.h5 so make_timeline
    # uses a historical-consistent fluence seed instead of the live file.
    mock_current_dat = out_dir / "current.dat"
    make_mock = importlib.resources.files("replan_central") / "make_acis_fluence_dat.py"
    mock_cmd = [
        sys.executable,
        str(make_mock),
        "--ace-h5",
        str(out_dir / "ACE.h5"),
        "--out",
        str(mock_current_dat),
        "--date-now",
        date.date,
    ]
    print(f"\nRunning: {' '.join(mock_cmd)}")
    subprocess.run(mock_cmd, check=True)

    # Compute the historical 2-hr average P3 flux from the truncated ACE.h5 and
    # write a minimal ace.html so make_timeline uses historical rates for the
    # post-NOW fluence projection instead of reading the live MTA file.
    mock_ace_avg = out_dir / "ace.html"
    p3_avg = _compute_2hr_avg_p3(out_dir / "ACE.h5", cutoff_secs)
    print(f"\nHistorical 2-hr avg P3 = {p3_avg:.1f} p/cm2/s/ster/MeV")
    _write_ace_avg_file(mock_ace_avg, p3_avg)

    # Generate a historical dsn_summary.yaml from kadi for the plot window.
    mock_dsn_comms = out_dir / "dsn_summary.yaml"
    make_dsn = (
        importlib.resources.files("replan_central") / "make_historical_dsn_yaml.py"
    )
    dsn_start = (date - 1 * u.day).date
    dsn_stop = (date + 3 * u.day).date
    dsn_cmd = [
        sys.executable,
        str(make_dsn),
        "--start",
        dsn_start,
        "--stop",
        dsn_stop,
        "--out",
        str(mock_dsn_comms),
    ]
    print(f"\nRunning: {' '.join(dsn_cmd)}")
    subprocess.run(dsn_cmd, check=True)

    # Run make_timeline.py with the truncated data dir and the historical date.
    make_timeline = importlib.resources.files("replan_central") / "make_timeline.py"
    env = os.environ.copy()
    env.setdefault("SKA", "/proj/sot/ska")
    cmd = [
        sys.executable,
        str(make_timeline),
        "--data-dir",
        str(out_dir),
        "--out",
        str(out_dir),
        "--date-now",
        date.date,
        "--acis-fluence-file",
        str(mock_current_dat),
        "--ace-avg-file",
        str(mock_ace_avg),
        "--dsn-comms-file",
        str(mock_dsn_comms),
    ]
    print(f"\nRunning: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, env=env)

    print(f"\nDone. Timeline output is in {out_dir}")


if __name__ == "__main__":
    main()
