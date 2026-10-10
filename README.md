# Torrent Studio — Vercel + Render + Seedr

Torrent Studio uses each user's own Seedr account for cloud torrent transfers and storage.

## Architecture

- **frontend/** — React + Vite.
- **backend/** — FastAPI.
- **Torrent search** — fast search/indexer endpoints.
- **Seedr** — cloud torrent storage/transfers.
- No developer Seedr storage is used for a normal connected user.

## Seedr account connection

The app uses Seedr's device authorization flow:

1. Create a Seedr account at [seedr.cc](https://www.seedr.cc/).
2. Return to Torrent Studio and choose **Connect Seedr account**.
3. Torrent Studio requests a Seedr device code.
4. Open the Seedr authorization page and enter the displayed code.
5. After approval, Torrent Studio receives the user's Seedr access token on the backend and associates it with that browser session.
6. Torrents, quota, files, downloads, and streaming use that connected Seedr account.

For users who already created a Seedr account with Google/Facebook, Seedr may require setting a password through its password-reset flow before device approval. The password is entered only on Seedr, never in Torrent Studio.

Torrent Studio never asks the user for their Seedr password.

Seedr's current settings page exposes **Add Device Code** under **Extensions, API & External Access**, and Seedr says its REST API uses OAuth2 for third-party integrations. citeturn160665search1turn160665search2

The exact device-code endpoints used by the implementation are the currently reachable Seedr device-code endpoints; the live code endpoint returns a device code, user code, verification URL, expiry, and polling interval. citeturn594779view0

## Dynamic movie catalogues

Marvel Movies, DC Live-Action and DC Animated shortcuts use the existing hardcoded title lists and persistent server-side torrent caches, so the cached torrent options are shown without requiring TMDB for the initial view. The separate **TMDB movie discovery** control lets visitors select one of those three franchises and browse paginated TMDB movie metadata instead. In TMDB discovery, OTT availability is loaded in the background and torrent-provider searches happen only when a movie's **Prepare** action is selected.

Popular Hollywood, Trending Hollywood and Popular Bollywood browse paginated movie metadata from TMDB. Popular categories sort by TMDB popularity; trending categories use TMDB's weekly trending feed and filter to the selected original language. The Latest Hollywood, Latest Bollywood and Trending Bollywood shortcuts are intentionally omitted from the current UI.

For non-commercial use, request a free TMDB API credential in your TMDB account under **Settings → API**. Add either variable to the private `.env.debug` file on the VPS (never commit credentials):

```dotenv
TMDB_READ_ACCESS_TOKEN=<your TMDB API Read Access Token>
# Or use the v3 API key instead:
# TMDB_API_KEY=<your TMDB API key>
```

The deployment workflow recreates the container from `.env.debug`, so rerun the Home VPS deployment after saving the variable. TMDB requires its logo and attribution notice in the app; the movie-browser UI includes both. Its free developer access is for non-commercial use—commercial use needs a separate TMDB agreement.

## OTT availability

Movie catalogue cards check India (`IN`) streaming availability in the background. Home distinguishes subscription/free/ad-supported streaming from rental and purchase options, displays the regional digital release date when TMDB has one, and offers a **Streaming now in India** filter. A missing provider listing is shown as unconfirmed rather than proof that a movie is unavailable.

The backend endpoint is `GET /api/movies/ott/{movie_id}?region=IN`. It queries TMDB's movie watch-provider and regional release-date endpoints, deduplicates concurrent lookups, and caches results for 24 hours in `/app/data/tmdb_ott_availability_cache.json` (override with `TMDB_OTT_CACHE_FILE` or `TMDB_OTT_CACHE_SECONDS`). Provider availability is powered by JustWatch; the UI includes the required JustWatch attribution. TMDB attribution remains required as described above.

## Vercel

Set the project Root Directory to `frontend`.

Environment variable:

```
VITE_API_URL=https://YOUR-RENDER-SERVICE.onrender.com
```

## Render

Set the service Root Directory to `backend`.

For the normal per-user flow, no personal `SEEDR_API_TOKEN` or shared `SEEDR_LIBRARY_FOLDER_ID` is required. Set `SEEDR_SESSION_SECRET` to a stable random secret so encrypted personal Seedr sessions survive backend restarts. If it is omitted, personal connections reset when the backend process restarts.

Optional settings:

```
SEEDR_DEVICE_CLIENT_ID=seedr_xbmc
SEEDR_SESSION_TTL_SECONDS=2592000
SEEDR_SESSION_SECRET=<long-random-secret>
CORS_ORIGINS=https://YOUR-VERCEL-DOMAIN.vercel.app
```

A legacy developer token can only be used when explicitly enabled with:

```
ALLOW_LEGACY_SEEDR_TOKEN=true
SEEDR_API_TOKEN=<developer Seedr token>
```

Do not enable the legacy mode for the normal multi-user deployment.

## Local development

Backend:

```bash
cd backend
python -m venv .venv
pip install -r requirements.txt
uvicorn main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Then set `VITE_API_URL=http://127.0.0.1:8000` for the frontend.

## Main Seedr API routes

- `GET /api/seedr/session`
- `POST /api/seedr/connect/start`
- `GET /api/seedr/connect/status`
- `POST /api/seedr/connect/disconnect`
- `POST /api/seedr/add`
- `GET /api/seedr/quota`
- `GET /api/seedr/library`
- `GET /api/seedr/files`
- `GET /api/seedr/files/:id/download`

The repository also contains compatibility endpoints used by the existing UI. They do not start a qBittorrent process or create shared local torrent storage.
