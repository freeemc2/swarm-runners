"""
swarm_runner.py — GitHub Actions worker that joins the v2 swarm.

Runs inside a GitHub Actions workflow on a scheduled cron. Each invocation:
  1. registers with the master Flask
  2. claims up to MAX_JOBS jobs for its (provider, stage, source)
  3. executes them (ddg search, google scrape, or web fetch)
  4. submits results with lease_token

Env (all set by the workflow file):
  MASTER_URL    https://elevatehomeprogram.com
  AGENT_KEY     repo secret
  SOURCE        ddg | google | web
  STAGE         search | fetch
  MAX_JOBS      cap per run (default 3) — keep low so cron stays under 5 min
"""
import os
import re
import sys
import time
import random
import urllib.parse as urlparse
import requests

MASTER = os.environ["MASTER_URL"].rstrip("/")
KEY = os.environ["AGENT_KEY"]
SOURCE = os.environ.get("SOURCE", "ddg")
STAGE = os.environ.get("STAGE", "search")
MAX_JOBS = int(os.environ.get("MAX_JOBS", "3"))
# Each workflow has its own provider so a DDG runner can't grab a Google job.
PROVIDER = os.environ.get("PROVIDER", f"gha-{SOURCE}")
# Lane-2 residential egress (aria 2026-07-31): when SWARM_PROXY is set
# (e.g. socks5h://127.0.0.1:1055 = tailscale-scraper -> residential exit),
# every outbound search request egresses from Brian's home IP instead of
# a datacenter IP. Google/Bing do not suppress residential.
SWARM_PROXY = os.environ.get("SWARM_PROXY", "").strip()
PROXIES = {"http": SWARM_PROXY, "https": SWARM_PROXY} if SWARM_PROXY else None
RUN_ID = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))[-6:]
NAME = f"{PROVIDER}-{RUN_ID}"

