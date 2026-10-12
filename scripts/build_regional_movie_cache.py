#!/usr/bin/env python3
"""Build a persistent shared cache for Home's six Bollywood/Hollywood sections.

Run inside the VPS host with the Home API reachable at 127.0.0.1:10000.
Resumable checkpoints are written after each title; the last good cache is kept
if a provider fails. Intended for a weekly cron job.
"""
import json, os, re, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.getenv("HOME_DATA_DIR", "/home/ubuntu/home-data"))
API = os.getenv("HOME_API_BASE", "http://127.0.0.1:10000").rstrip("/")
CACHE = BASE / "regional_movie_prebuilt_cache.json"
STATE = BASE / "regional_movie_prebuilt_state.json"
CATALOGUES = (
    "popular-bollywood", "trending-bollywood", "latest-bollywood",
    "popular-hollywood", "trending-hollywood", "latest-hollywood",
)
PAGE_LIMIT = int(os.getenv("REGIONAL_CACHE_PAGES", "3"))
TORRENT_LIMIT = 50
MAX_SIZE = 5 * 1024**3
BAD_QUALITY = re.compile(r"(?:camrip|hdcam|telesync|telecine|dvdscr|screener|workprint)", re.I)

def request(path, timeout=90):
    req = urllib.request.Request(API + path, headers={"Accept":"application/json","User-Agent":"HomeRegionalCacheBuilder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)

def norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").casefold())

def main():
    old = {}
    if CACHE.exists():
        try: old = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception: old = {}
    state = {}
    if STATE.exists():
        try: state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception: state = {}
    cache = {"version":1,"builtAt":old.get("builtAt"),"catalogues":old.get("catalogues",{}),"movies":old.get("movies",{}),"status":"building"}
    for cat in CATALOGUES:
        prior = cache["catalogues"].get(cat,{})
        if prior.get("builtAt") and not os.getenv("FORCE_REGIONAL_CACHE"):
            # Reuse page snapshots for this run; refreshed weekly via cron.
            continue
        pages = []
        for page in range(1, PAGE_LIMIT + 1):
            try:
                data = request("/api/movies/catalogue/"+cat+"?page="+str(page)+"&released_only=true")
                pages.append({"page":page,"results":data.get("results",[]),"totalPages":data.get("totalPages",1)})
                if page >= int(data.get("totalPages") or 1) or not data.get("results"): break
            except Exception as e:
                print(json.dumps({"event":"catalogue_error","catalogue":cat,"page":page,"error":str(e)}),flush=True)
                if not pages and not prior.get("pages"): raise
                pages = prior.get("pages", pages); break
        cache["catalogues"][cat] = {"builtAt":datetime.now(timezone.utc).isoformat(),"pages":pages}
        atomic_json(CACHE,cache)
        print(json.dumps({"event":"catalogue_cached","catalogue":cat,"movies":sum(len(x["results"]) for x in pages)}),flush=True)
    movies = {}
    for cat, snapshot in cache["catalogues"].items():
        for page in snapshot.get("pages",[]):
            for movie in page.get("results",[]):
                key = norm(movie.get("title"))+"|"+str(movie.get("year") or "")
                if key: movies.setdefault(key,{"movie":movie,"catalogues":[]})["catalogues"].append(cat)
    cache["movies"] = movies
    total = len(movies)
    for idx,(key,entry) in enumerate(movies.items(),1):
        if key in state.get("completed",{}) and not os.getenv("FORCE_REGIONAL_CACHE"):
            entry["torrentDetails"] = state["completed"][key]
            continue
        title = str(entry["movie"].get("title") or "").strip()
        year = entry["movie"].get("year")
        if not title: continue
        query = title + (" "+str(year) if year else "")
        try:
            path = "/api/search?q="+urllib.parse.quote(query)+"&limit="+str(TORRENT_LIMIT)
            response = request(path, timeout=150)
            rows = response if isinstance(response,list) else response.get("results", response.get("torrents",[]))
            target = norm(title)
            good = []
            for row in rows or []:
                name = str(row.get("title") or "")
                media = str(row.get("mediaTitle") or "")
                size = int(row.get("size") or 0)
                has_source = bool(row.get("magnetUrl") or row.get("downloadUrl") or row.get("sourceUrl"))
                if not has_source or size > MAX_SIZE or BAD_QUALITY.search(name): continue
                if target and (target in norm(name) or target in norm(media)): good.append(row)
            entry["torrentDetails"] = {"query":query,"results":good,"scannedAt":datetime.now(timezone.utc).isoformat(),"status":"ready" if good else "empty"}
            state.setdefault("completed",{})[key] = entry["torrentDetails"]
        except Exception as e:
            print(json.dumps({"event":"torrent_error","title":title,"error":str(e)}),flush=True)
            entry["torrentDetails"] = old.get("movies",{}).get(key,{}).get("torrentDetails",{"query":query,"results":[],"status":"unavailable"})
        if idx % 5 == 0 or idx == total:
            cache["movies"] = movies
            cache["builtAt"] = datetime.now(timezone.utc).isoformat()
            cache["status"] = "building"
            atomic_json(CACHE,cache); atomic_json(STATE,state)
            print(json.dumps({"event":"progress","completed":idx,"total":total}),flush=True)
        time.sleep(float(os.getenv("REGIONAL_CACHE_DELAY_SECONDS","0.4")))
    cache["movies"] = movies
    cache["builtAt"] = datetime.now(timezone.utc).isoformat()
    cache["status"] = "ready"
    cache["movieCount"] = len(movies)
    cache["torrentMovieCount"] = sum(bool((x.get("torrentDetails") or {}).get("results")) for x in movies.values())
    atomic_json(CACHE,cache)
    print(json.dumps({"event":"complete","movieCount":cache["movieCount"],"torrentMovieCount":cache["torrentMovieCount"],"file":str(CACHE)}),flush=True)

if __name__ == "__main__": main()
