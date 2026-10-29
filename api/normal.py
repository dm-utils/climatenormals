"""Vercel Python serverless function: GET /api/normal

Query params:
  lat, lon        - coordinates (use these, or `address`)
  address         - free-text address, geocoded via TomTom (needs TOMTOM_API_KEY env var)
  date            - YYYY-MM-DD (defaults to today)
  w_long, w_30, w_10 - optional custom weights (0-1) for the three disjoint
                    windows (40y+ back / 10-40y back / last 10y). Must sum
                    to 1. Free to customize: the three components are
                    stored separately, so a custom blend is just a
                    different weighted sum of already-cached numbers, no
                    recomputation needed. Default: 0.20 / 0.35 / 0.45.

Resolves the nearest known region by simple haversine distance to each
region's reference-station coordinates (no reverse-geocoding product
needed), then looks up the precomputed normal components for that
day-of-year from normals.json (bundled with the deployment, built by
dev/build_normals.py).

Response includes "temp" (deg C) always, plus "precip" (mm) and "wind"
(km/h, max daily) once this region's backfill has reached them -- omitted
entirely for regions still pending (see region_needs_backfill in
dev/build_normals.py). Same w_long/w_30/w_10 weights apply to all three.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Vercel's Python runtime imports this file in isolation -- its own
# directory isn't on sys.path by default, so a sibling import needs a
# manual nudge.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _shared import check_rate_limit, client_ip, geocode_cache_get, geocode_cache_set, get_forecast  # noqa: E402

DEFAULT_WEIGHTS = {"p_long": 0.20, "p30": 0.35, "p10": 0.45}
# Cached normals only change when the (now-complete) build re-runs, which
# is rare -- safe to let Vercel's CDN absorb repeat identical queries.
CACHE_CONTROL = "public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800"

# Day 15-30 "trend/anomaly persistence" blend: nudge the climate normal by
# the recent forecast's deviation from normal, fading the nudge out as the
# forecast signal gets less trustworthy. A real, named technique in
# subseasonal forecasting (e.g. NOAA CPC's 3-4 week outlooks) -- not a
# substitute for an actual forecast, but more grounded than pure guessing.
FORECAST_HORIZON_DAYS = 14  # 0-14: real Open-Meteo forecast, no blending
TREND_ZONE_END_DAYS = 30  # 15-30: trend-nudged climate normal
TREND_WEIGHT_AT_15 = 0.30
TREND_DECAY_TAU = 6.5  # weight(30) ~= 0.03 with these two constants
NORMALS_PATH = Path(__file__).resolve().parent.parent / "normals.json"
REGIONS_PATH = Path(__file__).resolve().parent.parent / "dev" / "regions.json"
_normals_cache = None
_regions_cache = None


def load_normals() -> dict:
    global _normals_cache
    if _normals_cache is None:
        _normals_cache = json.loads(NORMALS_PATH.read_text(encoding="utf-8"))
    return _normals_cache


def load_regions() -> list:
    """The FULL list of all known regions (even ones not computed yet) --
    nearest-region matching must consider all of them, not just the ones
    that happen to already have data, or a query near a not-yet-computed
    region silently snaps to some unrelated region on the other side of
    the planet that does have data (found the hard way: Sydney matched to
    Aguascalientes, Mexico, 12,819 km away)."""
    global _regions_cache
    if _regions_cache is None:
        _regions_cache = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    return _regions_cache


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_region(regions: list, lat: float, lon: float) -> tuple[dict, float]:
    best, best_dist = None, float("inf")
    for region in regions:
        d = haversine_km(lat, lon, region["lat"], region["lon"])
        if d < best_dist:
            best, best_dist = region, d
    return best, best_dist


def trend_weight(horizon_days: int) -> float:
    """0 outside [15, 30]; decays from TREND_WEIGHT_AT_15 at day 15 to
    ~3% of that at day 30 (see module docstring above)."""
    if horizon_days < 15 or horizon_days > TREND_ZONE_END_DAYS:
        return 0.0
    return TREND_WEIGHT_AT_15 * math.exp(-(horizon_days - 15) / TREND_DECAY_TAU)


def variable_blend(day_data: dict, var: str, weights: dict) -> float | None:
    """Climate-normal blend for one variable ("temp"/"precip"/"wind") in an
    already-resolved day entry. None if this region/day doesn't have that
    variable yet (precip/wind backfill is still in progress for some
    regions; temp has been complete since the original build)."""
    v = day_data.get(var)
    if v is None:
        return None
    return weights["p_long"] * v["p_long"] + weights["p30"] * v["p30"] + weights["p10"] * v["p10"]


def normals_day(d: date) -> int:
    """The key of a date in normals.json: its day of the year in 2024, the leap year
    the normals were built for. In an ordinary year 1 March is day 60, but in the
    normals it is day 61 (day 60 is 29 February)."""
    return date(2024, d.month, d.day).timetuple().tm_yday


def region_blend_for_date(region: dict, d: date, weights: dict, var: str = "temp") -> float | None:
    """Same as variable_blend, but looks up an arbitrary date within the
    same region first -- used to compute how far the recent forecast
    deviates from normal, for dates other than the one being requested."""
    day_data = region["days"].get(str(normals_day(d)))
    if day_data is None:
        return None
    return variable_blend(day_data, var, weights)


def parse_weights(params: dict) -> dict:
    if not ({"w_long", "w_30", "w_10"} & params.keys()):
        return DEFAULT_WEIGHTS
    try:
        w_long = float(params.get("w_long", [DEFAULT_WEIGHTS["p_long"]])[0])
        w_30 = float(params.get("w_30", [DEFAULT_WEIGHTS["p30"]])[0])
        w_10 = float(params.get("w_10", [DEFAULT_WEIGHTS["p10"]])[0])
    except ValueError as e:
        raise ValueError("w_long/w_30/w_10 must be numbers") from e
    total = w_long + w_30 + w_10
    if not (0.99 <= total <= 1.01):
        raise ValueError(f"w_long + w_30 + w_10 must sum to 1 (got {total})")
    return {"p_long": w_long, "p30": w_30, "p10": w_10}


def geocode_address(address: str) -> tuple[float, float]:
    cached = geocode_cache_get(address)
    if cached is not None:
        return cached

    api_key = os.environ["TOMTOM_API_KEY"]
    url = f"https://api.tomtom.com/search/2/geocode/{urllib.parse.quote(address)}.json?key={api_key}&limit=1"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.load(resp)
    results = data.get("results") or []
    if not results:
        raise ValueError(f"No geocode result for: {address}")
    pos = results[0]["position"]
    lat, lon = pos["lat"], pos["lon"]
    geocode_cache_set(address, lat, lon)
    return lat, lon


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if not check_rate_limit(client_ip(self.headers)):
            self._json_response(429, {
                "error": "Rate limit exceeded (max 60 requests/minute per IP).",
                "contact": "Need a higher limit for a legitimate use case? "
                           "Get in touch via https://datamodder.com/contact and we'll work something out.",
            }, retry_after=60)
            return

        try:
            try:
                weights = parse_weights(params)
            except ValueError as e:
                self._json_response(400, {"error": str(e)})
                return

            if "lat" in params and "lon" in params:
                lat, lon = float(params["lat"][0]), float(params["lon"][0])
            elif "address" in params:
                lat, lon = geocode_address(params["address"][0])
            else:
                self._json_response(400, {"error": "provide either lat+lon or address"})
                return

            target_date = (
                datetime.strptime(params["date"][0], "%Y-%m-%d").date()
                if "date" in params
                else date.today()
            )
            day_of_year = normals_day(target_date)

            regions = load_regions()
            nearest, distance_km = nearest_region(regions, lat, lon)
            if nearest is None:
                self._json_response(404, {"error": "no known region at all (empty regions.json)"})
                return
            region_id = nearest["region_id"]

            normals = load_normals()
            region = normals.get(region_id)
            day_data = region["days"].get(str(day_of_year)) if region else None

            if day_data is None:
                self._json_response(202, {
                    "region": region_id,
                    "region_meta": {
                        "country": nearest["country"], "admin1": nearest["admin1"],
                        "station": nearest["station"], "station_name": nearest["station_name"],
                        "lat": nearest["lat"], "lon": nearest["lon"],
                    },
                    "distance_to_reference_station_km": round(distance_km, 1),
                    "error": "this is the correct nearest region, but its data hasn't been "
                             "computed yet -- the incremental build is still filling in regions",
                })
                return

            custom_blend = variable_blend(day_data, "temp", weights)
            resp_temp = {**day_data["temp"], "blend": round(custom_blend, 1)}
            forecast_trend = None

            # precip/wind: same climate-normal blend as temp, but only when
            # this region's backfill (see dev/build_normals.py) has reached
            # them yet -- None means "not there yet", field omitted below.
            resp_precip = None
            precip_blend = variable_blend(day_data, "precip", weights)
            if precip_blend is not None:
                resp_precip = {**day_data["precip"], "blend": round(precip_blend, 1)}

            resp_wind = None
            wind_blend = variable_blend(day_data, "wind", weights)
            if wind_blend is not None:
                resp_wind = {**day_data["wind"], "blend": round(wind_blend, 1)}

            horizon_days = (target_date - date.today()).days
            if 0 <= horizon_days <= TREND_ZONE_END_DAYS:
                forecast = get_forecast(region_id, nearest["lat"], nearest["lon"])
                if forecast is not None:
                    if horizon_days <= FORECAST_HORIZON_DAYS:
                        # 0-14 days: real forecast, pass through as-is for
                        # every variable that has both forecast + normal data.
                        fc = forecast.get(target_date.isoformat())
                        if fc is not None:
                            if "temp" in fc:
                                resp_temp["forecast_adjusted"] = round(fc["temp"], 1)
                                forecast_trend = {
                                    "method": "raw_forecast", "horizon_days": horizon_days,
                                    "trend_weight": 1.0,
                                }
                            if resp_precip is not None and "precip" in fc:
                                resp_precip["forecast_adjusted"] = round(fc["precip"], 1)
                            if resp_wind is not None and "wind" in fc:
                                resp_wind["forecast_adjusted"] = round(fc["wind"], 1)
                    else:
                        # 15-30 days: trend/anomaly persistence nudge --
                        # temperature only (see module docstring). Precip is
                        # far too day-to-day chaotic for "current trend
                        # persists, fading out" to carry real signal the way
                        # it does for temperature, so precip/wind just keep
                        # their plain climate blend from above, unnudged.
                        anomalies = []
                        for offset in range(8, 15):
                            d = date.today() + timedelta(days=offset)
                            fc = forecast.get(d.isoformat())
                            fc_temp = fc.get("temp") if fc else None
                            normal_temp = region_blend_for_date(region, d, weights, "temp")
                            if fc_temp is not None and normal_temp is not None:
                                anomalies.append(fc_temp - normal_temp)
                        if anomalies:
                            anomaly_c = sum(anomalies) / len(anomalies)
                            w = trend_weight(horizon_days)
                            resp_temp["forecast_adjusted"] = round(custom_blend + w * anomaly_c, 1)
                            forecast_trend = {
                                "method": "trend_blend", "horizon_days": horizon_days,
                                "trend_weight": round(w, 3), "anomaly_c": round(anomaly_c, 1),
                            }
                # forecast is None (Open-Meteo/Redis unavailable): silently fall back to
                # the plain climate-normal response above -- no forecast_trend, no error.

            body = {
                "region": region_id,
                "region_meta": region["meta"],
                "distance_to_reference_station_km": round(distance_km, 1),
                "date": target_date.isoformat(),
                "weights_used": weights,
                "temp": resp_temp,
            }
            if resp_precip is not None:
                body["precip"] = resp_precip
            if resp_wind is not None:
                body["wind"] = resp_wind
            if forecast_trend is not None:
                body["forecast_trend"] = forecast_trend
            self._json_response(200, body, cache=True)
        except Exception as e:  # noqa: BLE001
            self._json_response(500, {"error": str(e)})

    def _json_response(self, status: int, body: dict, cache: bool = False, retry_after: int | None = None):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        if cache:
            self.send_header("Cache-Control", CACHE_CONTROL)
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(payload)
