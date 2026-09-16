"""Generalized version of pilot_normals.py: computes the blended climate
normal (temperature, for now) for every region in regions.json, for all 366
days of the year, and writes normals.json.

Usage: python dev/build_normals.py path/to/regions.json
"""

from __future__ import annotations

import argparse
import csv
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

WINDOW_DAYS = 7
WEIGHTS = {"p100": 0.4, "p30": 0.3, "p10": 0.3}
TODAY = date(2026, 9, 16)


def download_station_csv(station_id: str) -> Path:
    path = DATA_DIR / f"{station_id}.csv"
    if path.exists():
        return path
    url = f"https://noaa-ghcn-pds.s3.amazonaws.com/csv/by_station/{station_id}.csv"
    with urllib.request.urlopen(url, context=SSL_CONTEXT, timeout=60) as resp:
        path.write_bytes(resp.read())
    return path


def day_window(target: date, window: int) -> set[tuple[int, int]]:
    pairs = set()
    # 2024 anchor (leap year) so Feb 29 targets don't raise ValueError
    for offset in range(-window, window + 1):
        d = date(2024, target.month, target.day) + timedelta(days=offset)
        pairs.add((d.month, d.day))
    return pairs


def index_ghcn_csv(csv_path: Path, element: str) -> dict[tuple[int, int], list[float]]:
    """Read a station CSV once; index values by (month, day) across all years."""
    index: dict[tuple[int, int], list[float]] = {}
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["ELEMENT"] != element or row["Q_FLAG"]:
                continue
            d = row["DATE"]
            m, day = int(d[4:6]), int(d[6:8])
            index.setdefault((m, day), []).append(int(row["DATA_VALUE"]) / 10.0)
    return index


def avg_from_index(index: dict[tuple[int, int], list[float]], wanted: set[tuple[int, int]]):
    values = []
    for key in wanted:
        values.extend(index.get(key, []))
    return (sum(values) / len(values)) if values else None


def fetch_openmeteo(lat: float, lon: float, start: date, end: date, retries=3):
    # Only temperature_2m_max: that's the only variable the calculation uses,
    # and Open-Meteo appears to weight request cost by variables x date-range
    # (a 30y range already made us hit the hourly quota fast) -- fetching an
    # unused second variable was silently doubling that cost for nothing.
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&daily=temperature_2m_max&timezone=UTC"
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, context=SSL_CONTEXT, timeout=60) as resp:
                return json.load(resp)["daily"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10)
                continue
            raise
    raise RuntimeError(f"Open-Meteo failed after {retries} retries for {lat},{lon}")


def index_openmeteo(daily, key) -> dict[tuple[int, int], list[tuple[date, float]]]:
    """Index Open-Meteo daily values by (month, day) once per station."""
    index: dict[tuple[int, int], list[tuple[date, float]]] = {}
    for date_str, value in zip(daily["time"], daily[key]):
        if value is None:
            continue
        y, m, d = (int(x) for x in date_str.split("-"))
        index.setdefault((m, d), []).append((date(y, m, d), value))
    return index


def recent_avg_from_index(index, wanted: set[tuple[int, int]], cutoff: date):
    values = []
    for key in wanted:
        for dt, value in index.get(key, []):
            if dt >= cutoff:
                values.append(value)
    return (sum(values) / len(values)) if values else None


def all_days_of_year() -> list[date]:
    days = []
    d = date(2024, 1, 1)  # leap year anchor to include Feb 29
    while d.year == 2024:
        days.append(d)
        d += timedelta(days=1)
    return days


def save(normals: dict, out_path: Path) -> None:
    out_path.write_text(json.dumps(normals, ensure_ascii=False), encoding="utf-8")


def git_commit_and_push(repo_root: Path, message: str) -> None:
    subprocess.run(["git", "add", "normals.json"], cwd=repo_root, check=True)
    result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    if result.returncode == 0:
        print("  (no changes to commit)")
        return
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_root, check=True)
    subprocess.run(["git", "push"], cwd=repo_root, check=True)
    print("  Committed and pushed normals.json")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("regions_path", nargs="?", default="regions.json")
    parser.add_argument("--max", type=int, default=15, help="max regions to process this run")
    parser.add_argument("--no-git", action="store_true", help="skip commit+push (for manual/test runs)")
    args = parser.parse_args()

    regions_path = Path(args.regions_path)
    regions = json.loads(regions_path.read_text(encoding="utf-8"))
    repo_root = Path(__file__).resolve().parent.parent

    out_path = repo_root / "normals.json"
    normals = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    done_before = len(normals)
    print(f"Resuming: {done_before}/{len(regions)} regions already done. "
          f"Processing up to {args.max} more this run.")

    fetch_start = TODAY.replace(year=TODAY.year - 30)
    days = all_days_of_year()
    cutoff30 = TODAY.replace(year=TODAY.year - 30)
    cutoff10 = TODAY.replace(year=TODAY.year - 10)

    processed_this_run = 0
    for i, region in enumerate(regions):
        if processed_this_run >= args.max:
            print(f"Reached --max {args.max} for this run, stopping.")
            break
        rid = region["region_id"]
        if rid in normals:
            continue
        processed_this_run += 1
        print(f"[{i + 1}/{len(regions)}] {rid} ({region['station_name']})...", flush=True)
        try:
            csv_path = download_station_csv(region["station"])
            daily = fetch_openmeteo(region["lat"], region["lon"], fetch_start, TODAY)
            time.sleep(0.3)  # be polite to Open-Meteo across 336 back-to-back requests

            long_index = index_ghcn_csv(csv_path, "TMAX")
            recent_index = index_openmeteo(daily, "temperature_2m_max")

            region_normals = {}
            for target in days:
                doy = target.timetuple().tm_yday
                wanted = day_window(target, WINDOW_DAYS)
                long_avg = avg_from_index(long_index, wanted)
                avg30 = recent_avg_from_index(recent_index, wanted, cutoff30)
                avg10 = recent_avg_from_index(recent_index, wanted, cutoff10)
                if long_avg is None or avg30 is None or avg10 is None:
                    continue
                blend = WEIGHTS["p100"] * long_avg + WEIGHTS["p30"] * avg30 + WEIGHTS["p10"] * avg10
                region_normals[str(doy)] = {
                    "temp": {
                        "p_long": round(long_avg, 1),
                        "p30": round(avg30, 1),
                        "p10": round(avg10, 1),
                        "blend": round(blend, 1),
                    }
                }
            normals[rid] = {
                "meta": {
                    "country": region["country"], "admin1": region["admin1"],
                    "station": region["station"], "station_name": region["station_name"],
                    "lat": region["lat"], "lon": region["lon"],
                    "long_years": region["span"],
                },
                "days": region_normals,
            }
        except Exception as e:  # noqa: BLE001 - one bad region must not kill the whole run
            print(f"  SKIP ({e})")
            continue

        if processed_this_run % 5 == 0:
            save(normals, out_path)  # checkpoint periodically, not just at the very end

    save(normals, out_path)
    print(f"\nWrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB, "
          f"{len(normals)}/{len(regions)} regions total, {processed_this_run} done this run)")

    if not args.no_git and processed_this_run > 0:
        git_commit_and_push(
            repo_root,
            f"Add {processed_this_run} more region normals ({len(normals)}/{len(regions)} total)",
        )


if __name__ == "__main__":
    main()
