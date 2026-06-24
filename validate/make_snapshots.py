#!/usr/bin/env python3
"""Create side-by-side web snapshots for test vs flight outputs.

This script is intended to run standalone from cron and generate a fully
self-contained viewer that can be copied to any web-accessible directory.
"""

import argparse
import difflib
import html
import io
import json
import posixpath
import re
import shutil
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

try:
    from astropy.table import Table

    HAS_ASTROPY = True
except Exception:
    HAS_ASTROPY = False

DEFAULT_TEST_SRC = Path("/export/jeanconn/miniforge3/envs/arc-pip-test/www/ASPECT/arc3")
DEFAULT_FLIGHT_SRC = Path("/proj/sot/ska/www/ASPECT/arc3")
DEFAULT_OUT_DIR = Path("snapshot_compare")
DEFAULT_BUCKET_MINUTES = 15
DEFAULT_ENTRY_FILE = "index.html"
MANIFEST_FILE = "manifest.json"
TEXT_DIFF_SUFFIXES = {
    ".css",
    ".csv",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".txt",
    ".yaml",
    ".yml",
}
HTML_SUFFIXES = {".htm", ".html"}


class LinkExtractor(HTMLParser):
    """Extract candidate local refs from HTML href/src attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        for name, value in attrs:
            if value is None:
                continue
            if name in {"href", "src", "poster", "data-src"}:
                self.links.append(value)


class RenderedTextExtractor(HTMLParser):
    """Extract rendered text-like content from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._ignore_depth = 0
        self.lines: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in {"script", "style"}:
            self._ignore_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignore_depth > 0:
            self._ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        for line in data.splitlines():
            text = " ".join(line.split())
            if text:
                self.lines.append(text)


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture side-by-side snapshots of test and flight arc3 web outputs"
    )
    parser.add_argument(
        "--test-src",
        type=Path,
        default=DEFAULT_TEST_SRC,
        help=f"Path to test output directory (default: {DEFAULT_TEST_SRC})",
    )
    parser.add_argument(
        "--flight-src",
        type=Path,
        default=DEFAULT_FLIGHT_SRC,
        help=f"Path to flight output directory (default: {DEFAULT_FLIGHT_SRC})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Destination root for snapshots + viewer HTML (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--bucket-minutes",
        type=int,
        default=DEFAULT_BUCKET_MINUTES,
        help=(
            "Bucket interval in minutes; snapshots taken in the same bucket share an ID "
            f"(default: {DEFAULT_BUCKET_MINUTES})"
        ),
    )
    parser.add_argument(
        "--captured-at",
        type=str,
        help=(
            "UTC capture time in ISO format (example: 2026-06-18T16:00:00Z). "
            "Default: now"
        ),
    )
    parser.add_argument(
        "--entry-file",
        default=DEFAULT_ENTRY_FILE,
        help=f"Page to open in each snapshot iframe (default: {DEFAULT_ENTRY_FILE})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing snapshot bucket directory if present",
    )
    return parser


def parse_utc_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bucket_time(dt: datetime, bucket_minutes: int) -> datetime:
    if bucket_minutes <= 0:
        raise ValueError("bucket-minutes must be a positive integer")

    epoch = int(dt.timestamp())
    bucket_seconds = bucket_minutes * 60
    bucket_epoch = (epoch // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)


def snapshot_id_from_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%MZ")


def load_manifest(manifest_path: Path) -> List[Dict]:
    if not manifest_path.exists():
        return []
    return json.loads(manifest_path.read_text())


def write_manifest(manifest_path: Path, snapshots: List[Dict]) -> None:
    manifest_path.write_text(json.dumps(snapshots, indent=2) + "\n")


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=True)


