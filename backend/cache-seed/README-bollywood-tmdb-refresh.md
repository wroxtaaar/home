# Refresh Bollywood metadata through today

Notebook: `bollywood_tmdb_refresh.ipynb`

## What it does

- Uses TMDB's paginated `discover/movie` API with Hindi original language, India origin, and primary release date no later than the day the notebook runs.
- Reads the historical metadata seed from the `feature/background-scraped-catalogue` branch and merges candidates by normalized title + year.
- Writes `bollywood_tmdb_updated_metadata.json` as a new export with an `asOfDate`, counts, historical records, and newly discovered TMDB records.
- Handles transient TMDB rate limits/server errors with retries.

## Run it in Google Colab

1. Open `bollywood_tmdb_refresh.ipynb` from this branch in GitHub and choose **Open in Colab**, or upload the notebook to Colab.
2. In Colab **Secrets**, add `TMDB_READ_ACCESS_TOKEN` with your TMDB API Read Access Token. Enable notebook access to the secret. If the secret is unavailable, the notebook prompts for the token without echoing it.
3. Run all cells. `MAX_PAGES = 500` allows up to TMDB's documented discover pagination cap; lower it for a quick test.
4. Download `bollywood_tmdb_updated_metadata.json` from the Colab file panel after the final cell finishes.

## Important safeguards

- This is a **metadata export**, not a runtime integration or deployment.
- Do not overwrite `latest_bollywood_catalogue.json`. That checked-in file contains torrent results and has a different schema.
- The notebook does not scrape IMDb pages. It uses the historical CSV-derived seed plus TMDB API data.
- Hindi + India-origin filters are only a candidate filter, not a definitive Bollywood classifier. Inspect the results before runtime integration.
- New TMDB-only rows include title/year/overview/poster path/genre IDs, but do not yet hydrate IMDb IDs, genre names, director, or cast. Add a separate detail-enrichment pass if those fields are needed.
- The notebook currently reads the historical seed from this feature branch. It does not commit the generated output back to GitHub automatically.
- Keep API tokens in Colab Secrets or a local environment variable; never commit tokens.

## Sources

- Historical seed: [bollywood_imdb_historical_metadata.json](https://github.com/wroxtaaar/home/blob/feature/background-scraped-catalogue/backend/cache-seed/bollywood_imdb_historical_metadata.json)
- Source CSV: [Bollywood-Movie-Dataset CSV](https://github.com/devensinghbhagtani/Bollywood-Movie-Dataset/blob/main/IMDB-Movie-Dataset%282023-1951%29.csv)
- TMDB API docs: [Discover](https://developer.themoviedb.org/reference/discover-movie)
