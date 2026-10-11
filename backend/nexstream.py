"""Experimental NexStream resolver used only by the isolated test page.

The API key stays on the server. Only approved HTTPS EmbedMaster player URLs
are returned to the browser; no direct media URLs are exposed by this adapter.
"""
import os
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
NEXSTREAM_API_BASE = "https://api.codespecters.com"
ALLOWED_EMBED_HOSTS = {"embedmaster.link", "www.embedmaster.link"}


@router.get("/api/nexstream/resolve")
async def resolve_nexstream(
    media_type: str = Query("movie", alias="type", pattern="^(movie|tv)$"),
    tmdb_id: int = Query(..., gt=0),
    season: int | None = Query(None, ge=1),
    episode: int | None = Query(None, ge=1),
):
    """Resolve a movie or TV episode through NexStream without exposing its key."""
    api_key = os.getenv("NEXSTREAM_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="NEXSTREAM_API_KEY is not configured on the server.",
        )

    if media_type == "tv":
        if season is None or episode is None:
            raise HTTPException(
                status_code=422,
                detail="TV playback requires both season and episode.",
            )
        path = f"/api/tv/{tmdb_id}/{season}/{episode}"
    else:
        path = f"/api/movie/{tmdb_id}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(
                f"{NEXSTREAM_API_BASE}{path}",
                params={"apikey": api_key},
                headers={"Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to NexStream. Try again later.",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"NexStream returned HTTP {response.status_code}.",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="NexStream returned an invalid response.",
        ) from exc

    if not isinstance(payload, dict) or not payload.get("success"):
        message = payload.get("message") if isinstance(payload, dict) else None
        raise HTTPException(
            status_code=502,
            detail=str(message or "NexStream could not resolve this title."),
        )

    safe_sources = []
    for source in payload.get("sources", []):
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_EMBED_HOSTS:
            continue
        safe_sources.append({
            "name": str(source.get("name") or "NexStream source")[:80],
            "url": url,
        })

    if not safe_sources:
        raise HTTPException(
            status_code=502,
            detail="NexStream returned no supported EmbedMaster player source.",
        )

    return {
        "success": True,
        "type": media_type,
        "tmdbId": str(tmdb_id),
        "season": season if media_type == "tv" else None,
        "episode": episode if media_type == "tv" else None,
        "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
        "sources": safe_sources,
    }