def choose_entry_file(snapshot_side_dir: Path, preferred: str) -> str:
    candidate = snapshot_side_dir / preferred
    if candidate.exists():
        return preferred

    for alt in ("index.html", "timeline.html"):
        alt_path = snapshot_side_dir / alt
        if alt_path.exists():
            return alt

    html_files = sorted(snapshot_side_dir.glob("*.html"))
    if html_files:
        return html_files[0].name

    raise FileNotFoundError(
        f"No HTML file found in snapshot side directory: {snapshot_side_dir}"
    )


def update_manifest_entry(snapshots: List[Dict], entry: Dict) -> List[Dict]:
    filtered = [snap for snap in snapshots if snap["id"] != entry["id"]]
    filtered.append(entry)
    filtered.sort(key=lambda snap: snap["id"], reverse=True)
    return filtered


def files_match(path_a: Path, path_b: Path) -> bool:
    return path_a.read_bytes() == path_b.read_bytes()


def read_text_if_possible(path: Path) -> Optional[List[str]]:
    if path.suffix.lower() not in TEXT_DIFF_SUFFIXES:
        return None
    try:
        return path.read_text().splitlines()
    except UnicodeDecodeError:
        return None


def normalize_local_ref(ref: str, current_rel: Path) -> Optional[Path]:
    parsed = urlsplit(ref)
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path:
        return None

    raw = parsed.path
    if raw.startswith("/"):
        rel = PurePosixPath(raw.lstrip("/"))
    else:
        rel = PurePosixPath(current_rel.as_posix()).parent / raw

    norm = posixpath.normpath(str(rel))
    if norm.startswith("../") or norm in {"..", "."}:
        return None
    return Path(norm)


def extract_local_refs_from_html(text: str, current_rel: Path) -> List[Path]:
    parser = LinkExtractor()
    parser.feed(text)
    refs: List[Path] = []
    for ref in parser.links:
        normalized = normalize_local_ref(ref, current_rel)
        if normalized is not None:
            refs.append(normalized)
    return refs


def extract_local_refs_from_css(text: str, current_rel: Path) -> List[Path]:
    refs: List[Path] = []
    for match in re.findall(r"url\((.*?)\)", text, flags=re.IGNORECASE):
        ref = match.strip().strip("\"'")
        if not ref:
            continue
        normalized = normalize_local_ref(ref, current_rel)
        if normalized is not None:
            refs.append(normalized)
    return refs


def crawl_reachable_files(root: Path, entry_file: str) -> Set[Path]:
    start = Path(entry_file)
    if not (root / start).exists():
        return set()

    seen: Set[Path] = set()
    queue: List[Path] = [start]

    while queue:
        rel = queue.pop(0)
        if rel in seen:
            continue

        abs_path = root / rel
        if not abs_path.exists() or not abs_path.is_file():
            continue

        seen.add(rel)
        text_lines = read_text_if_possible(abs_path)
        if text_lines is None:
            continue

        content = "\n".join(text_lines)
        refs: List[Path] = []
        suffix = rel.suffix.lower()
        if suffix in HTML_SUFFIXES:
            refs = extract_local_refs_from_html(content, rel)
        elif suffix == ".css":
            refs = extract_local_refs_from_css(content, rel)

        queue.extend(ref for ref in refs if (root / ref).exists() and ref not in seen)

    return seen


def html_rendered_text_lines(path: Path) -> Optional[List[str]]:
    if path.suffix.lower() not in HTML_SUFFIXES:
        return None
    try:
        content = path.read_text()
    except UnicodeDecodeError:
        return None

    table_lines: List[str] = []
    if HAS_ASTROPY:
        table_blocks = re.findall(r"(?is)<table\b.*?</table>", content)
        for i, table_html in enumerate(table_blocks, start=1):
            try:
                table = Table.read(io.StringIO(table_html), format="ascii.html")
            except Exception:
                continue

            delta_cols = [
                name
                for name in table.colnames
                if "".join(name.lower().split()) == "deltatime"
            ]
            for name in delta_cols:
                table.remove_column(name)

            table_lines.append(f"[table {i}]")
            table_lines.append(" | ".join(table.colnames))
            for row in table:
                values = [str(row[name]).strip() for name in table.colnames]
                table_lines.append(" | ".join(values))

    # Parse non-table content as rendered text and compare that as well.
    non_table_content = re.sub(r"(?is)<table\b.*?</table>", "", content)
    parser = RenderedTextExtractor()
    parser.feed(non_table_content)

    filtered: List[str] = []
    for line in parser.lines:
        norm = " ".join(line.split())
        if norm.lower() == "delta time":
            continue
        # Delta time cells are standalone values like "-11:48", "1d 00:48", "0:00".
        if re.match(r"^[+-]?\s*(?:\d+d\s+)?\d{1,2}:\d{2}$", norm):
            continue
        filtered.append(line)

    filtered.extend(table_lines)
    return filtered


