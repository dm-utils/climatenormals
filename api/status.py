"""Vercel Python serverless function: GET /api/status

Simple progress check: how many of the known regions have precomputed
normals so far, broken down by country. Useful to poll while the
incremental build (dev/build_normals.py, run on a schedule) is still
filling in normals.json.
"""

from __future__ import annotations

import json
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path

NORMALS_PATH = Path(__file__).resolve().parent.parent / "normals.json"
REGIONS_PATH = Path(__file__).resolve().parent.parent / "dev" / "regions.json"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        regions = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
        normals = json.loads(NORMALS_PATH.read_text(encoding="utf-8")) if NORMALS_PATH.exists() else {}

        total_by_country = Counter(r["country"] for r in regions)
        done_by_country = Counter(
            r["country"] for r in regions if r["region_id"] in normals
        )
        countries = sorted(total_by_country.keys())

        body = {
            "total_regions": len(regions),
            "done_regions": len(normals),
            "percent": round(100 * len(normals) / len(regions), 1) if regions else 0,
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
        self.end_headers()
        self.wfile.write(payload)
