#!/usr/bin/env python3
"""Build the persistent Home search catalogue from scraper-discovered titles.

Run from the repository root:
  TMDB_API_KEYS=... python backend/catalogue_builder.py --query "2025 movies" --query "Bollywood movies"
  python backend/catalogue_builder.py --defaults --max-titles 100

This is deliberately an offline/background job: it does not run in the user-facing
search request and it never changes or deletes the checked-in franchise caches.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from typing import Any

# main.py owns the existing scraper, TMDB key rotation, normalizers, and the
# durable metadata writer. Reuse those implementations instead of maintaining
# a second scraper or a second cache format.
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


def _title_and_year(raw_title: str) -> tuple[str, int]:
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    # Remove common release/source tags without stripping meaningful title words.
    title = re.sub(
        r"(?i)\b(1080p|2160p|720p|480p|4k|uhd|web[- .]?dl|webrip|bluray|brrip|hdrip|x264|x265|hevc|h264|h265|proper|repack|yify|yts)\b",
        " ",
        title,
    )
    title = re.sub(r"[\[\](){}._]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -")
    match = re.search(r"\b((?:19|20|21)\d{2})\b", title)
    year = int(match.group(1)) if match else 0
    if match:
        title = (title[:match.start()] + " " + title[match.end():]).strip(" -")
    title = re.sub(r"\s+", " ", title).strip()
    return title, year


def _normalize(value: str) -> str:
    return home_backend._normalize_title(value)


def _tmdb_candidate(title: str, year: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    results = payload.get("results") or []
    choices: list[tuple[tuple[int, int, float], dict[str, Any], str, str]] = []
    target = _normalize(title)
    for row in results:
        if not isinstance(row, dict) or row.get("media_type") not in {"movie", "tv"} or not row.get("id"):
            continue
        media_type = str(row["media_type"])
        canonical = str((row.get("title") if media_type == "movie" else row.get("name")) or "").strip()
        if not canonical:
            canonical = str(row.get("name") or row.get("title") or "").strip()
        if not canonical:
            continue
        release_date = str((row.get("release_date") if media_type == "movie" else row.get("first_air_date")) or "")
        try:
            result_year = int(release_date[:4]) if len(release_date) >= 4 else 0
        except ValueError:
            result_year = 0
        normalized = _normalize(canonical)
        exact = int(normalized == target)
        year_match = int(not year or not result_year or result_year == year)
        popularity = float(row.get("popularity") or 0)
        # Exact title is the strongest signal; prefer a matching year next.
        choices.append(((exact, year_match, popularity), row, canonical, media_type))
    if not choices:
        return None
    choices.sort(key=lambda item: item[0], reverse=True)
    score, row, canonical, media_type = choices[0]
    # Avoid storing a different title just because it was popular in TMDB.
    if not score[0] and not target.startswith(_normalize(canonical)) and not _normalize(canonical).startswith(target):
        return None
    release_date = str(row.get("release_date") if media_type == "movie" else row.get("first_air_date") or "")
    try:
        result_year = int(release_date[:4]) if len(release_date) >= 4 else year
    except ValueError:
        result_year = year
    tmdb_id = int(row["id"])
    poster_path = str(row.get("poster_path") or "")
    backdrop_path = str(row.get("backdrop_path") or "")
    return {
        "id": tmdb_id,
        "tmdbId": tmdb_id,
        "title": canonical,
        "year": result_year or year or None,
        "mediaType": media_type,
        "overview": re.sub(r"\s+", " ", str(row.get("overview") or "")).strip(),
        "posterUrl": ("https://image.tmdb.org/t/p/w500" + poster_path) if poster_path else "",
        "backdropUrl": ("https://image.tmdb.org/t/p/w780" + backdrop_path) if backdrop_path else "",
        "rating": row.get("vote_average") or 0,
        "tmdbUrl": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
        "source": "background scraper catalogue",
        "releaseDate": release_date,
    }


async def build_catalogue(queries: list[str], max_titles: int, per_query: int) -> dict[str, int]:
    if not (home_backend.TMDB_READ_ACCESS_TOKENS or home_backend.TMDB_API_KEYS):
        raise RuntimeError("Configure TMDB_API_KEYS or TMDB_READ_ACCESS_TOKENS before building the catalogue.")

    discovered: dict[tuple[str, int], str] = {}
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            continue
        try:
            rows = await home_backend.search_1337x(query, min(50, per_query))
        except Exception as exc:
            log.warning("Scraper query failed for %r (%s)", query, type(exc).__name__)
            continue
        for row in rows:
            title, year = _title_and_year(str(row.get("title") or ""))
            if len(title) < 2:
                continue
            key = (_normalize(title), year)
            if key[0]:
                discovered.setdefault(key, title)
        log.info("Scraped query %r: %d rows, %d unique titles so far", query, len(rows), len(discovered))
        await asyncio.sleep(0.25)  # be polite to indexers and avoid burst traffic

    selected = list(discovered.items())[:max(1, max_titles)]
    log.info("Resolving %d discovered titles through TMDB", len(selected))
    semaphore = asyncio.Semaphore(3)
    output: list[dict[str, Any]] = []
    resolved = 0

    async def resolve(item: tuple[tuple[str, int], str]) -> None:
        nonlocal resolved
        (normalized, year), title = item
        async with semaphore:
            try:
                payload = await home_backend._tmdb_get_json(
                    "search/multi",
                    {"query": title, "include_adult": "false", "language": "en-US", "page": 1},
                    request_timeout=5.0,
                )
                candidate = _tmdb_candidate(title, year, payload)
                if candidate:
                    output.append(candidate)
                    resolved += 1
            except Exception as exc:
                log.debug("TMDB lookup failed for %r (%s)", title, type(exc).__name__)

    await asyncio.gather(*(resolve(item) for item in selected))
    persisted = home_backend._persist_search_metadata_cache_rows(output)
    return {
        "queries": len(queries),
        "scraped_unique_titles": len(discovered),
        "titles_attempted": len(selected),
        "tmdb_matches": resolved,
        "rows_written_or_updated": persisted,
    }


async def _run(args: argparse.Namespace) -> None:
    queries = list(args.query or [])
    if args.defaults or not queries:
        queries.extend(DEFAULT_QUERIES)
    # Preserve order while removing duplicate queries.
    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    stats = await build_catalogue(queries, args.max_titles, args.per_query)
    print("Catalogue build complete:")
    for key, value in stats.items():
        print(f"  {key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Scraper search query; repeat for multiple queries.")
    parser.add_argument("--defaults", action="store_true", help="Also include the built-in broad discovery queries.")
    parser.add_argument("--max-titles", type=int, default=150, help="Maximum unique scraped titles to resolve (default: 150).")
    parser.add_argument("--per-query", type=int, default=30, help="Maximum scraper results to inspect per query (default: 30).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Catalogue build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
