#!/usr/bin/env python3
"""Build Home's persistent search metadata cache in a background job.

TMDB discovery supplies canonical movie/TV records first; torrent scrapers are
a secondary source for titles that may not appear in popular discovery lists.
This job never changes or deletes the checked-in franchise caches.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import re
import sys
from difflib import SequenceMatcher
from typing import Any

try:
    import main as home_backend
except ModuleNotFoundError:
    from backend import main as home_backend  # type: ignore[no-redef]


DEFAULT_QUERIES = [
    "2026 movies",
    "2025 movies",
    "2024 movies",
    "2023 movies",
    "Bollywood movies",
    "Hindi movies",
    "Hollywood movies",
    "popular TV series",
    "anime movies",
    "anime series",
]
log = logging.getLogger("home-catalogue-builder")
PACK_OR_NOISE = re.compile(
    r"(?i)\\b(collection|complete collection|movie pack|movies pack|mega pack|pack of movies|all movies|"
    r"filmography|discography|soundtrack|ost|songs|music album|mp3|flac|audio songs|"
    r"full album|jukebox|best of|vol(?:ume)?\\s*\\d|season pack|episodes pack|"
    r"complete season|complete series|all episodes|xxx|porn|adult pack|uncensored|"
    r"erotic|hot scene|web series pack)\\b"
)
EDITORIAL_NOISE = re.compile(
    r"(?i)(?:^\\s*\\d+\\s+(?:best|top|latest|newest)\\b|"
    r"\\b(?:top|best)\\s+\\d{1,2}\\b|"
    r"\\b(?:latest|newest)\\s+(?:bollywood|hollywood|hindi|movies|films)\\b|"
    r"\\b(?:bollywood|hollywood|hindi)\\s+movies?\\s+(?:in\\s+hindi|of\\s+\\d{4}|"
    r"in\\s+best\\s+quality|available|download)\\b|"
    r"\\b(?:hand[ -]?picked|best quality available|prints available|full movie collection|"
    r"movie review|movie trailer|list of movies|movies list|actress|wet compilation)\\b|"
    r"\\b(?:from|of)\\s+(?:the\\s+)?(?:bollywood|hollywood)\\s+movies?\\b|"
    r"\\b(?:webhd|mega\\s+pack|avc|aac)\\b)"
)
GENERIC_SCRAPER_QUERIES = {"movie", "movies", "film", "films", "tv", "series", "show", "shows", "anime"}
RELEASE_TAIL = re.compile(r"(?i)(?:\s*[-|]\s*)?(?:yify|yts|rarbg|tgx|ettv|etrg|psa|mkvhub|world4ufree|mkvcinemas|katmoviehd|torrentgalaxy).*$")
QUALITY_TAGS = re.compile(
    r"(?i)\b(2160p|1080p|720p|480p|4k|uhd|web[- .]?dl|webhd|webrip|bluray|brrip|"
    r"hdrip|x264|x265|hevc|h264|h265|proper|repack|yify|yts|10bit|8bit)\b"
)


def _title_and_year(raw_title: str) -> tuple[str, int]:
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    title = RELEASE_TAIL.sub(" ", title)
    title = re.sub(r"(?i)\b(dual audio|multi audio|hindi dubbed|english dubbed|directors cut|extended cut|uncut|proper|repack|limited|remastered|complete)\b", " ", title)
    title = QUALITY_TAGS.sub(" ", title)
    title = re.sub(r"\[[^\]]*\]|\([^)]*(?:1080p|720p|2160p|web.?dl|bluray|x26[45]|hevc|hdr)[^)]*\)", " ", title, flags=re.I)
    title = re.sub(r"(?i)\b(webrip|webdl|web-dl|bluray|brrip|hdrip|hdtv|dvdrip|aac[ .]?\d?\.\d?|ddp?\d?\.\d?|atmos|dts|proper|repack)\b", " ", title)
    title = re.sub(r"[\[\](){}._]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -")
    match = re.search(r"\b((?:19|20|21)\d{2})\b", title)
    year = int(match.group(1)) if match else 0
    if match:
        title = (title[:match.start()] + " " + title[match.end():]).strip(" -")
    return re.sub(r"\s+", " ", title).strip(), year


def _normalize(value: str) -> str:
    return home_backend._normalize_title(value)


def _is_plausible_scraped_title(title: str) -> bool:
    cleaned = re.sub(r"\s+", " ", title).strip(" -|:")
    if (
        len(cleaned) < 2
        or len(cleaned) > 90
        or PACK_OR_NOISE.search(cleaned)
        or EDITORIAL_NOISE.search(cleaned)
    ):
        return False
    words = cleaned.split()
    if len(words) > 10:
        return False
    if re.search(r"(?i)\b(1080p|720p|2160p|x264|x265|hevc|webrip|bluray|\d+\s*gb|season \d|episode \d|complete pack)\b", cleaned):
        return False
    if len(re.findall(r"(?i)\b(?:19|20|21)\d{2}\b", cleaned)) > 1:
        return False
    return True


def _scraper_query_is_useful(query: str) -> bool:
    """Avoid broad searches that the provider normalizes to a generic keyword."""
    try:
        provider_query = home_backend._media_provider_query(query)
    except Exception:
        provider_query = query
    normalized = re.sub(r"[^a-z0-9]+", " ", str(provider_query or "").casefold()).strip()
    tokens = normalized.split()
    if not tokens or normalized in GENERIC_SCRAPER_QUERIES:
        return False
    if len(tokens) <= 2 and any(re.fullmatch(r"(?:19|20|21)\d{2}", t) for t in tokens) and all(
        t in GENERIC_SCRAPER_QUERIES or re.fullmatch(r"(?:19|20|21)\d{2}", t) for t in tokens
    ):
        return False
    return True


def _release_year(row: dict[str, Any], media_type: str) -> int:
    date = str((row.get("release_date") if media_type == "movie" else row.get("first_air_date")) or "")
    try:
        return int(date[:4]) if len(date) >= 4 else 0
    except ValueError:
        return 0


def _candidate_from_tmdb(row: dict[str, Any], media_type: str, source: str) -> dict[str, Any] | None:
    if not isinstance(row, dict) or not row.get("id"):
        return None
    canonical = str((row.get("title") if media_type == "movie" else row.get("name")) or "").strip()
    if not canonical:
        return None
    try:
        tmdb_id = int(row["id"])
    except (TypeError, ValueError):
        return None
    release_date = str((row.get("release_date") if media_type == "movie" else row.get("first_air_date")) or "")
    poster_path = str(row.get("poster_path") or "")
    backdrop_path = str(row.get("backdrop_path") or "")
    year = _release_year(row, media_type)
    return {
        "id": tmdb_id,
        "tmdbId": tmdb_id,
        "title": canonical,
        "year": year or None,
        "mediaType": media_type,
        "overview": re.sub(r"\s+", " ", str(row.get("overview") or "")).strip(),
        "posterUrl": "https://image.tmdb.org/t/p/w500" + poster_path if poster_path else "",
        "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
        "rating": row.get("vote_average") or 0,
        "tmdbUrl": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
        "source": source,
        "releaseDate": release_date,
    }


def _tmdb_candidate(title: str, year: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    target = _normalize(title)
    choices: list[tuple[tuple[int, int, int, float], dict[str, Any], str]] = []
    for row in payload.get("results") or []:
        if not isinstance(row, dict) or row.get("media_type") not in {"movie", "tv"} or not row.get("id"):
            continue
        media_type = str(row["media_type"])
        canonical = str((row.get("title") if media_type == "movie" else row.get("name")) or "").strip()
        if not canonical:
            continue
        normalized = _normalize(canonical)
        if not normalized:
            continue
        result_year = _release_year(row, media_type)
        exact = int(normalized == target)
        year_match = int(not year or not result_year or result_year == year)
        ratio = SequenceMatcher(None, target, normalized).ratio()
        target_tokens = set(target.split())
        result_tokens = set(normalized.split())
        overlap = len(target_tokens & result_tokens) / max(1, len(target_tokens | result_tokens))
        if not exact and not (ratio >= 0.88 and overlap >= 0.75):
            continue
        if year and result_year and abs(year - result_year) > 1:
            continue
        choices.append(((exact, year_match, int(ratio * 1000), float(row.get("popularity") or 0)), row, media_type))
    if not choices:
        return None
    choices.sort(key=lambda item: item[0], reverse=True)
    _, row, media_type = choices[0]
    return _candidate_from_tmdb(row, media_type, "background scraper catalogue")


def _discovery_plan(query: str) -> list[tuple[str, dict[str, Any], str]]:
    """Translate known catalogue queries into relevant, year-aware TMDB discovery filters."""
    q = query.casefold()
    year_match = re.search(r"\b((?:19|20|21)\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None
    anime = "anime" in q
    hindi = "bollywood" in q or "hindi" in q
    hollywood = "hollywood" in q
    tv = any(token in q for token in ("tv", "series", "anime series", "show"))
    media_types = ["tv"] if tv else (["movie", "tv"] if anime and "movies" not in q else ["movie"])
    plan: list[tuple[str, dict[str, Any], str]] = []
    for media_type in media_types:
        params: dict[str, Any] = {
            "include_adult": "false",
            "language": "en-US",
            "sort_by": "popularity.desc",
            "page": 1,
        }
        if media_type == "movie":
            params["primary_release_date.lte"] = "2027-12-31"
            if year:
                params["primary_release_year"] = year
            if hindi:
                params["with_original_language"] = "hi"
                params["region"] = "IN"
            elif hollywood:
                params["with_original_language"] = "en"
            if anime:
                params["with_genres"] = "16"
            endpoint = "discover/movie"
        else:
            if year:
                params["first_air_date_year"] = year
            if hindi:
                params["with_original_language"] = "hi"
                params["with_origin_country"] = "IN"
            elif hollywood:
                params["with_original_language"] = "en"
            if anime:
                params["with_genres"] = "16"
            endpoint = "discover/tv"
        label = f"TMDB {media_type} discovery for {query}"
        plan.append((endpoint, params, label))
    return plan


async def _discover_query(query: str, per_query: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    plan = _discovery_plan(query)
    for endpoint, base_params, source in plan:
        pages = max(1, min(5, math.ceil(max(1, per_query) / 20)))
        for page in range(1, pages + 1):
            params = dict(base_params)
            params["page"] = page
            try:
                payload = await home_backend._tmdb_get_json(endpoint, params, request_timeout=8.0)
            except Exception as exc:
                log.warning("TMDB discovery failed for %r page %d (%s)", query, page, type(exc).__name__)
                break
            results = payload.get("results") or []
            media_type = "movie" if endpoint.endswith("/movie") else "tv"
            for row in results:
                candidate = _candidate_from_tmdb(row, media_type, source)
                if candidate:
                    rows.append(candidate)
                if len(rows) >= per_query:
                    return rows
            if not results or page >= int(payload.get("total_pages") or page):
                break
    return rows


async def _scrape_query(query: str, per_query: int) -> list[tuple[str, int, str]]:
    if not _scraper_query_is_useful(query):
        log.info("Skipping broad scraper query %r; provider would search a generic term", query)
        return []
    try:
        rows = await home_backend.search_1337x(query, min(50, per_query))
    except Exception as exc:
        log.warning("Scraper query failed for %r (%s)", query, type(exc).__name__)
        return []
    candidates: list[tuple[str, int, str]] = []
    rejected = 0
    for row in rows:
        title, year = _title_and_year(str(row.get("title") or ""))
        if not _is_plausible_scraped_title(title):
            rejected += 1
            log.debug("Rejected non-title/noisy scraper result %r from query %r", title, query)
            continue
        if _normalize(title):
            candidates.append((title, year, query))
    log.info("Scraped query %r: %d rows, %d plausible titles, %d rejected", query, len(rows), len(candidates), rejected)
    return candidates


async def build_catalogue(queries: list[str], max_titles: int, per_query: int) -> dict[str, int]:
    if not (home_backend.TMDB_READ_ACCESS_TOKENS or home_backend.TMDB_API_KEYS):
        raise RuntimeError("Configure TMDB_API_KEYS or TMDB_READ_ACCESS_TOKENS before building the catalogue.")

    cap = max(1, max_titles)
    per_query = max(1, min(100, per_query))
    output_by_id: dict[tuple[str, int], dict[str, Any]] = {}
    discovered: dict[tuple[str, int], tuple[str, int, str]] = {}
    discovery_failures = 0

    # First populate most of the cache from canonical TMDB discovery results.
    # Reserve capacity for scraper-discovered titles so the scraper remains useful
    # for less-popular and newly indexed films/series too.
    discovery_budget = max(1, int(cap * 0.65)) if cap > 1 else 1
    for query in queries:
        if len(output_by_id) >= discovery_budget:
            break
        rows = await _discover_query(query, min(per_query, discovery_budget - len(output_by_id)))
        if not rows:
            discovery_failures += 1
        for candidate in rows:
            key = (str(candidate["mediaType"]), int(candidate["tmdbId"]))
            output_by_id.setdefault(key, candidate)
        log.info("TMDB discovery %r: %d rows, %d unique records so far", query, len(rows), len(output_by_id))
        await asyncio.sleep(0.15)

    # Scrapers remain useful for less-popular and newly indexed titles. Only resolve
    # plausible individual-title candidates, and never let these displace TMDB rows.
    for query in queries:
        if len(discovered) >= cap:
            break
        for title, year, source_query in await _scrape_query(query, per_query):
            key = (_normalize(title), year)
            if key[0]:
                discovered.setdefault(key, (title, year, source_query))
        await asyncio.sleep(0.2)

    remaining = max(0, cap - len(output_by_id))
    selected = list(discovered.values())[:remaining]
    log.info("Resolving %d plausible scraper titles through TMDB", len(selected))
    semaphore = asyncio.Semaphore(3)
    matched = 0
    lookup_failures = 0
    lookup_no_match = 0
    lookup_retries = 0

    async def resolve(item: tuple[str, int, str]) -> None:
        nonlocal matched, lookup_failures, lookup_no_match, lookup_retries
        title, year, source_query = item
        async with semaphore:
            payload = None
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    payload = await home_backend._tmdb_get_json(
                        "search/multi",
                        {"query": title, "include_adult": "false", "language": "en-US", "page": 1},
                        request_timeout=10.0,
                    )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt == 0:
                        lookup_retries += 1
                        await asyncio.sleep(0.5)
            if last_exc is not None or payload is None:
                lookup_failures += 1
                log.warning("TMDB title lookup failed for %r from %r after retry (%s)", title, source_query, type(last_exc).__name__ if last_exc else "empty response")
                return
            candidate = _tmdb_candidate(title, year, payload)
            if candidate:
                key = (str(candidate["mediaType"]), int(candidate["tmdbId"]))
                if key not in output_by_id:
                    output_by_id[key] = candidate
                    matched += 1
            else:
                lookup_no_match += 1
                log.debug("No confident TMDB match for scraper title %r (year=%s, query=%r)", title, year or "unknown", source_query)

    await asyncio.gather(*(resolve(item) for item in selected))

    # If scraping yielded no confident new TMDB IDs, spend unused capacity on
    # canonical TMDB discovery pages instead of returning an artificially small
    # catalogue (e.g. 13 rows when cap=20).
    if len(output_by_id) < cap:
        for query in queries:
            if len(output_by_id) >= cap:
                break
            rows = await _discover_query(query, min(100, max(per_query, cap)))
            added = 0
            for candidate in rows:
                key = (str(candidate["mediaType"]), int(candidate["tmdbId"]))
                if key in output_by_id:
                    continue
                output_by_id[key] = candidate
                added += 1
                if len(output_by_id) >= cap:
                    break
            if added:
                log.info(
                    "TMDB fallback discovery %r: added %d new records, %d total",
                    query,
                    added,
                    len(output_by_id),
                )
            await asyncio.sleep(0.15)

    output = list(output_by_id.values())[:cap]
    persisted = home_backend._persist_search_metadata_cache_rows(output)
    return {
        "queries": len(queries),
        "tmdb_discovery_records": len(output_by_id) - matched,
        "scraped_unique_titles": len(discovered),
        "scraper_titles_attempted": len(selected),
        "scraper_tmdb_matches": matched,
        "tmdb_discovery_queries_without_results": discovery_failures,
        "tmdb_lookup_failures": lookup_failures,
        "scraper_titles_without_confident_tmdb_match": lookup_no_match,
        "tmdb_lookup_retries": lookup_retries,
        "total_unique_tmdb_records": len(output),
        "rows_written_or_updated": persisted,
    }


async def _run(args: argparse.Namespace) -> None:
    queries = list(args.query or [])
    if args.defaults or not queries:
        queries.extend(DEFAULT_QUERIES)
    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    stats = await build_catalogue(queries, args.max_titles, args.per_query)
    print("Catalogue build complete:")
    for key, value in stats.items():
        print(f"  {key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Discovery/scraper query; repeat for multiple queries.")
    parser.add_argument("--defaults", action="store_true", help="Include the built-in discovery queries.")
    parser.add_argument("--max-titles", type=int, default=150, help="Maximum unique TMDB records to persist (default: 150).")
    parser.add_argument("--per-query", type=int, default=30, help="Maximum discovery/scraper results per query (default: 30).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Catalogue build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
