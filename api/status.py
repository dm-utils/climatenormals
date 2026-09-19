"""Vercel Python serverless function: GET /api/status

Simple progress check: how many of the known regions have precomputed
normals so far, broken down by country -- and separately, how many of
those also have the precip/wind backfill (dev/build_normals.py's
region_needs_backfill), since that runs as a second pass over regions
that already have temp. Useful to poll while the incremental build
(run on a schedule) is still filling in normals.json.
"""

from __future__ import annotations

import json
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path

NORMALS_PATH = Path(__file__).resolve().parent.parent / "normals.json"
REGIONS_PATH = Path(__file__).resolve().parent.parent / "dev" / "regions.json"
_regions_cache = None
_normals_cache = None


def load_regions() -> list:
    global _regions_cache
    if _regions_cache is None:
        _regions_cache = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    return _regions_cache


def load_normals() -> dict:
    global _normals_cache
    if _normals_cache is None:
        _normals_cache = json.loads(NORMALS_PATH.read_text(encoding="utf-8")) if NORMALS_PATH.exists() else {}
    return _normals_cache


def has_precip_wind(entry: dict) -> bool:
    days = entry.get("days") or {}
    if not days:
        return False
    sample = next(iter(days.values()))
    return "precip" in sample and "wind" in sample


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        regions = load_regions()
        normals = load_normals()

        total_by_country = Counter(r["country"] for r in regions)
        done_by_country = Counter(
            r["country"] for r in regions if r["region_id"] in normals
        )
        precip_wind_done = sum(1 for entry in normals.values() if has_precip_wind(entry))
        countries = sorted(total_by_country.keys())

        body = {
            "total_regions": len(regions),
            "done_regions": len(normals),
            "percent": round(100 * len(normals) / len(regions), 1) if regions else 0,
            "precip_wind_done_regions": precip_wind_done,
            "precip_wind_percent": round(100 * precip_wind_done / len(regions), 1) if regions else 0,
            "by_country": {
                cc: {"done": done_by_country.get(cc, 0), "total": total_by_country[cc]}
                for cc in countries
            },
        }

        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(payload)
