"""Geocoding (OpenStreetMap Nominatim) and geographic midpoint math."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import aiohttp

NOMINATIM_URL = "https://nominatim.openstreetmap.org"


class GeocodeError(Exception):
    pass


@dataclass
class Location:
    lat: float
    lon: float
    display_name: str


class Geocoder:
    """Async Nominatim client. Respects the public server's 1 request/second policy."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        user_agent: str,
        country_codes: str | None = None,
        min_interval: float = 1.0,
    ):
        self.session = session
        self.headers = {"User-Agent": user_agent}
        self.country_codes = country_codes
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def _get(self, path: str, params: dict) -> dict | list:
        async with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self.session.get(
                    f"{NOMINATIM_URL}/{path}",
                    params=params,
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        raise GeocodeError(f"Geocoding service returned HTTP {resp.status}")
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise GeocodeError(f"Could not reach geocoding service: {e}") from e
            finally:
                self._last_request = time.monotonic()

    async def geocode(self, query: str) -> Location | None:
        params = {"q": query, "format": "jsonv2", "limit": 1}
        if self.country_codes:
            params["countrycodes"] = self.country_codes
        results = await self._get("search", params)
        if not results:
            return None
        r = results[0]
        return Location(float(r["lat"]), float(r["lon"]), r.get("display_name", query))

    async def reverse(self, lat: float, lon: float) -> str | None:
        params = {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 16, "addressdetails": 1}
        result = await self._get("reverse", params)
        if not result or "error" in result:
            return None
        return short_place_name(result)


def short_place_name(result: dict) -> str:
    """Turn a Nominatim reverse result into something like 'MG Road, Indiranagar, Bengaluru'."""
    addr = result.get("address", {})
    groups = [
        ("road", "pedestrian", "square"),
        ("neighbourhood", "suburb", "quarter", "city_district", "hamlet"),
        ("city", "town", "village", "municipality", "county"),
    ]
    parts = []
    for keys in groups:
        for k in keys:
            if addr.get(k) and addr[k] not in parts:
                parts.append(addr[k])
                break
    if parts:
        return ", ".join(parts)
    return result.get("display_name", "Unknown place")


def midpoint(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Geographic midpoint (centroid on the sphere) of (lat, lon) pairs in degrees.

    Averages 3D unit vectors instead of raw lat/lon so it stays correct across
    the antimeridian and at high latitudes.
    """
    if not points:
        raise ValueError("need at least one point")
    x = y = z = 0.0
    for lat, lon in points:
        la, lo = math.radians(lat), math.radians(lon)
        x += math.cos(la) * math.cos(lo)
        y += math.cos(la) * math.sin(lo)
        z += math.sin(la)
    n = len(points)
    x, y, z = x / n, y / n, z / n
    hyp = math.hypot(x, y)
    if hyp < 1e-12 and abs(z) < 1e-12:
        raise ValueError("points cancel out (e.g. exact opposite sides of the Earth)")
    return math.degrees(math.atan2(z, hyp)), math.degrees(math.atan2(y, x))


def map_links(lat: float, lon: float) -> tuple[str, str]:
    google = f"https://www.google.com/maps/search/?api=1&query={lat:.5f},{lon:.5f}"
    osm = f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=15/{lat:.5f}/{lon:.5f}"
    return google, osm
