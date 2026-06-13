import time
import random
import logging
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

HEADERS_POOL = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"},
]

FL_ZIPS = [
    "34231","34229","34238","34285","34293",
    "33948","33952","34223","34224","34201",
    "34202","33901","33907","33904","33909",
    "33950","33982","34102","34103","34286","34287"
]

US_ZIPS = {
    "Tampa FL": ["33601","33602"],
    "Orlando FL": ["32801","32803"],
    "Jacksonville FL": ["32202","32204"],
    "Atlanta GA": ["30301","30303"],
    "Houston TX": ["77001","77002"],
    "Dallas TX": ["75201","75203"],
    "Chicago IL": ["60601","60602"],
    "Columbus OH": ["43201","43202"],
    "Charlotte NC": ["28201","28202"],
    "Phoenix AZ": ["85001","85003"],
    "Los Angeles CA": ["90001","90002"],
    "New York NY": ["10001","10002"],
}


@dataclass
class Listing:
    source: str
    title: str
    url: str
    address: Optional[str] = None
    date_text: Optional[str] = None
    description: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


def polite_get(url, retries=3, min_delay=2.0, max_delay=5.0):
    for attempt in range(retries):
        delay = random.uniform(min_delay, max_delay)
        time.sleep(delay)
        try:
            resp = requests.get(
                url,
                headers=random.choice(HEADERS_POOL),
                timeout=20
            )
            if resp.status_code == 200:
                return resp
            elif resp.status_code == 429:
                log.warning("Rate limited - waiting 60s")
                time.sleep(60)
            elif resp.status_code == 403:
                log.warning(f"Blocked 403: {url}")
                return None
            else:
                log.warning(f"HTTP {resp.status_code}: {url}")
        except Exception as e:
            log.error(f"Request error attempt {attempt+1}: {e}")
    return None


def scrape_yardsalesearch_zip(zip_code, radius=25):
    listings = []
    url = f"https://www.yardsalesearch.com/garage-sales.html?zip={zip_code}&radius={radius}"
    log.info(f"Scraping zip={zip_code}")
    resp = polite_get(url)
    if not resp:
        return listings

    soup = BeautifulSoup(resp.text, "html.parser")
    details = soup.find_all("div", class_="sale-details")
    log.info(f"  Found {len(details)} listings for zip {zip_code}")

    for block in details:
        try:
            title_tag = block.find("h2", itemprop="name")
            title = "Yard Sale"
            if title_tag:
                title = title_tag.get_text(strip=True).split("(")[0].strip()

            link_tag = block.find("a", itemprop="url")
            link = link_tag["href"] if link_tag else url

            lat_tag = block.find("meta", itemprop="latitude")
            lon_tag = block.find("meta", itemprop="longitude")
            lat = float(lat_tag["content"]) if lat_tag else None
            lon = float(lon_tag["content"]) if lon_tag else None

            street_tag = block.find("span", itemprop="streetAddress")
            city_tag = block.find("span", itemprop="addressLocality")
            region_tags = block.find_all("span", itemprop="addressRegion")

            street = street_tag.get_text(strip=True) if street_tag else ""
            city = city_tag.get_text(strip=True) if city_tag else ""
            state = region_tags[0].get_text(strip=True) if region_tags else "FL"
            zip_val = region_tags[1].get_text(strip=True) if len(region_tags) > 1 else zip_code
            full_address = f"{street}, {city}, {state} {zip_val}".strip(", ")

            start_tag = block.find("meta", itemprop="startDate")
            end_tag = block.find("meta", itemprop="endDate")
            date_text = ""
            if start_tag:
                date_text = start_tag.get("content", "")
                if end_tag and end_tag.get("content") != date_text:
                    date_text = date_text + " to " + end_tag.get("content", "")

            desc_tag = block.find("span", class_="eventdesc")
            description = desc_tag.get_text(strip=True)[:400] if desc_tag else ""

            listing = Listing(
                source="YardSaleSearch",
                title=title,
                url=link,
                address=full_address,
                date_text=date_text,
                description=description,
                lat=lat,
                lon=lon,
                city=city,
                state=state,
                zip_code=zip_val,
            )
            listings.append(listing)
            log.info(f"  + {title[:50]} | {full_address}")

        except Exception as e:
            log.error(f"Parse error: {e}")

    return listings


def scrape_all(lat=27.3364, lon=-82.5307, state="FL", cl_subdomains=None, expand_us=False):
    all_listings = []
    seen = set()

    log.info("=== Starting YardSaleSearch scrape - SW Florida ===")
    for zip_code in FL_ZIPS:
        if len(all_listings) >= 200:
            break
        batch = scrape_yardsalesearch_zip(zip_code, radius=25)
        for listing in batch:
            if listing.url not in seen:
                seen.add(listing.url)
                all_listings.append(listing)

    if expand_us:
        log.info("=== Expanding to US market ===")
        for region, zips in US_ZIPS.items():
            log.info(f"Region: {region}")
            for zip_code in zips:
                batch = scrape_yardsalesearch_zip(zip_code, radius=25)
                for listing in batch:
                    if listing.url not in seen:
                        seen.add(listing.url)
                        all_listings.append(listing)
            time.sleep(random.uniform(3, 7))

    log.info(f"=== Done: {len(all_listings)} unique listings ===")
    return all_listings


if __name__ == "__main__":
    results = scrape_all()
    print(f"\nTotal: {len(results)} listings\n")
    for r in results:
        print(f"[{r.source}] {r.title[:50]}")
        print(f"  {r.address} | {r.date_text}")
        print(f"  lat={r.lat} lon={r.lon}")
        print()
