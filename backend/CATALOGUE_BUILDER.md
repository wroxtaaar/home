# Background scraper-driven catalogue builder

The Home scraper can discover titles for the metadata catalogue without making normal
search requests slower. This job runs separately from the user's search and Seedr flow.

## Run it on the Oracle VPS

From the VPS, check the running Home container name with `docker ps`, then run:

```bash
docker exec -it home python /app/catalogue_builder.py --defaults --max-titles 150 --per-query 30
```

Or target specific areas/titles:

```bash
docker exec -it home python /app/catalogue_builder.py \
  --query "2025 movies" \
  --query "Bollywood movies" \
  --query "popular TV series" \
  --max-titles 100
```

The container needs the same `TMDB_API_KEYS` or `TMDB_READ_ACCESS_TOKENS` environment
configuration as the Home backend. Use `--verbose` to see query progress.

## What it does

1. Runs the existing 1337x-backed Home scraper for a list of broad or user-provided queries.
2. Cleans release tags from discovered torrent names and deduplicates title/year pairs.
3. Resolves candidates against TMDB's multi-search endpoint, keeping movie and TV matches.
4. Merges useful title metadata into the existing persistent search metadata cache at
   `TMDB_SEARCH_METADATA_CACHE_FILE` (normally `/app/data/tmdb_search_metadata_cache.json`).
5. Leaves the existing committed franchise/popular catalogue files untouched.

The metadata cache is the same source already read by the search metadata batch resolver, so
catalogued matches can be returned from local data instead of requiring a fresh metadata API
lookup. It does not store torrent files or magnets in the metadata catalogue.

## Scheduling

For recurring discovery, schedule the command on the VPS with cron after confirming one manual
run succeeds. Example: run daily at 04:20 server time (adjust the container name if needed):

```cron
20 4 * * * docker exec home python /app/catalogue_builder.py --defaults --max-titles 150 --per-query 30 >> /home/ubuntu/catalogue-builder.log 2>&1
```

**Persistence note:** the cache must live on persistent storage for additions to survive container
replacement. If `/app/data` is not backed by a Docker volume or bind mount in the current
deployment, configure one before relying on scheduled runs; otherwise the container's writable
layer can be lost on recreation. The job does not automatically run at app startup, so it won't
add load to interactive searches.
