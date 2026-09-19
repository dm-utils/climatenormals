"""Shared helpers for the api/ serverless functions.

Filename starts with `_` so Vercel's Python builder does NOT turn this
into its own endpoint -- it's a plain importable module for normal.py
(and future functions) to share.

Uses Upstash Redis (via its REST API, no client library needed -- same
"plain urllib call" style as the TomTom geocoding call in normal.py) for
things that need to survive across serverless invocations, which
in-memory state cannot:
  - a long-lived cache of address -> (lat, lon) geocode results, so a
    repeated address never re-hits TomTom's paid API
  - a simple fixed-window per-IP request counter, as a rate-limit backstop
  - a short-lived cache of each region's Open-Meteo forecast, so the
    day-15-30 trend blend doesn't refetch per request (same quota risk as
    the archive API in dev/build_normals.py -- cached from day one here)

All fail OPEN: if Redis or Open-Meteo is unreachable, requests are still
served (uncached / unlimited / without the trend blend) rather than
breaking the API. A degraded safety net is better than an outage caused by
the safety net.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_KV_URL = os.environ.get("KV_REST_API_URL")
_KV_TOKEN = os.environ.get("KV_REST_API_TOKEN")

RATE_LIMIT_MAX_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60
GEOCODE_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
FORECAST_CACHE_TTL_SECONDS = 12 * 3600  # 12 hours


def _redis(*command: str):
    """Run a single Redis command via Upstash's REST API. Returns the
    `result` field, or None on any failure (network, auth, Redis down)."""
    if not _KV_URL or not _KV_TOKEN:
        return None
    try:
        req = urllib.request.Request(
            _KV_URL,
            data=json.dumps(list(command)).encode("utf-8"),
            headers={"Authorization": f"Bearer {_KV_TOKEN}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.load(resp).get("result")
    except Exception:  # noqa: BLE001 -- Redis being unavailable must never break the API
        return None


def client_ip(headers) -> str:
    """Best-effort client IP from the headers Vercel's edge sets. Falls
    back to a constant so a missing header degrades to "one shared
    bucket" rather than crashing."""
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return headers.get("x-real-ip") or headers.get("X-Real-Ip") or "unknown"


def check_rate_limit(ip: str) -> bool:
    """Fixed-window counter: RATE_LIMIT_MAX_REQUESTS per IP per
    RATE_LIMIT_WINDOW_SECONDS. Returns True if the request is allowed.
    Fails open (returns True) if Redis is unreachable."""
    key = f"ratelimit:{ip}"
    count = _redis("INCR", key)
    if count is None:
        return True
    if count == 1:
        _redis("EXPIRE", key, str(RATE_LIMIT_WINDOW_SECONDS))
    return count <= RATE_LIMIT_MAX_REQUESTS


def geocode_cache_get(address: str):
    """Returns a cached (lat, lon) tuple, or None on a cache miss/error."""
    cached = _redis("GET", f"geocode:{address.strip().lower()}")
    if not cached:
        return None
    try:
        lat_str, lon_str = cached.split(",")
        return float(lat_str), float(lon_str)
    except (ValueError, AttributeError):
        return None


def geocode_cache_set(address: str, lat: float, lon: float) -> None:
    _redis(
        "SET", f"geocode:{address.strip().lower()}", f"{lat},{lon}",
        "EX", str(GEOCODE_CACHE_TTL_SECONDS),
    )


def get_forecast(region_id: str, lat: float, lon: float) -> dict | None:
    """Returns {iso_date: temp_max} for the next ~14 days for this region's
    reference-station coordinates, or None if unavailable (Open-Meteo
    down/quota, or Redis + Open-Meteo both failing).

    Cached per REGION (not per exact address/coordinates) so the cache
    stays bounded to the known region count and a burst of different
    addresses resolving to the same region shares one cache entry."""
    cache_key = f"forecast:{region_id}"
    cached = _redis("GET", cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (ValueError, TypeError):
            pass  # fall through and refetch

    # forecast_days=16 (Open-Meteo's max for the free tier) so "day 14"
    # (today + 14, inclusive) is actually covered -- forecast_days=N returns
    # today plus N-1 more days, so N=14 would stop one day short of it.
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&daily=temperature_2m_max"
        "&forecast_days=16&timezone=UTC"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            daily = json.load(resp)["daily"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        return None

    forecast = {
        d: v for d, v in zip(daily["time"], daily["temperature_2m_max"]) if v is not None
    }
    _redis("SET", cache_key, json.dumps(forecast), "EX", str(FORECAST_CACHE_TTL_SECONDS))
    return forecast
