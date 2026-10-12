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
    r"(?i)\b(collection|complete collection|movie pack|movies pack|all movies|"
    r"filmography|discography|soundtrack|ost|songs|music album|vol(?:ume)?\s*\d|"
    r"season pack|episodes pack|xxx|adult pack)\b"
)
QUALITY_TAGS = re.compile(
    r"(?i)\b(2160p|1080p|720p|480p|4k|uhd|web[- .]?dl|webrip|bluray|brrip|"
    r"hdrip|x264|x265|hevc|h264|h265|proper|repack|yify|yts|10bit|8bit)\b"
)


def _title_and_year(raw_title: str) -> tuple[str, int]:
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    title = QUALITY_TAGS.sub(" ", title)
    title = re.sub(r"\[[^\]]*\]|\([^)]*(?:1080p|720p|2160p|web.?dl|bluray)[^)]*\)", " ", title, flags=re.I)
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
    cleaned = re.sub(r"\s+", " ", title).strip()
    if len(cleaned) < 2 or len(cleaned) > 120 or PACK_OR_NOISE.search(cleaned):
        return False
    words = cleaned.split()
    if len(words) > 14:
        return False
    # Reject release descriptions with several technical/source tokens left over.
    if len(re.findall(r"(?i)\b(1080p|720p|2160p|x264|x265|hevc|webrip|bluray)\b", cleaned)) >= 2:
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
    choices: list[tuple[tuple[int, int, float], dict[str, Any], str]] = []
    for row in payload.get("results") or []:
        if not isinstance(row, dict) or row.get("media_type") not in {"movie", "tv"} or not row.get("id"):
            continue
        media_type = str(row["media_type"])
        canonical = str((row.get("title") if media_type == "movie" else row.get("name)) or "").strip()
        if not canonical:
            continue
        normalized = _normalize(canonical)
        if not normalized:
            continue
        result_year = _release_year(row, media_type)
        exact = int(normalized == target)
        year_match = int(not year or not result_year or result_year == year)
        # Do not accept unrelated popular results; exact or a meaningful prefix match only.
        prefix_match = target.startswith(normalized) or normalized.startswith(target)
        if not exact and not prefix_match:
            continue
        choices.append(((exact, year_match, float(row.get("popularity") or 0)), row, media_type))
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

    # First populate the cache from canonical TMDB discovery results. Unlike torrent
    # titles, these rows already have verified IDs, years, posters, and overviews.
    for query in queries:
        if len(output_by_id) >= cap:
            break
        rows = await _discover_query(query, min(per_query, cap - len(output_by_id)))
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

    async def resolve(item: tuple[str, int, str]) -> None:
        nonlocal matched, lookup_failures
        title, year, source_query = item
        async with semaphore:
            try:
                payload = await home_backend._tmdb_get_json(
                    "search/multi",
                    {"query": title, "include_adult": "false", "language": "en-US", "page": 1},
                    request_timeout=8.0,
                )
                candidate = _tmdb_candidate(title, year, payload)
                if candidate:
                    key = (str(candidate["mediaType"]), int(candidate["tmdbId"]))
                    if key not in output_by_id:
                        output_by_id[key] = candidate
                        matched += 1
            except Exception as exc:
                lookup_failures += 1
                log.warning("TMDB title lookup failed for %r from %r (%s)", title, source_query, type(exc).__name__)

    await asyncio.gather(*(resolve(item) for item in selected))
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