def render_path_list(paths: List[Path], base: str) -> str:
    if not paths:
        return "<li>None</li>"

    items = []
    for path in paths[:100]:
        href = html.escape(f"{base}/{path.as_posix()}")
        name = html.escape(path.as_posix())
        items.append(f'<li><a href="{href}" target="_blank">{name}</a></li>')
    if len(paths) > 100:
        items.append(f"<li>... and {len(paths) - 100} more</li>")
    return "".join(items)


def render_changed_paths(paths: List[Path]) -> str:
    if not paths:
        return "<li>None</li>"

    items = []
    for path in paths[:100]:
        test_href = f"test/{path.as_posix()}"
        flight_href = f"flight/{path.as_posix()}"
        items.append(
            "<li>"
            f"{html.escape(path.as_posix())} "
            f'(<a href="{html.escape(flight_href)}" target="_blank">flight</a> | '
            f'<a href="{html.escape(test_href)}" target="_blank">test</a>)'
            "</li>"
        )
    if len(paths) > 100:
        items.append(f"<li>... and {len(paths) - 100} more</li>")
    return "".join(items)


def make_diff_report(
    snapshot_dir: Path,
    test_dir: Path,
    flight_dir: Path,
    test_entry: str,
    flight_entry: str,
) -> str:
    test_files = crawl_reachable_files(test_dir, test_entry)
    flight_files = crawl_reachable_files(flight_dir, flight_entry)
    shared_files = sorted(test_files & flight_files)
    only_test = sorted(test_files - flight_files)
    only_flight = sorted(flight_files - test_files)

    different_files = [
        path
        for path in shared_files
        if not files_match(test_dir / path, flight_dir / path)
    ]
    identical_files = len(shared_files) - len(different_files)

    missing_count = len(only_test) + len(only_flight)
    rendered_diff = "No rendered-text diff available for selected entry files."
    test_rendered_lines = html_rendered_text_lines(test_dir / test_entry)
    flight_rendered_lines = html_rendered_text_lines(flight_dir / flight_entry)
    if test_rendered_lines is not None and flight_rendered_lines is not None:
        diff_lines = list(
            difflib.unified_diff(
                flight_rendered_lines,
                test_rendered_lines,
                fromfile=f"flight-rendered/{flight_entry}",
                tofile=f"test-rendered/{test_entry}",
                lineterm="",
            )
        )
        rendered_diff = (
            "\n".join(diff_lines)
            if diff_lines
            else "No differences in rendered entry text."
        )

    report = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Snapshot Diff</title>
  <style>
    body {{
      font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      margin: 0;
      padding: 16px;
      background: #fbfcfe;
      color: #1a2233;
    }}
    h1, h2 {{ margin: 0 0 12px 0; }}
    section {{
      background: #fff;
      border: 1px solid #d5dde8;
      border-radius: 10px;
      padding: 14px;
      margin-bottom: 14px;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }}
    .metric {{
      padding: 10px;
      border-radius: 8px;
      background: #f4f8fc;
      border: 1px solid #d5dde8;
    }}
    .metric strong {{ display: block; font-size: 1.1rem; }}
    ul {{ margin: 0; padding-left: 20px; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #0f1724;
      color: #e8edf7;
      padding: 12px;
      border-radius: 8px;
      overflow: auto;
    }}
    a {{ color: #0a5cab; }}
    @media (max-width: 900px) {{
      .summary {{ grid-template-columns: 1fr 1fr; }}
    }}
  </style>
</head>
<body>
  <h1>Diff Summary</h1>
  <section>
    <div class=\"summary\">
      <div class=\"metric\"><strong>{len(shared_files)}</strong> displayed shared files</div>
      <div class=\"metric\"><strong>{identical_files}</strong> identical files</div>
      <div class=\"metric\"><strong>{len(different_files)}</strong> changed files</div>
      <div class=\"metric\"><strong>{missing_count}</strong> displayed missing files</div>
    </div>
  </section>
  <section>
    <h2>Only In Flight</h2>
    <ul>{render_path_list(only_flight, "flight")}</ul>
  </section>
  <section>
    <h2>Only In Test</h2>
    <ul>{render_path_list(only_test, "test")}</ul>
  </section>
  <section>
    <h2>Changed Displayed Files</h2>
    <ul>{render_changed_paths(different_files)}</ul>
  </section>
  <section>
    <h2>Rendered Entry Text Diff</h2>
    <pre>{html.escape(rendered_diff)}</pre>
  </section>
</body>
</html>
"""

    diff_path = snapshot_dir / "diff.html"
    diff_path.write_text(report)
    return "diff.html"


def render_index_html(
    snapshots: List[Dict],
    flight_label: str = "Flight",
    test_label: str = "Test",
    diff_label: str = "Diff",
) -> str:
    snapshots_json = json.dumps(snapshots)
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Replan Central Snapshot Compare</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --panel: #ffffff;
      --ink: #1a2233;
      --muted: #60708a;
      --line: #d5dde8;
      --accent: #0a5cab;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #ecf2f8 0%, var(--bg) 55%);
      color: var(--ink);
      font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    }}
    header {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 5;
    }}
    h1 {{ margin: 0 0 10px 0; font-size: 1.2rem; }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    button, select {{
      font: inherit;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 8px;
      padding: 6px 10px;
    }}
    button:hover {{ border-color: var(--accent); }}
    .status {{ color: var(--muted); margin-left: 8px; }}
    .frames {{
      display: grid;
      grid-template-columns: 1fr 1fr 0.9fr;
      gap: 10px;
      padding: 10px;
      height: calc(100vh - 110px);
    }}
    .pane {{
      display: flex;
      flex-direction: column;
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
      background: var(--panel);
    }}
    .pane h2 {{
      margin: 0;
      padding: 10px 12px;
      font-size: 0.95rem;
      border-bottom: 1px solid var(--line);
      background: #f8fbff;
    }}
    iframe {{
      border: 0;
      width: 100%;
      flex: 1;
      background: #fff;
    }}
    @media (max-width: 1200px) {{
      .frames {{
        grid-template-columns: 1fr;
        height: auto;
      }}
      .pane {{ height: 72vh; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Replan Central Snapshot Compare</h1>
    <div class=\"controls\">
      <button id=\"prev\" type=\"button\">Previous</button>
      <button id=\"next\" type=\"button\">Next</button>
      <select id=\"snapshotSelect\"></select>
      <span class=\"status\" id=\"status\"></span>
    </div>
  </header>

  <section class=\"frames\">
    <div class=\"pane\">
      <h2>{flight_label}</h2>
      <iframe id=\"flightFrame\" title=\"{flight_label} snapshot\"></iframe>
    </div>
    <div class=\"pane\">
      <h2>{test_label}</h2>
      <iframe id=\"testFrame\" title=\"{test_label} snapshot\"></iframe>
    </div>
    <div class=\"pane\">
      <h2>{diff_label}</h2>
      <iframe id=\"diffFrame\" title=\"{diff_label} snapshot\"></iframe>
    </div>
  </section>

  <script>
    const snapshots = {snapshots_json};
    const select = document.getElementById('snapshotSelect');
    const status = document.getElementById('status');
    const flightFrame = document.getElementById('flightFrame');
    const testFrame = document.getElementById('testFrame');
    const diffFrame = document.getElementById('diffFrame');
    const prevBtn = document.getElementById('prev');
    const nextBtn = document.getElementById('next');

    function labelFor(snapshot) {{
      return `${{snapshot.id}} (${{snapshot.captured_at}})`;
    }}

    function updateControls() {{
      const i = select.selectedIndex;
      prevBtn.disabled = i <= 0;
      nextBtn.disabled = i < 0 || i >= snapshots.length - 1;
    }}

    function setSnapshot(i) {{
      if (i < 0 || i >= snapshots.length) {{
        return;
      }}
      select.selectedIndex = i;
      const snap = snapshots[i];
      flightFrame.src = snap.flight_url;
      testFrame.src = snap.test_url;
      diffFrame.src = snap.diff_url;
      status.textContent = `Snapshot ${{i + 1}} of ${{snapshots.length}}`;
      updateControls();
    }}

    function init() {{
      if (snapshots.length === 0) {{
        status.textContent = 'No snapshots yet. Run show_snapshots.py to capture one.';
        select.disabled = true;
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        return;
      }}

      for (const snap of snapshots) {{
        const opt = document.createElement('option');
        opt.value = snap.id;
        opt.textContent = labelFor(snap);
        select.appendChild(opt);
      }}

      select.addEventListener('change', () => setSnapshot(select.selectedIndex));
      prevBtn.addEventListener('click', () => setSnapshot(select.selectedIndex - 1));
      nextBtn.addEventListener('click', () => setSnapshot(select.selectedIndex + 1));
      setSnapshot(0);
    }}

    init();
  </script>
</body>
</html>
"""


def main() -> None:
    args = get_parser().parse_args()

    if not args.test_src.exists():
        raise FileNotFoundError(
            f"Test source directory does not exist: {args.test_src}"
        )
    if not args.flight_src.exists():
        raise FileNotFoundError(
            f"Flight source directory does not exist: {args.flight_src}"
        )

    captured_at = (
        parse_utc_iso(args.captured_at)
        if args.captured_at
        else datetime.now(timezone.utc)
    )
    bucket = bucket_time(captured_at, args.bucket_minutes)
    snap_id = snapshot_id_from_time(bucket)

    out_dir = args.out_dir.resolve()
    snapshots_dir = out_dir / "snapshots"
    snap_dir = snapshots_dir / snap_id
    test_dst = snap_dir / "test"
    flight_dst = snap_dir / "flight"

    if snap_dir.exists() and args.force:
        shutil.rmtree(snap_dir)

    if not snap_dir.exists():
        test_dst.parent.mkdir(parents=True, exist_ok=True)
        copy_tree(args.test_src, test_dst)
        copy_tree(args.flight_src, flight_dst)

    test_entry = choose_entry_file(test_dst, args.entry_file)
    flight_entry = choose_entry_file(flight_dst, args.entry_file)
    diff_entry = make_diff_report(
        snap_dir, test_dst, flight_dst, test_entry, flight_entry
    )

    manifest_path = out_dir / MANIFEST_FILE
    snapshots = load_manifest(manifest_path)

    entry = {
        "id": snap_id,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "bucket_minutes": args.bucket_minutes,
        "test_url": f"snapshots/{snap_id}/test/{test_entry}",
        "flight_url": f"snapshots/{snap_id}/flight/{flight_entry}",
        "diff_url": f"snapshots/{snap_id}/{diff_entry}",
    }
    snapshots = update_manifest_entry(snapshots, entry)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(manifest_path, snapshots)

    index_html = render_index_html(snapshots)
    (out_dir / "index.html").write_text(index_html)

    print(f"Created/updated snapshot: {snap_id}")
    print(f"Output directory: {out_dir}")
    print(f"Open viewer: {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
