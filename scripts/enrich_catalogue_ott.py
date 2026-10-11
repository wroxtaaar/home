#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import shutil
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

BASE = Path("/home/ubuntu/home-data")
ENV_FILE = Path("/home/ubuntu/home/.env.debug")
API_BASE = "http://127.0.0.1:10000"
REGION = "IN"

CATALOGUES = [
    "marvel_catalogue.json",
    "marvel_hindi_catalogue.json",
    "dc_live_action_catalogue.json",
    "dc_live_action_hindi_catalogue.json",
    "dc_animated_catalogue.json",
]

STATE_FILE = BASE / "ott_enrichment_state.json"
LOG_FILE = BASE / "catalogue_ott_enrichment.jsonl"
SUMMARY_FILE = BASE / "catalogue_ott_enrichment_summary.json"

def now():
    return datetime.now(timezone.utc).isoformat()

def log(event):
    event = {"time": now(), **event}
    line = json.dumps(event, ensure_ascii=False)
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

def read_env(path):
    values = {}
    if not path.exists():
        raise RuntimeError(f"Environment file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "resolved": {}, "completed": {}}

def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)

def request_json(url, token=None, timeout=25):
    headers = {"Accept": "application/json", "User-Agent": "HomeCatalogueOTT/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def norm(value):
    value = str(value or "").lower()
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"\b(1080p|2160p|720p|480p|4k|uhd|hdrip|webrip|web-dl|bluray|brrip|bdrip|hdtv|x264|x265|h264|h265|hevc|aac|dts|proper|repack|extended|remastered|dual audio|hindi|english|multi audio|10bit|8bit)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split()).strip()

def clean_title(row):
    # Prefer catalogue metadata over the noisy torrent release name.
    for key in ("mediaTitle", "marvelCatalogueTitle", "catalogueTitle"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    title = str(row.get("title") or "").strip()
    # Remove common trailing release year / torrent details.
    title = re.sub(r"\s*[\[(]\s*(19|20)\d{2}\s*[\])]", " ", title)
    return re.sub(r"\s+", " ", title).strip()

def get_year(row, title):
    year = row.get("year")
    try:
        year = int(year)
        if 1880 <= year <= 2100:
            return year
    except (ValueError, TypeError):
        pass
    match = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    return int(match.group(1)) if match else None

def resolve_movie(title, year, tokens):
    if not title:
        return None, "empty_title"

    params = {"query": title, "include_adult": "false"}
    if year:
        params["year"] = str(year)
    url = "https://api.themoviedb.org/3/search/movie?" + urllib.parse.urlencode(params)
    errors = []

    for token in tokens:
        try:
            result = request_json(url, token=token)
            candidates = result.get("results") or []
            wanted = norm(title)
            exact = []
            for candidate in candidates:
                candidate_title = candidate.get("title") or candidate.get("original_title") or ""
                candidate_year = (candidate.get("release_date") or "")[:4]
                if norm(candidate_title) == wanted:
                    if year and candidate_year and candidate_year.isdigit() and int(candidate_year) != year:
                        continue
                    exact.append(candidate)
            if exact:
                # Prefer matching release year when available.
                exact.sort(key=lambda c: (
                    0 if year and (c.get("release_date") or "")[:4] == str(year) else 1,
                    -(float(c.get("popularity") or 0))
                ))
                return int(exact[0]["id"]), "exact_title_match"
            return None, "no_exact_tmdb_match"
        except urllib.error.HTTPError as e:
            errors.append(f"HTTP_{e.code}")
            if e.code not in (401, 403, 429):
                break
        except Exception as e:
            errors.append(type(e).__name__)

    return None, "tmdb_search_failed:" + ",".join(errors or ["unknown"])

def main():
    if not BASE.exists():
        raise RuntimeError(f"Data directory not found: {BASE}")

    env = read_env(ENV_FILE)
    raw_tokens = env.get("TMDB_READ_ACCESS_TOKENS", "")
    # Accept comma, semicolon, or whitespace-separated token lists.
    tokens = [t.strip() for t in re.split(r"[,;\s]+", raw_tokens) if t.strip()]
    if not tokens:
        raise RuntimeError("TMDB_READ_ACCESS_TOKENS is empty in .env.debug")

    state = load_state()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    catalogues = []

    # Validate every input and back up all catalogues before changing any.
    for name in CATALOGUES:
        path = BASE / name
        if not path.exists():
            log({"status": "FILE_MISSING", "file": name})
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            log({"status": "INVALID_CATALOGUE_SHAPE", "file": name})
            continue
        backup = path.with_name(path.name + ".ott-backup-" + stamp)
        shutil.copy2(path, backup)
        catalogues.append((name, path, data))
        log({"status": "BACKUP_CREATED", "file": name, "backup": str(backup),
             "entries": len(data["results"])})

    if not catalogues:
        raise RuntimeError("No valid catalogues found; nothing changed.")

    counts = {}
    total = sum(len(data["results"]) for _, _, data in catalogues)
    processed = 0

    for filename, path, data in catalogues:
        changed = False
        for row in data["results"]:
            processed += 1
            if not isinstance(row, dict):
                continue

            title = clean_title(row)
            year = get_year(row, title)
            identity = norm(title) + "|" + str(year or "")
            existing = row.get("ottAvailability")

            # Keep successful enrichment. Retry only entries without a successful result.
            if isinstance(existing, dict) and existing.get("status") in (
                "streaming", "rent_buy", "no_providers"
            ):
                counts["ALREADY_ENRICHED"] = counts.get("ALREADY_ENRICHED", 0) + 1
                continue

            saved = state.get("completed", {}).get(identity)
            if isinstance(saved, dict) and saved.get("status") in (
                "streaming", "rent_buy", "no_providers"
            ):
                row["ottAvailability"] = saved["availability"]
                row["tmdbId"] = saved["tmdbId"]
                changed = True
                counts["RESTORED_FROM_PROGRESS"] = counts.get("RESTORED_FROM_PROGRESS", 0) + 1
                continue

            tmdb_id = state.get("resolved", {}).get(identity)
            if not tmdb_id:
                tmdb_id, resolution = resolve_movie(title, year, tokens)
                if tmdb_id:
                    state.setdefault("resolved", {})[identity] = tmdb_id
                    save_state(state)
                else:
                    counts[resolution] = counts.get(resolution, 0) + 1
                    log({"status": "RESOLUTION_FAILED", "file": filename,
                         "title": title, "year": year, "reason": resolution})
                    continue

            try:
                payload = request_json(
                    f"{API_BASE}/api/movies/ott/{tmdb_id}?region={REGION}",
                    timeout=30,
                )
                if not isinstance(payload, dict) or payload.get("movieId") is None:
                    raise ValueError("Unexpected OTT API response")

                streams = payload.get("streamingProviders") or []
                rents = payload.get("rentProviders") or []
                buys = payload.get("buyProviders") or []

                if streams:
                    status = "streaming"
                    log_status = "FETCHED"
                elif rents or buys:
                    status = "rent_buy"
                    log_status = "FETCHED"
                else:
                    status = "no_providers"
                    log_status = "NO_PROVIDERS"

                availability = {
                    "region": REGION,
                    "status": status,
                    "streamingProviders": streams,
                    "rentProviders": rents,
                    "buyProviders": buys,
                    "providerLink": payload.get("providerLink"),
                    "source": payload.get("source"),
                    "checkedAt": payload.get("checkedAt"),
                    "providerAttribution": payload.get("providerAttribution"),
                    "attribution": payload.get("attribution"),
                }

                row["tmdbId"] = int(tmdb_id)
                row["ottAvailability"] = availability
                changed = True
                state.setdefault("completed", {})[identity] = {
                    "tmdbId": int(tmdb_id),
                    "availability": availability,
                }
                save_state(state)

                counts[log_status] = counts.get(log_status, 0) + 1
                log({"status": log_status, "file": filename, "title": title,
                     "year": year, "tmdbId": tmdb_id, "streaming": [p.get("name") for p in streams],
                     "rent": [p.get("name") for p in rents],
                     "buy": [p.get("name") for p in buys], "source": payload.get("source")})
            except Exception as e:
                counts["API_FAILED"] = counts.get("API_FAILED", 0) + 1
                log({"status": "API_FAILED", "file": filename, "title": title,
                     "year": year, "tmdbId": tmdb_id,
                     "error": f"{type(e).__name__}: {str(e)[:250]}"})
                # Do not overwrite an existing good value on failure.

            if processed % 25 == 0:
                log({"status": "PROGRESS", "processed_entries": processed,
                     "total_entries": total, "counts": counts})
            time.sleep(0.15)

        if changed:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
            log({"status": "CATALOGUE_SAVED", "file": filename,
                 "entries": len(data["results"])})

    summary = {
        "finishedAt": now(),
        "region": REGION,
        "counts": counts,
        "catalogues": {name: len(data["results"]) for name, _, data in catalogues},
        "logFile": str(LOG_FILE),
        "stateFile": str(STATE_FILE),
    }
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    log({"status": "FINISHED", **summary})
    print("\nSummary:", json.dumps(summary, indent=2), flush=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user. Progress is saved; rerun the same command to resume.")
        sys.exit(130)
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
