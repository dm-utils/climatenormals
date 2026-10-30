"""Takes the recent temperature periods (p30, p10) of normals.json from the
reference station itself where its record covers them, and corrects ERA5
where it does not. ERA5 (Open-Meteo, build_normals.py) runs cooler than the
stations, on average 0.7-1.1 degrees and up to 4-5 in mountains and on
islands, so a station-based p_long next to ERA5-based recent periods made the
periods incomparable and the typical highs too low.

Per region, per day and per period:
  - the station's own average, when enough of the period's years were
    measured (MIN_YEARS);
  - otherwise ERA5 plus the station-minus-ERA5 difference over a period
    where both exist (p30, else p10), smoothed over BIAS_DAYS either side;
    for that difference BIAS_MIN_YEARS of the period's years are enough;
  - otherwise ERA5 as it is.

Uses only dev/data and the ERA5 values build_normals.py wrote, which this
script keeps in dev/era5_temp.json the first time, so it makes no Open-Meteo
requests and can be run again at any time (after build_normals.py has run,
delete dev/era5_temp.json first):

    python dev/correct_normals.py
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from build_details import FIRST_MID, MIN_DAYS_IN_WINDOW, MIN_YEARS, period_of, read_station
from build_normals import WEIGHTS, WINDOW_DAYS, all_days_of_year, day_window, download_station_csv

ROOT = Path(__file__).resolve().parent.parent
NORMALS = ROOT / "normals.json"
ERA5 = Path(__file__).resolve().parent / "era5_temp.json"
REGIONS = Path(__file__).resolve().parent / "regions.json"

BIAS_DAYS = 15
BIAS_MIN_YEARS = 5
RECENT = ("p30", "p10")


def station_means(highs: dict, day, min_years: dict) -> dict:
    """Per recent period the station's average high around this day, when enough of its years were measured."""
    wanted = day_window(day, WINDOW_DAYS)
    by_year: dict = defaultdict(list)
    for k in wanted:
        for y, v in highs.get(k, {}).items():
            if y >= FIRST_MID:
                by_year[y].append(v)
    out = {}
    for period in RECENT:
        years = [vs for y, vs in by_year.items() if period_of(y) == period]
        if sum(1 for vs in years if len(vs) >= MIN_DAYS_IN_WINDOW) >= min_years[period]:
            out[period] = statistics.fmean(v for vs in years for v in vs)
    return out


def smoothed(diffs: dict[int, float], doy: int) -> float | None:
    """The average difference over the days around this one (the year wraps around)."""
    near = [diffs[d] for off in range(-BIAS_DAYS, BIAS_DAYS + 1) if (d := (doy - 1 + off) % 366 + 1) in diffs]
    return statistics.fmean(near) if near else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    normals = json.loads(NORMALS.read_text(encoding="utf-8"))
    if not ERA5.exists():
        raw = {rid: {doy: {k: e["temp"][k] for k in RECENT} for doy, e in r["days"].items()} for rid, r in normals.items()}
        ERA5.write_text(json.dumps(raw, separators=(",", ":")), encoding="utf-8")
        print(f"Kept the ERA5 values in {ERA5.name}")
    era5 = json.loads(ERA5.read_text(encoding="utf-8"))
    stations = {r["region_id"]: r["station"] for r in json.loads(REGIONS.read_text(encoding="utf-8"))}

    sources: dict = defaultdict(int)
    shifts = []
    for i, (rid, region) in enumerate(normals.items(), 1):
        highs, _ = read_station(download_station_csv(stations[rid]))
        own = {str(d.timetuple().tm_yday): station_means(highs, d, MIN_YEARS) for d in all_days_of_year()}
        some = {str(d.timetuple().tm_yday): station_means(highs, d, dict.fromkeys(RECENT, BIAS_MIN_YEARS)) for d in all_days_of_year()}
        raw = era5[rid]
        # Station minus ERA5 per day, over p30 where the station has it on most days, else over p10.
        overlap = max(RECENT, key=lambda k: (sum(1 for doy in raw if k in some[doy]), k == "p30"))
        diffs = {int(doy): some[doy][overlap] - raw[doy][overlap] for doy in raw if overlap in some[doy]}
        counts: dict = defaultdict(int)
        for doy, entry in region["days"].items():
            t = entry["temp"]
            for k in RECENT:
                if k in own[doy]:
                    value, source = own[doy][k], "station"
                elif (bias := smoothed(diffs, int(doy))) is not None:
                    value, source = raw[doy][k] + bias, "era5_corrected"
                else:
                    value, source = raw[doy][k], "era5"
                t[k] = round(value, 1)
                counts[(k, source)] += 1
            new_blend = round(sum(WEIGHTS[k] * t[k] for k in WEIGHTS), 1)
            shifts.append(new_blend - t["blend"])
            t["blend"] = new_blend
        # What most days of each period come from, for the API and the site to say.
        region["meta"]["recent_source"] = {
            k: max(("station", "era5_corrected", "era5"), key=lambda s: counts[(k, s)]) for k in RECENT
        }
        for k in RECENT:
            sources[(k, region["meta"]["recent_source"][k])] += 1
        if i % 25 == 0:
            print(f"[{i}/{len(normals)}]", flush=True)

    NORMALS.write_text(json.dumps(normals, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {NORMALS.name}. Regions by source of most days:")
    for k in RECENT:
        print(f"  {k}: " + ", ".join(f"{s} {sources[(k, s)]}" for s in ("station", "era5_corrected", "era5")))
    print(f"Typical high changed by {statistics.fmean(shifts):+.2f} on average "
          f"(from {min(shifts):+.1f} to {max(shifts):+.1f})")


if __name__ == "__main__":
    main()
