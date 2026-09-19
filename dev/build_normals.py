"""Generalized version of pilot_normals.py: computes the blended climate
normal (temperature, precipitation, wind) for every region in regions.json,
for all 366 days of the year, and writes normals.json.

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
WEIGHTS = {"p_long": 0.20, "p30": 0.35, "p10": 0.45}
TODAY = date(2026, 9, 16)

# Disjoint windows (not nested): p10 = last 10y, p30 = the 30y *before* that
# (10-40y back), p_long = everything older than 40y back. Each year of
# history counts toward exactly one component now, not up to three.
YEARS_RECENT = 10
YEARS_MID = 40  # p30 covers YEARS_RECENT..YEARS_MID years back
# Temperature's p_long comes from GHCN (often 100+ years of per-station
# history, see ghcn_avg_before_year below); precip/wind have no GHCN path
# here (AWND wind coverage in GHCN is too sparse historically to trust),
# so their own p_long comes from this same Open-Meteo fetch instead --
# needs to reach past YEARS_MID or that bucket would be empty. Deliberately
# NOT widened to GHCN-like depth: every extra year here multiplies
# Open-Meteo's per-request cost, on top of the ~3x from adding
# precipitation_sum + wind_speed_10m_max to the same request (this build
# already hit Open-Meteo's daily quota once with 1 variable/40y).
FETCH_YEARS = 55


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


def index_ghcn_csv(csv_path: Path, element: str) -> dict[tuple[int, int], list[tuple[int, float]]]:
    """Read a station CSV once; index (year, value) by (month, day)."""
    index: dict[tuple[int, int], list[tuple[int, float]]] = {}
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["ELEMENT"] != element or row["Q_FLAG"]:
                continue
            d = row["DATE"]
            year, m, day = int(d[0:4]), int(d[4:6]), int(d[6:8])
            index.setdefault((m, day), []).append((year, int(row["DATA_VALUE"]) / 10.0))
    return index


def ghcn_avg_before_year(index, wanted: set[tuple[int, int]], cutoff_year: int):
    """Average of values strictly older than cutoff_year (the disjoint 'long' window)."""
    values = []
    for key in wanted:
        for year, value in index.get(key, []):
            if year < cutoff_year:
                values.append(value)
    return (sum(values) / len(values)) if values else None


def fetch_openmeteo(lat: float, lon: float, start: date, end: date, retries=3):
    # temperature_2m_max, precipitation_sum, wind_speed_10m_max. Open-Meteo
    # appears to weight request cost by variables x date-range (a 30y range
    # already made us hit the daily quota once with a single variable) --
    # three variables over FETCH_YEARS is a real, deliberate cost increase,
    # not an oversight; if the daily quota starts getting hit again during
    # the precip/wind backfill, dial back FETCH_YEARS or --max before
    # assuming something else is wrong.
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&daily=temperature_2m_max,precipitation_sum,wind_speed_10m_max&timezone=UTC"
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


def recent_avg_from_index(index, wanted: set[tuple[int, int]], min_date: date, max_date: date | None = None):
    """Average of values in [min_date, max_date) -- max_date=None means no upper bound."""
    values = []
    for key in wanted:
        for dt, value in index.get(key, []):
            if dt < min_date:
                continue
            if max_date is not None and dt >= max_date:
                continue
            values.append(value)
    return (sum(values) / len(values)) if values else None


def all_days_of_year() -> list[date]:
    days = []
    d = date(2024, 1, 1)  # leap year anchor to include Feb 29
    while d.year == 2024:
        days.append(d)
        d += timedelta(days=1)
    return days


def region_needs_backfill(entry: dict) -> bool:
    """True if this region's data predates the precip/wind addition (or is
    otherwise incomplete) and should be reprocessed."""
    days = entry.get("days") or {}
    if not days:
        return True
    sample = next(iter(days.values()))
    return "precip" not in sample or "wind" not in sample


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
    done_before = sum(1 for entry in normals.values() if not region_needs_backfill(entry))
    print(f"Resuming: {done_before}/{len(regions)} regions fully done "
          f"(temp+precip+wind), {len(normals)} have at least temp. "
          f"Processing up to {args.max} more this run.")

    fetch_start = TODAY.replace(year=TODAY.year - FETCH_YEARS)
    days = all_days_of_year()
    cutoff_recent = TODAY.replace(year=TODAY.year - YEARS_RECENT)  # p10 lower bound
    cutoff_mid = TODAY.replace(year=TODAY.year - YEARS_MID)  # p30 lower bound / p_long upper bound

    processed_this_run = 0
    for i, region in enumerate(regions):
        if processed_this_run >= args.max:
            print(f"Reached --max {args.max} for this run, stopping.")
            break
        rid = region["region_id"]
        if rid in normals and not region_needs_backfill(normals[rid]):
            continue
        processed_this_run += 1
        print(f"[{i + 1}/{len(regions)}] {rid} ({region['station_name']})...", flush=True)
        try:
            csv_path = download_station_csv(region["station"])
            daily = fetch_openmeteo(region["lat"], region["lon"], fetch_start, TODAY)
            time.sleep(0.3)  # be polite to Open-Meteo across 336 back-to-back requests

            long_index = index_ghcn_csv(csv_path, "TMAX")
            recent_index = index_openmeteo(daily, "temperature_2m_max")
            precip_index = index_openmeteo(daily, "precipitation_sum")
            wind_index = index_openmeteo(daily, "wind_speed_10m_max")

            region_normals = {}
            for target in days:
                doy = target.timetuple().tm_yday
                wanted = day_window(target, WINDOW_DAYS)
                # disjoint: p10 = last 10y, p30 = 10-40y back, p_long = 40y+ back
                long_avg = ghcn_avg_before_year(long_index, wanted, cutoff_mid.year)
                avg30 = recent_avg_from_index(recent_index, wanted, cutoff_mid, cutoff_recent)
                avg10 = recent_avg_from_index(recent_index, wanted, cutoff_recent)
                if long_avg is None or avg30 is None or avg10 is None:
                    continue
                blend = WEIGHTS["p_long"] * long_avg + WEIGHTS["p30"] * avg30 + WEIGHTS["p10"] * avg10
                day_entry = {
                    "temp": {
                        "p_long": round(long_avg, 1),
                        "p30": round(avg30, 1),
                        "p10": round(avg10, 1),
                        "blend": round(blend, 1),
                    }
                }

                # precip/wind: same disjoint scheme, but all three windows
                # come from this one Open-Meteo fetch (no GHCN path) -- so
                # p_long here means "cutoff_mid..fetch_start years back"
                # (FETCH_YEARS-40, e.g. 40-55y), not GHCN's often much
                # longer unbounded history like temperature's p_long.
                for var_name, index in (("precip", precip_index), ("wind", wind_index)):
                    v_long = recent_avg_from_index(index, wanted, fetch_start, cutoff_mid)
                    v30 = recent_avg_from_index(index, wanted, cutoff_mid, cutoff_recent)
                    v10 = recent_avg_from_index(index, wanted, cutoff_recent)
                    if v_long is None or v30 is None or v10 is None:
                        continue
                    v_blend = WEIGHTS["p_long"] * v_long + WEIGHTS["p30"] * v30 + WEIGHTS["p10"] * v10
                    day_entry[var_name] = {
                        "p_long": round(v_long, 1),
                        "p30": round(v30, 1),
                        "p10": round(v10, 1),
                        "blend": round(v_blend, 1),
                    }

                region_normals[str(doy)] = day_entry
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
    done_after = sum(1 for entry in normals.values() if not region_needs_backfill(entry))
    print(f"\nWrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB, "
          f"{done_after}/{len(regions)} regions fully done, {processed_this_run} done this run)")

    if not args.no_git and processed_this_run > 0:
        git_commit_and_push(
            repo_root,
            f"Backfill precip/wind for {processed_this_run} more regions ({done_after}/{len(regions)} fully done)",
        )


if __name__ == "__main__":
    main()
