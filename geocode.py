"""
geocode.py — Free geocoding via OSM Nominatim. No API key required.
Rate limit: 1 request/second (enforced here).
"""
import re
import time
import logging
import requests

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "GarageSaleMap/1.0 (garagesalemap.app)"}


def normalize_address(address: str) -> str:
    """Fix common bad address formats before geocoding."""
    address = address.strip()
    # "FL 34231" or "FL 34231-1234" → "34231, FL, USA"
    m = re.match(r'^([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', address)
    if m:
        return f"{m.group(2)}, {m.group(1)}, USA"
    # Bare zip "34231" → "34231, USA"
    m2 = re.match(r'^(\d{5}(?:-\d{4})?)$', address)
    if m2:
        return f"{m2.group(1)}, USA"
    return address


def geocode_address(address: str) -> tuple[float, float] | None:
    """Geocode a single address string. Returns (lat, lon) or None."""
    address = normalize_address(address)
    if not address:
        return None
    time.sleep(1.1)  # Nominatim TOS: max 1 req/sec
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers=HEADERS,
            timeout=10,
        )
        data = resp.json()
        if data:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
            log.info(f"Geocoded: {address} → ({lat}, {lon})")
            return lat, lon
    except Exception as e:
        log.warning(f"Geocode failed for '{address}': {e}")
    return None


def geocode_listings(listings):
    """Geocode a list of Listing objects in place. Returns same list."""
    for listing in listings:
        if listing.lat and listing.lon:
            continue
        if not listing.address:
            continue
        coords = geocode_address(listing.address)
        if coords:
            listing.lat, listing.lon = coords
        else:
            log.warning(f"No result for: {listing.address}")
    return listings
