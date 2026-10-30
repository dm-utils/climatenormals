"""Day details per region, measured at its reference station: for every day of
the year and each of the three periods the average daytime high and night low
and the usual range of highs, the warmest day and coldest night on record, and
how fast that time of year has been warming. Writes details/<region_id>.json.

Uses only the station records already in dev/data (download_station_csv
fetches any that are missing), so it makes no Open-Meteo requests and can be
run again at any time:

    python dev/build_details.py

The periods are those of the normals (build_normals.py), but by calendar year
and from the station itself, where the normals' recent periods come from ERA5.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from build_normals import TODAY, WINDOW_DAYS, YEARS_MID, YEARS_RECENT, all_days_of_year, day_window, download_station_csv

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "details"
REGIONS = Path(__file__).resolve().parent / "regions.json"

# p_long: before FIRST_MID; p30: FIRST_MID up to FIRST_RECENT; p10: FIRST_RECENT on.
FIRST_RECENT = TODAY.year - YEARS_RECENT
FIRST_MID = TODAY.year - YEARS_MID
# Trend and decades from here on: older records are few and less comparable.
TREND_FROM = 1950
TREND_MIN_YEARS = 30
# A year counts for the trend when most days around the date were measured.
MIN_DAYS_IN_WINDOW = 2 * WINDOW_DAYS + 1 - 5
MIN_YEARS_IN_DECADE = 5
# A recent period only from the station when it measured most of its years (as correct_normals.py does).
MIN_YEARS = {"p_long": 1, "p30": 20, "p10": 8}


def period_of(year: int) -> str:
    return "p10" if year >= FIRST_RECENT else "p30" if year >= FIRST_MID else "p_long"


def read_station(path: Path) -> tuple[dict, dict]:
    """TMAX and TMIN in degrees, by (month, day) and then year."""
    highs: dict = defaultdict(dict)
    lows: dict = defaultdict(dict)
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            element = row["ELEMENT"]
            if element not in ("TMAX", "TMIN") or row["Q_FLAG"]:
                continue
            d = row["DATE"]
            year, key = int(d[0:4]), (int(d[4:6]), int(d[6:8]))
            (highs if element == "TMAX" else lows)[key][year] = int(row["DATA_VALUE"]) / 10.0
    return highs, lows


def percentile(sorted_values: list[float], q: float) -> float:
    i = (len(sorted_values) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (i - lo)


def slope(points: list[tuple[int, float]]) -> float:
    """Least-squares slope, in degrees per year."""
    mx = statistics.fmean(x for x, _ in points)
    my = statistics.fmean(y for _, y in points)
    return sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)


def r1(v: float) -> float:
    return round(v, 1)


def day_details(highs: dict, lows: dict, target: date) -> dict | None:
    wanted = day_window(target, WINDOW_DAYS)
    out: dict = {"w": {}}
    per_year: dict = defaultdict(int)
    for k in wanted:
        for y in highs.get(k, {}):
            per_year[y] += 1
    for period in ("p_long", "p30", "p10"):
        if sum(1 for y, n in per_year.items() if period_of(y) == period and n >= MIN_DAYS_IN_WINDOW) < MIN_YEARS[period]:
            continue
        hi = sorted(v for k in wanted for y, v in highs.get(k, {}).items() if period_of(y) == period)
        lo = [v for k in wanted for y, v in lows.get(k, {}).items() if period_of(y) == period]
        if hi and lo:
            # Average high, average low, and the range of highs on 8 days in 10.
            out["w"][period] = [r1(statistics.fmean(hi)), r1(statistics.fmean(lo)), r1(percentile(hi, 0.1)), r1(percentile(hi, 0.9))]
    if not out["w"]:
        return None

    # Records of the day itself: the warmest afternoon and the coldest night, with the year.
    key = (target.month, target.day)
    if highs.get(key):
        year, value = max(highs[key].items(), key=lambda t: (t[1], t[0]))
        out["rh"] = [r1(value), year]
    if lows.get(key):
        year, value = min(lows[key].items(), key=lambda t: (t[1], -t[0]))
        out["rl"] = [r1(value), year]

    # Per year the average high around this date; their trend and their averages per decade.
    by_year: dict = defaultdict(list)
    for k in wanted:
        for y, v in highs.get(k, {}).items():
            if y >= TREND_FROM:
                by_year[y].append(v)
    yearly = sorted((y, statistics.fmean(vs)) for y, vs in by_year.items() if len(vs) >= MIN_DAYS_IN_WINDOW)
    if len(yearly) >= TREND_MIN_YEARS:
        out["tr"] = round(slope(yearly) * 10, 2)
    decades: dict = defaultdict(list)
    for y, v in yearly:
        decades[y // 10 * 10].append(v)
    out["dec"] = {str(d): r1(statistics.fmean(vs)) for d, vs in sorted(decades.items()) if len(vs) >= MIN_YEARS_IN_DECADE}
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    regions = json.loads(REGIONS.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(exist_ok=True)
    days = all_days_of_year()
    for i, region in enumerate(regions, 1):
        highs, lows = read_station(download_station_csv(region["station"]))
        result = {
            "station": region["station_name"],
            "periods": {"p_long": [None, FIRST_MID - 1], "p30": [FIRST_MID, FIRST_RECENT - 1], "p10": [FIRST_RECENT, None]},
            "trend_from": TREND_FROM,
            "days": {},
        }
        for target in days:
            d = day_details(highs, lows, target)
            if d:
                result["days"][str(target.timetuple().tm_yday)] = d
        (OUT_DIR / f"{region['region_id']}.json").write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
        print(f"[{i}/{len(regions)}] {region['region_id']}: {len(result['days'])} days", flush=True)


if __name__ == "__main__":
    main()