H = {"X-Agent-Key": KEY, "Content-Type": "application/json"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

# Junk: business directories + job boards — never customer intent.
JUNK = ['yelp.com', 'houzz.com', 'homeadvisor.com', 'angies', 'angi.com',
        'indeed.com', 'ziprecruiter.com', 'monster.com', 'glassdoor.com',
        'bbb.org', 'thumbtack.com', 'porch.com', 'manta.com', 'yellowpages.com',
        'mapquest.com', 'foursquare.com', 'linkedin.com/jobs',
        'careerbuilder.com', 'snagajob.com']
INTENT_SITES_LIST = ["reddit.com", "nextdoor.com", "facebook.com/groups", "craigslist.org"]
INTENT_SITES = "(site:reddit.com OR site:nextdoor.com OR site:facebook.com/groups OR site:craigslist.org)"


def is_junk(url):
    u = (url or "").lower()
    return any(j in u for j in JUNK)


def register():
    try:
        r = requests.post(f"{MASTER}/api/v2/agents/register", headers=H, timeout=20,
                          json={"name": NAME, "agent_type": "github-actions",
                                "providers": [PROVIDER], "stages": [STAGE],
                                "capabilities": {"source": SOURCE}})
        print(f"register {NAME}: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"register failed: {e}")
        return False


def claim():
    try:
        r = requests.post(f"{MASTER}/api/v2/jobs/claim", headers=H, timeout=20,
                          json={"agent": NAME, "providers": [PROVIDER], "stages": [STAGE]})
        if r.status_code == 200:
            return r.json().get("job")
    except Exception as e:
        print(f"claim failed: {e}")
    return None


def submit(job, results, provider_error=False):
    try:
        r = requests.post(f"{MASTER}/api/v2/jobs/{job['job_id']}/submit", headers=H, timeout=45,
                          json={"agent": NAME, "lease_token": job["lease_token"],
                                "results": results, "provider_error": provider_error})
        print(f"submit job {job['job_id']} ({STAGE}/{SOURCE}): {r.status_code} {r.text[:120]}")
        return r.status_code == 200
    except Exception as e:
        print(f"submit failed: {e}")
        return False


# ----- executors -----

def is_intent_site(url):
    u = (url or "").lower()
    return any(s in u for s in INTENT_SITES_LIST)


def search_ddg(keyword, location):
    """Per-site fanout: DDG ignores the OR'd site: operator (confirmed 2026-07-08).
    Issue one query per intent site, merge+dedup by URL. Post-filter ensures only
    intent-site results survive even if DDG returns commercial junk."""
    from ddgs import DDGS
    out = []
    seen = set()
    for site in INTENT_SITES_LIST:
        q = f'"{keyword}" {location} site:{site}'.strip()
        try:
            with DDGS(proxy=SWARM_PROXY or None) as d:
                for r in d.text(q, max_results=8, timelimit="m"):
                    url = r.get("href", "")
                    if url in seen or is_junk(url):
                        continue
                    if not is_intent_site(url):
                        continue
                    seen.add(url)
                    out.append({"title": r.get("title", ""), "url": url,
                                "description": r.get("body", ""), "location": location})
        except Exception as e:
            print(f"ddg site:{site} error: {e}")
        time.sleep(random.uniform(1, 3))
    return out


def search_google(keyword, location):
    """Google HTML scrape with full Chrome-130 header fingerprint (JobSpy pattern).
    Two-header requests get flagged instantly by Google's sec-ch-ua-* checks.
    A full Chrome-shaped request + persistent Session + referer='https://www.google.com/'
    passes as a real Chrome instance from GH IPs. Aria 2026-07-08."""
    from bs4 import BeautifulSoup

    HEADERS = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-US,en;q=0.9",
        "priority": "u=0, i",
        "referer": "https://www.google.com/",
        "sec-ch-prefers-color-scheme": "dark",
        "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-arch": '"arm"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-form-factors": '"Desktop"',
        "sec-ch-ua-full-version": '"130.0.6723.58"',
        "sec-ch-ua-full-version-list": '"Chromium";v="130.0.6723.58", "Google Chrome";v="130.0.6723.58", "Not?A_Brand";v="99.0.0.0"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-platform-version": '"15.0.1"',
        "sec-ch-ua-wow64": "?0",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "x-browser-channel": "stable",
        "x-browser-copyright": "Copyright 2024 Google LLC. All rights reserved.",
        "x-browser-year": "2024",
    }
    q = f'"{keyword}" {location} {INTENT_SITES}'.strip()
    url = "https://www.google.com/search?" + urlparse.urlencode({"q": q, "num": "20", "hl": "en"})
    sess = requests.Session()
    if PROXIES: sess.proxies.update(PROXIES)
    # Prime session with a google.com hit so we have real cookies before searching.
    try:
        sess.get("https://www.google.com/", headers=HEADERS, timeout=15)
    except Exception:
        pass
    resp = sess.get(url, headers=HEADERS, timeout=25)
    if resp.status_code != 200:
        print(f"google HTTP {resp.status_code}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.match(r"^/url\?q=([^&]+)", href)
        if not m:
            if href.startswith("http") and "google.com" not in href:
                target = href
            else:
                continue
        else:
            target = urlparse.unquote(m.group(1))
        if target in seen or is_junk(target):
            continue
        if not is_intent_site(target):
            continue
        title = a.get_text()[:160].strip()
        if not title:
            continue
        out.append({"title": title, "url": target, "description": "", "location": location})
        seen.add(target)
        if len(out) >= 12:
            break
    return out



# --------------------------------------------------------------------------- #
# Lane-1 engines (aria 2026-07-30): widen intake 2 -> 6 search providers.
# Each is its own PROVIDER so the governor scores them independently and they
# compete for job budget via yield-weighting + provider_health breaker.
#   bing      - Microsoft index (independent of Google)
#   brave     - Brave's own index, privacy-first, tolerant of scraping
#   startpage - proxies GOOGLE results from a non-Google endpoint
#   mojeek    - fully independent crawler, genuinely different coverage
# --------------------------------------------------------------------------- #

ENGINE_CFG = {
    "bing": {
        "url": "https://www.bing.com/search?q={q}&count=20",
        "result_sel": "li.b_algo",
        "link_sel": "h2 a",
        "snip_sel": ".b_caption p, .b_algoSlug",
    },
    "brave": {
        "url": "https://search.brave.com/search?q={q}",
        "result_sel": "div.snippet",
        "link_sel": "a",
        "snip_sel": ".snippet-description, .snippet-content",
    },
    "startpage": {
        "url": "https://www.startpage.com/sp/search?query={q}",
        "result_sel": "div.w-gl__result, div.result",
        "link_sel": "a.w-gl__result-title, a.result-link, h3 a",
        "snip_sel": "p.w-gl__description, .description",
    },
    "mojeek": {
        "url": "https://www.mojeek.com/search?q={q}",
        "result_sel": "li.r, ul.results-standard li",
        "link_sel": "a.title, h2 a",
        "snip_sel": "p.s, .s",
    },
}

BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
}


def search_html_engine(engine, keyword, location):
    """Generic HTML-scrape search across the Lane-1 engines. Per-site fanout
    (same pattern as DDG) so we only keep user-generated intent sites."""
    from bs4 import BeautifulSoup
    import urllib.parse as urlparse

    cfg = ENGINE_CFG[engine]
    out, seen = [], set()
    sess = requests.Session()
    if PROXIES: sess.proxies.update(PROXIES)
    sess.headers.update(BROWSER_HEADERS)

    for site in INTENT_SITES_LIST:
        q = f'"{keyword}" {location} site:{site}'.strip()
        url = cfg["url"].format(q=urlparse.quote_plus(q))
        try:
            resp = sess.get(url, timeout=20)
            if resp.status_code != 200:
                print(f"{engine} HTTP {resp.status_code} for site:{site}")
                time.sleep(random.uniform(2, 5))
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for res in soup.select(cfg["result_sel"])[:10]:
                a = res.select_one(cfg["link_sel"])
                if not a:
                    continue
                href = a.get("href", "")
                if href.startswith("/"):          # relative -> skip redirect wrappers
                    continue
                if not href.startswith("http") or href in seen:
                    continue
                if is_junk(href) or not is_intent_site(href):
                    continue
                seen.add(href)
                sn = res.select_one(cfg["snip_sel"])
                out.append({
                    "title": a.get_text(strip=True)[:300],
                    "url": href,
                    "description": (sn.get_text(" ", strip=True)[:1000] if sn else ""),
                    "location": location,
                })
        except Exception as e:
            print(f"{engine} site:{site} error: {e}")
        time.sleep(random.uniform(1.5, 4))        # human pacing per engine
    return out

def fetch_web(url):
    from bs4 import BeautifulSoup
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True, proxies=PROXIES)
    if resp.status_code != 200:
        print(f"fetch HTTP {resp.status_code} for {url}")
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    title = (soup.title.string if soup.title else "") or ""
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n").strip())[:4000]
    if not text:
        return None
    return {"title": title.strip(), "post_text": text, "description": text[:300]}


