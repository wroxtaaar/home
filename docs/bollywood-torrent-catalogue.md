# Cached Bollywood torrent catalogue

The **Full Bollywood Library** endpoint reads only
`/app/data/bollywood_torrent_matched_catalogue.json`. The scheduled matcher searches
a bounded batch using Home's existing torrent-search implementation and atomically
publishes only titles with a plausible title/year match. Browsing the catalogue does
not trigger bulk torrent searches.

## Persistent data directory

Mount a host directory at `/app/data` when running the test container. This keeps
the refreshed TMDB metadata, matched catalogue, and progress cursor across image
rebuilds:

```bash
mkdir -p ~/home-catalogue-test/runtime-data
# Include this mount when recreating the test container:
# -v "$HOME/home-catalogue-test/runtime-data:/app/data"
```

Do not remove the old container until the new image builds successfully. Keep this
feature on `feature/background-scraped-catalogue`; production `main` is unrelated.

## Initial metadata refresh

Run once in the test container after the updated image has been deployed:

```bash
docker exec home-catalogue-test sh -lc \
  'BOLLYWOOD_METADATA_OUTPUT=/app/data/bollywood_tmdb_updated_metadata.json python /app/refresh_bollywood_metadata.py'
```

The refresh script requires the configured TMDB read-access token(s). It never logs
token values and writes the export atomically.

## Match torrents incrementally

A conservative starting batch is 150 titles per run. Override it with
`BOLLYWOOD_TORRENT_REFRESH_BATCH_SIZE` if provider capacity permits.

```bash
docker exec home-catalogue-test python /app/refresh_bollywood_torrent_catalogue.py
```

The cursor advances through the source catalogue; previously matched titles are
rechecked after the configured interval (30 days by default). A successful search
with no plausible title match removes that title from the visible catalogue. A
provider/search failure does not discard a previous match. Matching is heuristic,
so review the stored `torrentMatch.matchScore` and provider result before using this
for high-confidence availability claims.

## Suggested schedule

Use host cron after the persistent mount and first metadata refresh are in place:

```cron
# Rebuild the TMDB metadata snapshot weekly (Sunday 03:00 server time).
0 3 * * 0 docker exec home-catalogue-test sh -lc 'BOLLYWOOD_METADATA_OUTPUT=/app/data/bollywood_tmdb_updated_metadata.json python /app/refresh_bollywood_metadata.py' >> /home/ubuntu/home-catalogue-test/runtime-data/metadata-refresh.log 2>&1

# Search the next bounded batch every day (04:00 server time).
0 4 * * * docker exec home-catalogue-test python /app/refresh_bollywood_torrent_catalogue.py >> /home/ubuntu/home-catalogue-test/runtime-data/torrent-refresh.log 2>&1
```

The daily batch schedule steadily works through the library rather than firing off
thousands of provider searches at once. At 150 candidates/day, one full pass over
9,860 records takes about 66 days before accounting for retries and provider failures.
If this is too slow, increase the batch gradually while watching provider errors and
server load. The weekly metadata refresh and daily matcher are separate by design.

## Verify

```bash
docker exec home-catalogue-test python /app/refresh_bollywood_torrent_catalogue.py
curl -sS 'http://localhost:3000/api/movies/catalogue/bollywood-library?page=1&released_only=true'
```

The API response should report the matched count, not the full TMDB metadata count.
