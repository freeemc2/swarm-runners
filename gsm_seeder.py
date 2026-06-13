"""GSM metro pre-seeder — weekly GitHub Actions. Scrapes ~50 US metro zips via
YardSaleSearch, geocodes (Nominatim), POSTs to GSM /api/ingest. Keeps heavy load
off the 2GB EP box. Big-city users get instant loads; long tail fills via on-demand."""
import os, time, requests
from scrapers_final import scrape_yardsalesearch_zip
from geocode import geocode_listings

INGEST_URL = os.environ.get("INGEST_URL", "https://garagesalemap.app/api/ingest")
INGEST_KEY = os.environ.get("INGEST_KEY", "")
METRO_ZIPS = [
 "10001","90001","60601","77001","85001","19101","78201","92101","75201","95101",
 "78701","30301","32801","33101","37201","28201","98101","80201","02101","20001",
 "89101","97201","53201","48201","21201","94601","61601","55401","43201","27601",
 "33601","70112","32099","40201","64101","73101","84101","46201","87101","99501",
 "23218","96813","59601","83701","04101","57101","58501","82001","59101","68101"]

def post(sales):
    r = requests.post(INGEST_URL, headers={"X-Ingest-Key": INGEST_KEY,
        "Content-Type": "application/json"}, json={"sales": sales}, timeout=90)
    try: return r.json()
    except Exception: return {"status": r.status_code}

def main():
    if not INGEST_KEY:
        print("INGEST_KEY missing"); return
    total = 0
    for z in METRO_ZIPS:
        try:
            listings = scrape_yardsalesearch_zip(z, radius=25)
            geocode_listings(listings)
            payload = [{"source": L.source, "title": L.title, "url": L.url, "address": L.address,
                "date_text": L.date_text, "description": L.description, "lat": L.lat, "lon": L.lon,
                "city": L.city, "state": L.state, "zip_code": L.zip_code}
                for L in listings if L.lat and L.lon and L.url]
            if payload:
                res = post(payload); ing = res.get("ingested", 0); total += ing
                print(f"zip {z}: scraped {len(listings)}, ingested {ing}", flush=True)
            else:
                print(f"zip {z}: 0 geocoded", flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"zip {z} error: {e}", flush=True)
    print(f"TOTAL ingested: {total}")

if __name__ == "__main__":
    main()
