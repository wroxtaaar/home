# Background catalogue builder

The builder runs independently from interactive search and Seedr preparation. It writes
canonical movie/TV metadata into the persistent search metadata cache; it does not edit or
delete the checked-in franchise/popular catalogue files.

## Run it on the Oracle VPS

Pull/build the experimental branch in the test checkout, then rebuild the separate test image.
Do not rebuild or replace the original home container while validating this branch.

    cd ~/home-catalogue-test
    git pull origin feature/background-scraped-catalogue
    docker build -t home-catalogue-test .
    docker rm -f home-catalogue-test
    docker run -d --name home-catalogue-test --restart unless-stopped -p 3000:10000 -e PORT=10000 home-catalogue-test

Make sure the test container has TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS configured
without printing the values in your terminal history or logs.

Run a small test first:

    docker exec -it home-catalogue-test python /app/catalogue_builder.py --query "2025 movies" --query "Bollywood movies" --max-titles 20 --per-query 10 --verbose

Then run the defaults if the small run reports successful TMDB records and cache writes:

    docker exec -it home-catalogue-test python /app/catalogue_builder.py --defaults --max-titles 150 --per-query 30 --verbose

## Discovery strategy

1. TMDB discover/movie and discover/tv provide canonical IDs, titles, release years,
   artwork, ratings, and overviews first.
2. Year-specific queries preserve their year filter; Hindi/Bollywood queries filter to Hindi
   titles, Hollywood queries filter to English titles, and anime queries filter by animation genre.
3. A portion of the result limit is reserved for plausible scraper-discovered titles, allowing
   less-popular/newly indexed titles to supplement the discovery results.
4. Torrent title candidates with pack/collection/music noise and excessive release-description
   text are rejected before TMDB multi-search.
5. Transport/API failures are counted separately from successful lookups with no matching title.

## Cache destination

Metadata is merged through the existing _persist_search_metadata_cache_rows writer into
TMDB_SEARCH_METADATA_CACHE_FILE (normally /app/data/tmdb_search_metadata_cache.json).
The builder does not store torrent files or magnets.

**Persistence note:** confirm that /app/data is mounted to persistent storage before relying
on results surviving container recreation. The job is not run automatically at app startup.
