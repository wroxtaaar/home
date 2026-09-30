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
