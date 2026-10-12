# Historical IMDb Bollywood metadata seed

File: `bollywood_imdb_historical_metadata.json`

This seed was generated from the public CSV in the external
`devensinghbhagtani/Bollywood-Movie-Dataset` repository. It contains 2,129
unique rows with an IMDb ID, title, and parseable year, plus the source
genre, overview, director, and cast fields where available.

Sources:
- CSV: https://github.com/devensinghbhagtani/Bollywood-Movie-Dataset/blob/main/IMDB-Movie-Dataset%282023-1951%29.csv
- Notebook: https://github.com/devensinghbhagtani/Bollywood-Movie-Dataset/blob/main/Scraping-Bollywood-Movies-from-IMDB.ipynb

Limitations:
- The notebook was last updated in November 2023 and scrapes IMDb's Hindi-language
  feature-film search. It is not a continuously updated scraper.
- The CSV itself includes some non-Hindi and international movies (for example
  `Jailer`, `The BFG`, `Lion`, and `Life of Pi`). Do not treat it as a
  fully verified Bollywood-only list without additional language/origin checks.
- It has thin recent-year coverage: the source includes only 31 rows from 2024,
  two from 2025, and one from 2026. Do not use it as the source of truth for
  newly released films.
- This is a metadata seed only. It is deliberately separate from
  `latest_bollywood_catalogue.json`, which stores torrent release results in
  the shape expected by the existing latest-catalogue UI. Do not replace that
  torrent cache with these movie metadata records.
- Adding this file does not wire it into runtime search. Integration should
  validate/filter source rows and match them to canonical TMDB records before
  using them to enrich app results.

The file is versioned on the `feature/background-scraped-catalogue` branch as
an experimental source asset. Production `main` is unchanged.
