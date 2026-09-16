"""Pilot validation: full climate-normals pipeline for ONE NL region (Zuid /
Maastricht) and ONE DE region (Thueringen / Jena) — validates the approach
before scaling to all 26 countries.

Long window: NOAA GHCN-Daily per-station CSV (already downloaded to dev/data/).
Recent windows (30y/10y): Open-Meteo Historical Weather API (ERA5), fetched live.

Usage: python dev/pilot_normals.py
"""

from __future__ import annotations

import csv
import json
import ssl
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

WINDOW_DAYS = 7  # +/- days around the target day-of-year, per region/day bucket
WEIGHTS = {"p100": 0.4, "p30": 0.3, "p10": 0.3}

PILOT_REGIONS = {
    "NL-zuid": {"station": "NLM00006380", "lat": 50.9053, "lon": 5.7617, "name": "Maastricht"},
    "DE-thueringen": {"station": "GM000004204", "lat": 50.9267, "lon": 11.5842, "name": "Jena Sternwarte"},
}

TEST_DATES = [date(2026, 7, 15), date(2026, 12, 15)]


def day_of_year_window(target: date, window: int) -> set[tuple[int, int]]:
    """(month, day) pairs within +/- window days of target, ignoring year."""
    pairs = set()
    for offset in range(-window, window + 1):
        d = date(2001, target.month, target.day) + timedelta(days=offset)  # non-leap anchor year
        pairs.add((d.month, d.day))
    return pairs


def load_ghcn_long_window(csv_path: Path, element: str, target: date, window: int) -> tuple[float | None, int]:
    """Average of `element` (TMAX/TMIN) over all years in the file, for dates
    within +/-window days of target's month/day. Returns (avg_celsius, n_years_span)."""
    wanted = day_of_year_window(target, window)
    values = []
    years = set()
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["ELEMENT"] != element or row["Q_FLAG"]:
                continue
            d = row["DATE"]
            year, month, day = int(d[0:4]), int(d[4:6]), int(d[6:8])
            if (month, day) not in wanted:
                continue
            values.append(int(row["DATA_VALUE"]) / 10.0)  # tenths of degC -> degC
            years.add(year)
    if not values:
        return None, 0
    span = max(years) - min(years) + 1
    return sum(values) / len(values), span


def fetch_openmeteo_daily(lat: float, lon: float, start: date, end: date) -> dict[str, list]:
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&daily=temperature_2m_max,temperature_2m_min&timezone=UTC"
    )
    with urllib.request.urlopen(url, context=SSL_CONTEXT) as resp:
        return json.load(resp)["daily"]


def recent_window_avg(daily: dict, element_key: str, target: date, window: int, years_back: int, today: date) -> float | None:
    wanted = day_of_year_window(target, window)
    cutoff = today.replace(year=today.year - years_back)
    values = []
    for date_str, value in zip(daily["time"], daily[element_key]):
        if value is None:
            continue
        y, m, d = (int(x) for x in date_str.split("-"))
        if date(y, m, d) < cutoff:
            continue
        if (m, d) in wanted:
            values.append(value)
    return sum(values) / len(values) if values else None


def main() -> None:
    today = date(2026, 9, 16)
    fetch_start = today.replace(year=today.year - 30)

    for region_id, cfg in PILOT_REGIONS.items():
        print(f"\n=== {region_id} ({cfg['name']}, {cfg['lat']}, {cfg['lon']}) ===")
        csv_path = Path(__file__).resolve().parent / "data" / f"{cfg['station']}.csv"

        print("Fetching Open-Meteo ERA5 (30y)...")
        daily = fetch_openmeteo_daily(cfg["lat"], cfg["lon"], fetch_start, today)

        for target in TEST_DATES:
            long_avg, long_years = load_ghcn_long_window(csv_path, "TMAX", target, WINDOW_DAYS)
            avg30 = recent_window_avg(daily, "temperature_2m_max", target, WINDOW_DAYS, 30, today)
            avg10 = recent_window_avg(daily, "temperature_2m_max", target, WINDOW_DAYS, 10, today)

            blend = None
            if long_avg is not None and avg30 is not None and avg10 is not None:
                blend = WEIGHTS["p100"] * long_avg + WEIGHTS["p30"] * avg30 + WEIGHTS["p10"] * avg10

            print(f"  {target.isoformat()} (+/-{WINDOW_DAYS}d)  "
                  f"long({long_years}y)={long_avg:.1f}C  30y={avg30:.1f}C  10y={avg10:.1f}C  "
                  f"-> blend={blend:.1f}C")


if __name__ == "__main__":
    main()