# ----- main loop -----

def main():
    if not register():
        sys.exit(0)
    processed = errors = 0
    for _ in range(MAX_JOBS):
        job = claim()
        if not job:
            print("no job available, exiting")
            break
        kw = (job.get("keyword") or "")
        loc = (job.get("location") or "")
        url = (job.get("url") or "")
        print(f"-> job {job['job_id']} {STAGE}/{job.get('provider','?')} src={job.get('source','?')} "
              f"kw={kw[:40]!r} loc={loc!r} url={url[:60]!r}")
        results, err = [], False
        try:
            if STAGE == "search":
                if SOURCE == "google":
                    results = search_google(kw, loc)
                elif SOURCE in ENGINE_CFG:
                    results = search_html_engine(SOURCE, kw, loc)
                elif SOURCE == "ddg":
                    results = search_ddg(kw, loc)
                else:
                    # HARD FAIL (aria 2026-07-31, Brian's catch): never silently
                    # fall back to DDG. An unrecognized SOURCE used to collapse
                    # into DuckDuckGo invisibly -- that is exactly how the whole
                    # swarm quietly became "DDG only" and cost a week. Fail loud.
                    raise RuntimeError(
                        f"unknown SOURCE={SOURCE!r} for provider={PROVIDER!r}; "
                        f"known: google, ddg, {sorted(ENGINE_CFG)}. "
                        "Refusing to silently fall back to DDG.")
            elif STAGE == "fetch":
                f = fetch_web(url)
                if f:
                    results = [f]
                else:
                    err = True
        except Exception as e:
            print(f"executor error: {e}")
            err = True
        submit(job, results, provider_error=err)
        if err:
            errors += 1
        processed += 1
        # human-ish pacing between jobs so GH IPs don't burst
        time.sleep(random.randint(3, 7))
    print(f"DONE processed={processed} errors={errors}")


if __name__ == "__main__":
    main()
