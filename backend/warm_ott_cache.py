#!/usr/bin/env python3
"""Manually warm the persistent TMDB/Watchmode OTT availability cache.

Run inside the running Home container, for example:
  docker exec -it home python /app/warm_ott_cache.py
No scheduler is installed; this script runs only when explicitly invoked.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone

import main


CATALOGUES = (
    "marvel",
    "dc-live-action",
    "dc-animated",
    "popular-hollywood",
    "trending-hollywood",
    "popular-bollywood",
    "trending-bollywood",
    "latest-hollywood",
    "latest-bollywood",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manually fill Home's persistent OTT availability cache."
    )
    parser.add_argument("--region", default="IN", help="Two-letter region code (default: IN).")
    parser.add_argument(
        "--pages-per-catalogue",
        type=int,
        default=5,
        help="Maximum TMDB pages to inspect per catalogue (default: 5; each page is about 20 titles).",
    )
    parser.add_argument(
        "--max-titles",
        type=int,
        default=500,
        help="Maximum unique movie IDs to process; use 0 for no limit (default: 500).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between OTT lookups (default: 1.0).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh even fresh cached entries. Use carefully because this makes more provider requests.",
    )
    parser.add_argument(
        "--include-upcoming",
        action="store_true",
        help="Also include unreleased titles. By default, only released catalogue entries are used.",
    )
    return parser.parse_args()


async def load_catalogue_page(key: str, page: int, released_only: bool) -> dict:
    cache_key = (
        key + ("|released-v4" if released_only else "|upcoming-v3"),
        page,
    )
    cached = main._read_tmdb_persistent_cache(cache_key)
    ttl = 30 * 60 if key.startswith("trending-") else main.TMDB_CATALOGUE_CACHE_SECONDS
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    try:
        payload = await main._tmdb_fetch_movie_catalogue(key, page, released_only)
    except Exception:
        # Match the app's resilience: use an expired persistent catalogue as a
        # best-effort fallback if TMDB is unreachable during a manual warm-up.
        if cached:
            print(f"[stale catalogue] Using saved {key} page {page} because TMDB failed.")
            return cached[1]
        raise
    main._write_tmdb_persistent_cache(cache_key, payload)
    return payload


async def run() -> int:
    args = parse_args()
    region = args.region.strip().upper()
    if len(region) != 2 or not region.isalpha() or not region.isascii():
        raise SystemExit("--region must be a two-letter country code, such as IN.")
    if args.pages_per_catalogue < 1:
        raise SystemExit("--pages-per-catalogue must be at least 1.")
    if args.max_titles < 0 or args.delay < 0:
        raise SystemExit("--max-titles and --delay cannot be negative.")

    if not main.WATCHMODE_API_KEYS:
        raise SystemExit(
            "No Watchmode key found. Set WATCHMODE_API_KEYS in the container environment."
        )
    if not (main.TMDB_READ_ACCESS_TOKENS or main.TMDB_API_KEY):
        raise SystemExit(
            "No TMDB credentials found. Configure TMDB_READ_ACCESS_TOKEN(S) or TMDB_API_KEY."
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cache_path = main.TMDB_OTT_CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"Region: {region}")
    print(f"Watchmode keys configured: {len(main.WATCHMODE_API_KEYS)} (values hidden)")
    print(f"Persistent OTT cache: {cache_path}")
    print(f"Catalogues: {len(CATALOGUES)}; pages per catalogue: {args.pages_per_catalogue}")
    print(f"Maximum unique titles: {'unlimited' if args.max_titles == 0 else args.max_titles}")
    print(f"Refresh fresh entries: {'yes' if args.refresh else 'no'}")
    print("")

    ids: dict[int, str] = {}
    released_only = not args.include_upcoming
    for key in CATALOGUES:
        for page in range(1, args.pages_per_catalogue + 1):
            try:
                payload = await load_catalogue_page(key, page, released_only)
            except Exception as exc:
                print(f"[catalogue error] {key} page {page}: {type(exc).__name__}: {exc}")
                break
            rows = payload.get("results") or []
            if not rows:
                break
            for row in rows:
                try:
                    movie_id = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                if movie_id > 0:
                    ids.setdefault(movie_id, str(row.get("title") or f"Movie {movie_id}"))
            total_pages = int(payload.get("totalPages") or page)
            if page >= total_pages:
                break

    ordered = list(ids.items())
    if args.max_titles:
        ordered = ordered[: args.max_titles]
    print(f"Unique movie IDs collected: {len(ids)}; this run will inspect: {len(ordered)}")

    stats = {"cached": 0, "updated": 0, "errors": 0, "watchmode": 0, "tmdb_fallback": 0}
    total = len(ordered)
    for index, (movie_id, title) in enumerate(ordered, start=1):
        disk_entry = main._read_tmdb_ott_persistent_cache((movie_id, region))
        if not args.refresh and disk_entry:
            saved_at, cached_payload = disk_entry
            age = max(0, time.time() - saved_at)
            if age < main.TMDB_OTT_CACHE_SECONDS:
                stats["cached"] += 1
                continue

        try:
            result = await main.api_tmdb_movie_ott_availability(movie_id, region)
            stats["updated"] += 1
            source = str(result.get("source") or "unknown")
            if source.lower() == "watchmode":
                stats["watchmode"] += 1
            else:
                stats["tmdb_fallback"] += 1
            status = str(result.get("status") or "unknown")
            providers = len(result.get("streamingProviders") or [])
            print(
                f"[{index}/{total}] {title[:65]} (TMDB {movie_id}): "
                f"{status}, streaming providers={providers}, source={source}"
            )
        except Exception as exc:
            stats["errors"] += 1
            print(f"[{index}/{total}] ERROR {title[:65]} (TMDB {movie_id}): {type(exc).__name__}: {exc}")

        # If Watchmode signalled HTTP 429, stop sending requests until its
        # Retry-After window expires. This pause is local to this manual run.
        cooldown_until = float(getattr(main, "_WATCHMODE_RATE_LIMITED_UNTIL", 0.0) or 0.0)
        if cooldown_until > time.time():
            wait_seconds = cooldown_until - time.time()
            print(f"\n[rate limit] Watchmode asked us to pause; waiting {int(wait_seconds) + 1}s.")
            await asyncio.sleep(wait_seconds)
        elif args.delay:
            await asyncio.sleep(args.delay)

    elapsed = int(time.monotonic() - started)
    print("\nOTT cache warm-up finished.")
    print(f"Finished at: {datetime.now(timezone.utc).isoformat()}")
    print(f"Fresh entries skipped: {stats['cached']}")
    print(f"Entries updated: {stats['updated']}")
    print(f"Watchmode results: {stats['watchmode']}")
    print(f"TMDB fallback results: {stats['tmdb_fallback']}")
    print(f"Errors: {stats['errors']}")
    print(f"Elapsed: {elapsed // 60}m {elapsed % 60}s")
    print(f"Cache file: {cache_path}")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
