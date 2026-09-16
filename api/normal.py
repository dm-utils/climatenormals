"""Vercel Python serverless function: GET /api/normal

Query params:
  lat, lon        - coordinates (use these, or `address`)
  address         - free-text address, geocoded via TomTom (needs TOMTOM_API_KEY env var)
  date            - YYYY-MM-DD (defaults to today)

Resolves the nearest known region by simple haversine distance to each
region's reference-station coordinates (no reverse-geocoding product
needed), then looks up the precomputed blended normal for that
day-of-year from normals.json (bundled with the deployment, built by
dev/build_normals.py).
"""

from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

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


def geocode_address(address: str) -> tuple[float, float]:
    api_key = os.environ["TOMTOM_API_KEY"]
    url = f"https://api.tomtom.com/search/2/geocode/{urllib.parse.quote(address)}.json?key={api_key}&limit=1"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.load(resp)
    results = data.get("results") or []
    if not results:
        raise ValueError(f"No geocode result for: {address}")
    pos = results[0]["position"]
    return pos["lat"], pos["lon"]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        try:
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
            day_of_year = target_date.timetuple().tm_yday

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

            self._json_response(200, {
                "region": region_id,
                "region_meta": region["meta"],
                "distance_to_reference_station_km": round(distance_km, 1),
                "date": target_date.isoformat(),
                **day_data,
            })
        except Exception as e:  # noqa: BLE001
            self._json_response(500, {"error": str(e)})

    def _json_response(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
