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

def search_ddg(keyword, location):
    from ddgs import DDGS
    q = f'"{keyword}" {location} {INTENT_SITES}'.strip()
    out = []
    with DDGS() as d:
        for r in d.text(q, max_results=15, timelimit="m"):
            url = r.get("href", "")
            if is_junk(url):
                continue
            out.append({"title": r.get("title", ""), "url": url,
                        "description": r.get("body", ""), "location": location})
    return out


def search_google(keyword, location):
    # Light scrape of Google HTML. From GitHub runner IPs, much less blocked than
    # from our VPS IPs — but still rate-limited; cron interval handles pacing.
    from bs4 import BeautifulSoup
    q = f'"{keyword}" {location} {INTENT_SITES}'.strip()
    url = "https://www.google.com/search?" + urlparse.urlencode({"q": q, "num": "20", "hl": "en"})
    resp = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=25)
    if resp.status_code != 200:
        print(f"google HTTP {resp.status_code}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    seen = set()
    # Google wraps real result links as /url?q=<encoded>&...
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
        # require it to be one of our intent sites
        if not any(s in target for s in ["reddit.com", "facebook.com/groups", "nextdoor.com", "craigslist.org"]):
            continue
        title = a.get_text()[:160].strip()
        if not title:
            continue
        out.append({"title": title, "url": target, "description": "", "location": location})
        seen.add(target)
        if len(out) >= 12:
            break
    return out


def fetch_web(url):
    from bs4 import BeautifulSoup
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
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
                else:
                    results = search_ddg(kw, loc)
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
