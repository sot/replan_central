import argparse
import importlib.resources
import shutil
import subprocess
from pathlib import Path

ACIS_FLUENCE_DEFAULT = "/proj/web-cxc/htdocs/acis/Fluence/current.dat"


def main():
    parser = argparse.ArgumentParser(description="Run arc_time_machine.pl Perl script")
    parser.add_argument(
        "--arc-data-dir", dest="arc_data_dir", required=True, help="ARC data directory"
    )
    parser.add_argument(
        "--time-machine-dir",
        dest="time_machine_dir",
        required=True,
        help="Time machine directory",
    )
    parser.add_argument(
        "--acis-fluence-file",
        dest="acis_fluence_file",
        default=ACIS_FLUENCE_DEFAULT,
        help=(
            "Path to the live ACIS current.dat to snapshot into the time machine "
            f"(default: {ACIS_FLUENCE_DEFAULT})"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    args, unknown = parser.parse_known_args()

    # Copy the live current.dat into the time machine directory before running
    # the Perl script so the git add/commit picks it up as a historical snapshot.
    src = Path(args.acis_fluence_file)
    if src.exists():
        time_machine_dir = Path(args.time_machine_dir)
        time_machine_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, time_machine_dir / "current.dat")
        if args.verbose:
            print(f"Copied {src} -> {time_machine_dir / 'current.dat'}")
    else:
        print(f"WARNING: --acis-fluence-file {src} not found, skipping snapshot")

    perl_script = (
        importlib.resources.files("replan_central.perl") / "arc_time_machine.pl"
    )
    cmd = [
        "perl",
        str(perl_script),
        "--arc-data-dir",
        args.arc_data_dir,
        "--time-machine-dir",
        args.time_machine_dir,
    ]
    if args.verbose:
        cmd.append("--verbose")
    cmd.extend(unknown)
    subprocess.run(cmd, check=True)
