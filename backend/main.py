import asyncio
from html import unescape
import base64
import contextvars
import secrets
from datetime import datetime, timezone
from collections import deque
import json
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

import httpx
import libtorrent as lt
from cryptography.fernet import Fernet, InvalidToken
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

APP_NAME = "Torrent Studio API"
logger = logging.getLogger("torrent-studio")

# TMDB provides dynamic, paginated movie discovery. Credentials stay server-side;
# use a free developer key for non-commercial use or the API Read Access Token.
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_READ_ACCESS_TOKEN = (
    os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()
    or os.getenv("TMDB_BEARER_TOKEN", "").strip()
)
TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
# If TMDB API connections are reset before TLS completes, pause new API attempts
# briefly so catalogue requests fail fast and can fall back to persistent caches.
TMDB_API_CONNECT_FAILURE_COOLDOWN_SECONDS = max(15, int(os.getenv("TMDB_API_CONNECT_FAILURE_COOLDOWN_SECONDS", "60")))
_tmdb_api_connect_failure_until = 0.0
TMDB_CATALOGUE_CACHE_SECONDS = max(300, int(os.getenv("TMDB_CATALOGUE_CACHE_SECONDS", str(6 * 60 * 60))))
TMDB_COMPANY_CACHE_SECONDS = max(3600, int(os.getenv("TMDB_COMPANY_CACHE_SECONDS", str(24 * 60 * 60))))
TMDB_CATALOGUE_CACHE_FILE = Path(os.getenv("TMDB_CATALOGUE_CACHE_FILE", "/app/data/tmdb_movie_catalogue_cache.json"))
TMDB_OTT_CACHE_SECONDS = max(3600, int(os.getenv("TMDB_OTT_CACHE_SECONDS", str(24 * 60 * 60))))
TMDB_OTT_CACHE_FILE = Path(os.getenv("TMDB_OTT_CACHE_FILE", "/app/data/tmdb_ott_availability_cache.json"))
_tmdb_ott_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_tmdb_ott_inflight: dict[tuple[int, str], asyncio.Task[dict[str, Any]]] = {}
_tmdb_movie_catalogue_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_tmdb_movie_catalogue_inflight: dict[tuple[str, int], asyncio.Task[dict[str, Any]]] = {}
_tmdb_company_id_cache: dict[str, tuple[float, list[str]]] = {}
_tmdb_company_inflight: dict[str, asyncio.Task[list[str]]] = {}
_tmdb_cache_clear_last_at = 0.0

GITHUB_FEEDBACK_TOKEN = os.getenv("GITHUB_FEEDBACK_TOKEN", "").strip()
GITHUB_FEEDBACK_REPO = os.getenv("GITHUB_FEEDBACK_REPO", "wroxtaaar/new-test").strip()
SEEDR_BASE = "https://www.seedr.cc/api/v0.1/p"
SEEDR_MEDIA_BASE = "https://www.seedr.cc/api"
SEEDR_V2_BASE = "https://v2.seedr.cc/api/v0.1/p"
SEEDR_PAT_BASE = "https://www.seedr.cc/api/v0.1/p"
# Legacy developer-level Seedr credentials remain supported only when explicitly enabled.
# Normal requests use a per-browser Seedr connection established through device auth.
SEEDR_TOKEN = os.getenv("SEEDR_API_TOKEN", "").strip()
ALLOW_LEGACY_SEEDR_TOKEN = os.getenv("ALLOW_LEGACY_SEEDR_TOKEN", "false").strip().lower() in {"1", "true", "yes", "on"}
# A shared folder ID cannot be used safely across different users. Personal
# connections always start at each Seedr account's own root folder.
SEEDR_LIBRARY_FOLDER_ID = "0"
SEEDR_DEVICE_CLIENT_ID = os.getenv("SEEDR_DEVICE_CLIENT_ID", "seedr_xbmc").strip() or "seedr_xbmc"
SEEDR_DEVICE_CODE_URL = "https://www.seedr.cc/api/device/code"
SEEDR_DEVICE_AUTHORIZE_URL = "https://www.seedr.cc/api/device/authorize"
SEEDR_SESSION_COOKIE = os.getenv("SEEDR_SESSION_COOKIE", "torrent_studio_seedr_session").strip() or "torrent_studio_seedr_session"
SEEDR_SESSION_TTL_SECONDS = int(float(os.getenv("SEEDR_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60))))
SEEDR_SESSION_SECRET = os.getenv("SEEDR_SESSION_SECRET", "").strip()
if SEEDR_SESSION_SECRET:
    _seedr_fernet_key = base64.urlsafe_b64encode(hashlib.sha256(SEEDR_SESSION_SECRET.encode("utf-8")).digest())
else:
    _seedr_fernet_key = Fernet.generate_key()
    logger.warning("SEEDR_SESSION_SECRET is not configured; personal Seedr connections will reset on backend restart.")
_seedr_fernet = Fernet(_seedr_fernet_key)

_seedr_sessions: dict[str, dict[str, Any]] = {}
_seedr_request_token: contextvars.ContextVar[str] = contextvars.ContextVar("seedr_request_token", default="")
_seedr_request_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("seedr_request_session_id", default="")
SEARCH_STOPWORDS = {"the", "a", "an", "movie", "film", "series", "season", "episode", "web", "show", "tv"}
TORRENT_SEARCH_API_URL = os.getenv("TORRENT_SEARCH_API_URL", "https://torrent-search-api-ujfa.onrender.com").rstrip("/")
KNABEN_API_URL = os.getenv("KNABEN_API_URL", "https://api.knaben.org/v1").rstrip("/")
TORRENT_METADATA_API_URL = os.getenv("TORRENT_METADATA_API_URL", "https://torrentmeta.fly.dev").rstrip("/")
FAST_SEARCH_TEST_URL = os.getenv("FAST_SEARCH_TEST_URL", "https://torrent-search-test.onrender.com").rstrip("/")
SEARCH_SOURCE_TIMEOUT_SECONDS = float(os.getenv("SEARCH_SOURCE_TIMEOUT_SECONDS", "2.25"))
SEARCH_TOTAL_TIMEOUT_SECONDS = float(os.getenv("SEARCH_TOTAL_TIMEOUT_SECONDS", "3.5"))
SEARCH_GRACE_SECONDS = float(os.getenv("SEARCH_GRACE_SECONDS", "0.2"))
SEARCH_CACHE_SECONDS = float(os.getenv("SEARCH_CACHE_SECONDS", "60"))
SEARCH_CACHE_STALE_SECONDS = float(os.getenv("SEARCH_CACHE_STALE_SECONDS", "600"))
SEARCH_CACHE_MAX_ENTRIES = int(os.getenv("SEARCH_CACHE_MAX_ENTRIES", "75"))
SEARCH_CACHE_MIN_RESULTS = int(os.getenv("SEARCH_CACHE_MIN_RESULTS", "8"))
# Home intentionally uses the wider 100 MB–5 GB search window.
# new-test remains the separate 100 MB–2 GB variant.
MAX_SEARCH_RESULT_SIZE_BYTES = 5 * 1024 * 1024 * 1024
SEARCH_COMPOUND_ALIASES = {
    "antman": "ant man",
    "spiderman": "spider man",
    "ironman": "iron man",
    "blackpanther": "black panther",
    "doctorstrange": "doctor strange",
    "captainamerica": "captain america",
    "guardiansofthegalaxy": "guardians of the galaxy",
}
# Match the movie app's mirror strategy. YTS availability varies by ISP/domain,
# so keep multiple API mirrors and allow an environment override for ordering.
# YTS domains are inconsistent across networks. The accelerator API is a
# keyless YTS-compatible mirror, followed by the public YTS domains.
YTS_API_HOSTS = tuple(
    host.strip()
    for host in os.getenv(
        "YTS_API_HOSTS",
        "movies-api.accel.li,yts.bz,yts.mx,yts.am,yts.lt,yts.rs",
    ).split(",")
    if host.strip()
)
FAST_SEARCH_TRACKERS = (
    "http://tracker.dler.org:6969/announce",
    "http://tracker2.dler.org:80/announce",
    "http://1337.abcvg.info:80/announce",
)
TORRENT_METADATA_CACHE_FILE = Path(os.getenv("TORRENT_METADATA_CACHE_FILE", "/app/.torrent_metadata_cache.json"))
TORRENT_METADATA_JOB_TIMEOUT_SECONDS = float(os.getenv("TORRENT_METADATA_JOB_TIMEOUT_SECONDS", "60"))
TORRENT_METADATA_ITORRENTS_BASE_URL = os.getenv("TORRENT_METADATA_ITORRENTS_BASE_URL", "https://itorrents.net/torrent").rstrip("/")
TORRENT_METADATA_ITORRENTS_TIMEOUT_SECONDS = float(os.getenv("TORRENT_METADATA_ITORRENTS_TIMEOUT_SECONDS", "5"))
TORRENT_METADATA_BACKGROUND_TTL_SECONDS = float(os.getenv("TORRENT_METADATA_BACKGROUND_TTL_SECONDS", str(6 * 60 * 60)))
TORRENT_METADATA_BACKGROUND_RETRY_SECONDS = float(os.getenv("TORRENT_METADATA_BACKGROUND_RETRY_SECONDS", "30"))
TORRENT_METADATA_JOB_RETENTION_SECONDS = float(os.getenv("TORRENT_METADATA_JOB_RETENTION_SECONDS", str(60 * 60)))
_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_search_inflight: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}

# Rolling server-side byte counters for browser media streams. This is more
# reliable than Resource Timing for long-lived media responses, which browsers
# may report with transferSize=0 until the response completes.
_media_download_stats: dict[str, deque[tuple[float, int]]] = {}
_media_stats_lock = asyncio.Lock()
_MEDIA_STATS_WINDOW_SECONDS = 5.0

def _record_media_bytes(file_id: str, amount: int) -> None:
    if amount <= 0:
        return
    now = time.monotonic()
    bucket = _media_download_stats.setdefault(file_id, deque())
    bucket.append((now, amount))
    cutoff = now - _MEDIA_STATS_WINDOW_SECONDS
    while bucket and bucket[0][0] < cutoff:
        bucket.popleft()

def _media_download_speed(file_id: str) -> float:
    now = time.monotonic()
    bucket = _media_download_stats.get(file_id)
    if not bucket:
        return 0.0
    cutoff = now - _MEDIA_STATS_WINDOW_SECONDS
    while bucket and bucket[0][0] < cutoff:
        bucket.popleft()
    if not bucket:
        return 0.0
    total = sum(amount for _, amount in bucket)
    span = max(0.5, now - bucket[0][0])
    return total / span


# Metadata is deliberately independent of Seedr. The cache survives requests
# within a Render instance, while the global libtorrent session keeps DHT state
# warm between magnets until the container is restarted.
_metadata_cache: dict[str, dict[str, Any]] = {}
_metadata_jobs: dict[str, dict[str, Any]] = {}
_libtorrent_session: Any | None = None
_libtorrent_session_lock: asyncio.Lock | None = None

class SeedrError(Exception):
    def __init__(self, code: str, status_code: int, detail: str):
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


app = FastAPI(title=APP_NAME)

@app.exception_handler(SeedrError)
async def handle_seedr_error(request: Request, exc: SeedrError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "detail": exc.detail,
            "code": exc.code,
            "provider": "seedr",
        },
    )

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
CORS_ORIGIN_REGEX = os.getenv(
    "CORS_ORIGIN_REGEX",
    r"https://([a-zA-Z0-9-]+\.)*vercel\.app|https://([a-zA-Z0-9-]+\.)*onrender\.com|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _seedr_empty_session() -> dict[str, Any]:
    now = time.time()
    return {
        "csrf_token": secrets.token_urlsafe(32),
        "access_token": "",
        "refresh_token": "",
        "token_type": "Bearer",
        "auth_mode": "legacy",
        "device": None,
        "created_at": now,
        "last_seen": now,
    }

def _seedr_new_session() -> tuple[str, dict[str, Any]]:
    session_id = secrets.token_urlsafe(32)
    session = _seedr_empty_session()
    _seedr_sessions[session_id] = session
    return session_id, session

def _seedr_get_session(session_id: str) -> dict[str, Any]:
    return _seedr_sessions.setdefault(session_id, _seedr_empty_session())

def _seedr_session_cookie_value(session_id: str, session: dict[str, Any]) -> str:
    payload = {
        "v": 1,
        "sid": session_id,
        "csrf": str(session.get("csrf_token") or ""),
        "access": str(session.get("access_token") or ""),
        "refresh": str(session.get("refresh_token") or ""),
        "type": str(session.get("token_type") or "Bearer"),
        "mode": str(session.get("auth_mode") or "legacy"),
        "device": session.get("device") if isinstance(session.get("device"), dict) else None,
        "created": float(session.get("created_at") or time.time()),
        "last": time.time(),
    }
    encrypted = _seedr_fernet.encrypt(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return "v1." + encrypted.decode("ascii")

def _seedr_restore_session(raw_cookie: str) -> tuple[str, dict[str, Any]] | None:
    if not raw_cookie.startswith("v1."):
        return None
    try:
        payload = json.loads(_seedr_fernet.decrypt(raw_cookie[3:].encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        session_id = str(payload.get("sid") or "").strip()
        csrf_token = str(payload.get("csrf") or "").strip()
        if not session_id or not csrf_token:
            return None
        now = time.time()
        last_seen = float(payload.get("last") or 0)
        if not last_seen or now - last_seen > SEEDR_SESSION_TTL_SECONDS:
            return None
        return session_id, {
            "csrf_token": csrf_token,
            "access_token": normalize_seedr_token(str(payload.get("access") or "")),
            "refresh_token": str(payload.get("refresh") or "").strip(),
            "token_type": str(payload.get("type") or "Bearer").strip() or "Bearer",
            "auth_mode": str(payload.get("mode") or "legacy").strip().lower() or "legacy",
            "device": payload.get("device") if isinstance(payload.get("device"), dict) else None,
            "created_at": float(payload.get("created") or now),
            "last_seen": now,
        }
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return None

def current_seedr_token() -> str:
    """Return the Seedr token for the current request's connected account."""
    token = _seedr_request_token.get().strip()
    if token:
        return normalize_seedr_token(token)
    if ALLOW_LEGACY_SEEDR_TOKEN and SEEDR_TOKEN:
        return normalize_seedr_token(SEEDR_TOKEN)
    return ""

def _seedr_session_token(session_id: str) -> str:
    session = _seedr_sessions.get(session_id)
    return normalize_seedr_token(str(session.get("access_token") or "")) if session else ""


def _seedr_session_auth_mode(session_id: str) -> str:
    session = _seedr_sessions.get(session_id)
    mode = str(session.get("auth_mode") or "legacy").strip().lower() if session else "legacy"
    return mode if mode in {"legacy", "pat"} else "legacy"


def _seedr_session_fingerprint(session_id: str) -> str:
    """Short non-secret identifier for correlating session logs."""
    if not session_id:
        return "none"
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]

def _clear_seedr_session_token(session_id: str | None = None) -> None:
    sid = session_id or _seedr_request_session_id.get().strip()
    if not sid:
        return
    session = _seedr_sessions.get(sid)
    if not session:
        return
    session["access_token"] = ""
    session["refresh_token"] = ""
    session["token_type"] = "Bearer"
    session["auth_mode"] = "legacy"
    session["device"] = None
    session["last_seen"] = time.time()

@app.middleware("http")
async def attach_seedr_session(request: Request, call_next):
    raw_cookie = str(request.cookies.get(SEEDR_SESSION_COOKIE) or "").strip()
    restored = _seedr_restore_session(raw_cookie)
    is_new = False

    if restored:
        session_id, restored_session = restored
        _seedr_sessions[session_id] = restored_session
    elif raw_cookie in _seedr_sessions:
        session_id = raw_cookie
    else:
        session_id, _ = _seedr_new_session()
        is_new = True

    session = _seedr_get_session(session_id)
    session["last_seen"] = time.time()

    token_ctx = _seedr_request_token.set(_seedr_session_token(session_id))
    session_ctx = _seedr_request_session_id.set(session_id)
    try:
        path = request.url.path or ""
        method = request.method.upper()
        if path.startswith("/api/seedr/") and method not in {"GET", "HEAD", "OPTIONS"}:
            if not _seedr_session_csrf_ok(request):
                response = JSONResponse(
                    status_code=403,
                    content={"error": "Seedr session security check failed.", "code": "SEEDR_CSRF_INVALID"},
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
    finally:
        _seedr_request_token.reset(token_ctx)
        _seedr_request_session_id.reset(session_ctx)

    is_https = (
        request.url.scheme.lower() == "https"
        or str(request.headers.get("x-forwarded-proto") or "").lower() == "https"
    )
    response.set_cookie(
        SEEDR_SESSION_COOKIE,
        _seedr_session_cookie_value(session_id, session),
        max_age=SEEDR_SESSION_TTL_SECONDS,
        httponly=True,
        secure=is_https,
        samesite="none" if is_https else "lax",
        path="/",
    )

    now = time.time()
    if len(_seedr_sessions) > 2000:
        stale = [
            sid for sid, item in _seedr_sessions.items()
            if now - float(item.get("last_seen") or now) > SEEDR_SESSION_TTL_SECONDS
        ]
        for sid in stale:
            _seedr_sessions.pop(sid, None)

    return response

def _seedr_session_csrf_ok(request: Request) -> bool:
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_sessions.get(session_id)
    if not session:
        return False
    expected = str(session.get("csrf_token") or "")
    supplied = str(request.headers.get("X-Torrent-Studio-CSRF") or "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


@app.middleware("http")
async def add_timing_allow_origin(request: Request, call_next):
    # Allows the frontend to read Resource Timing transfer sizes for the
    # cross-origin Render media stream when the frontend is hosted on Vercel.
    response = await call_next(request)
    response.headers["Timing-Allow-Origin"] = "*"
    return response

class FeedbackRequest(BaseModel):
    type: str
    rating: int | None = None
    message: str
    name: str | None = None


class MagnetRequest(BaseModel):
    magnet: str
    folder_id: str | int | None = None
    torrent_name: str | None = None
    size: int | float | None = None
    # Browser clients may serialize indexes as strings; normalize them in the endpoint.
    selected_indexes: list[int | str] | None = None
    manifest: list[dict[str, Any]] | None = None



def decode_torrent_metadata(raw: bytes) -> dict[bytes, Any]:
    """Decode the small bencoded .torrent metadata file produced by aria2."""
    position = 0

    def parse() -> Any:
        nonlocal position
        if position >= len(raw):
            raise ValueError("unexpected end of bencode")

        marker = raw[position:position + 1]
        if marker == b"i":
            position += 1
            end = raw.find(b"e", position)
            if end < 0:
                raise ValueError("unterminated integer")
            value = int(raw[position:end])
            position = end + 1
            return value

        if marker == b"l":
            position += 1
            value = []
            while position < len(raw) and raw[position:position + 1] != b"e":
                value.append(parse())
            if position >= len(raw):
                raise ValueError("unterminated list")
            position += 1
            return value

        if marker == b"d":
            position += 1
            value: dict[bytes, Any] = {}
            while position < len(raw) and raw[position:position + 1] != b"e":
                key = parse()
                if not isinstance(key, bytes):
                    raise ValueError("dictionary key is not bytes")
                value[key] = parse()
            if position >= len(raw):
                raise ValueError("unterminated dictionary")
            position += 1
            return value

        if marker.isdigit():
            colon = raw.find(b":", position)
            if colon < 0:
                raise ValueError("invalid byte string")
            size = int(raw[position:colon])
            position = colon + 1
            end = position + size
            if end > len(raw):
                raise ValueError("byte string exceeds metadata")
            value = raw[position:end]
            position = end
            return value

        raise ValueError("invalid bencode token")

    value = parse()
    if position != len(raw):
        raise ValueError("trailing bencode data")
    if not isinstance(value, dict):
        raise ValueError("torrent metadata root is not a dictionary")
    return value

def _load_metadata_cache() -> None:
    global _metadata_cache
    try:
        if TORRENT_METADATA_CACHE_FILE.exists():
            data = json.loads(TORRENT_METADATA_CACHE_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                _metadata_cache = {
                    str(key).lower(): value
                    for key, value in data.items()
                    if isinstance(value, dict)
                }
    except Exception as exc:
        logger.info("Metadata cache load skipped: %s", exc)


def _save_metadata_cache() -> None:
    try:
        TORRENT_METADATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TORRENT_METADATA_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_metadata_cache, ensure_ascii=False), "utf-8")
        tmp.replace(TORRENT_METADATA_CACHE_FILE)
    except Exception as exc:
        logger.info("Metadata cache save skipped: %s", exc)


async def _fetch_itorrents_metadata(info_hash_value: str) -> dict[str, Any] | None:
    """Try the public iTorrents descriptor cache before network metadata discovery."""
    target = info_hash_value.strip().lower()
    if not TORRENT_METADATA_ITORRENTS_BASE_URL or not re.fullmatch(r"[0-9a-f]{40}", target):
        return None

    url = f"{TORRENT_METADATA_ITORRENTS_BASE_URL}/{target}.torrent"
    try:
        async with httpx.AsyncClient(
            timeout=TORRENT_METADATA_ITORRENTS_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Torrent-Studio/1.0"},
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            body = response.content
    except httpx.HTTPError as exc:
        logger.info("iTorrents cache lookup failed for %s: %s", target, exc)
        return None

    if len(body) > 8 * 1024 * 1024 or not body.startswith(b"d") or b"4:info" not in body:
        return None

    try:
        result = _metadata_from_torrent_bytes(body, target, "itorrents_cache")
    except (ValueError, TypeError) as exc:
        logger.info("iTorrents returned invalid metadata for %s: %s", target, exc)
        return None
    logger.info("Metadata cache hit for %s via iTorrents", target)
    return result


async def _fetch_source_torrent_descriptor(source_url: str, info_hash_value: str) -> dict[str, Any] | None:
    """Try to obtain a real .torrent descriptor from a trusted search detail URL."""
    if not source_url:
        return None

    try:
        parsed = urlsplit(source_url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return None

    # Search results currently expose Knaben detail URLs. Keep this narrowly
    # scoped rather than turning the endpoint into an arbitrary URL fetcher.
    allowed_hosts = {
        "knaben.org", "www.knaben.org", "api.knaben.org",
        "knaben.eu", "www.knaben.eu",
        "knaben.xyz", "www.knaben.xyz",
    }
    if host not in allowed_hosts:
        return None

    candidates = [source_url]
    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            follow_redirects=True,
            headers={"User-Agent": "Torrent-Studio/1.0"},
        ) as client:
            response = await client.get(source_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()

            if "bittorrent" in content_type or source_url.lower().split("?", 1)[0].endswith(".torrent"):
                candidates = [str(response.url)]
                raw = response.content
            else:
                html = response.text
                soup = BeautifulSoup(html, "html.parser")
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "").strip()
                    absolute = urljoin(str(response.url), href)
                    if absolute.lower().split("?", 1)[0].endswith(".torrent"):
                        candidates.append(absolute)

                # Some indexers expose a download attribute without a .torrent
                # suffix in the visible URL.
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "").strip()
                    absolute = urljoin(str(response.url), href)
                    label = " ".join(anchor.stripped_strings).lower()
                    if "torrent" in label and absolute not in candidates:
                        candidates.append(absolute)

                raw = None

            for candidate in candidates[:8]:
                try:
                    if raw is None or candidate != str(response.url):
                        descriptor = await client.get(candidate)
                        descriptor.raise_for_status()
                        candidate_type = descriptor.headers.get("content-type", "").lower()
                        body = descriptor.content
                    else:
                        candidate_type = content_type
                        body = raw

                    if len(body) > 8 * 1024 * 1024:
                        continue
                    if body.startswith(b"d") and b"4:info" in body:
                        return _metadata_from_torrent_bytes(
                            body,
                            info_hash_value,
                            "search_torrent_descriptor",
                        )
                except Exception:
                    continue
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Direct torrent descriptor lookup failed: %s", exc)

    return None


def _metadata_magnet_with_trackers(magnet: str) -> str:
    """Add the known-good HTTP trackers used by the fast WebTorrent test."""
    try:
        parsed = urlsplit(magnet)
        params = parse_qs(parsed.query, keep_blank_values=True)
        rebuilt: list[tuple[str, str]] = []
        for key in ("xt", "dn"):
            for value in params.get(key, []):
                if value:
                    rebuilt.append((key, str(value)))
        existing = {str(value).strip() for value in params.get("tr", []) if value}
        for tracker in FAST_SEARCH_TRACKERS:
            if tracker not in existing:
                rebuilt.append(("tr", tracker))
        return "magnet:?" + urlencode(rebuilt, doseq=True)
    except Exception:
        return magnet


async def _fetch_fast_test_metadata(magnet: str, info_hash_value: str) -> dict[str, Any] | None:
    """Use the proven WebTorrent resolver service as a metadata-only race participant."""
    if not FAST_SEARCH_TEST_URL:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=4.0,
            follow_redirects=True,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as client:
            response = await client.post(
                FAST_SEARCH_TEST_URL + "/api/metadata",
                json={"magnet": magnet},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Fast WebTorrent metadata service failed for %s: %s", info_hash_value, exc)
        return None

    if not isinstance(payload, dict):
        return None

    if str(payload.get("status") or "").lower() == "completed" and isinstance(payload.get("files"), list):
        metadata = payload
    else:
        job_id = str(payload.get("jobId") or "").strip()
        if not job_id:
            return None

        deadline = time.monotonic() + min(
            12.0,
            max(0.0, TORRENT_METADATA_JOB_TIMEOUT_SECONDS),
        )
        metadata = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.75)
            remaining = max(0.5, deadline - time.monotonic())
            try:
                async with httpx.AsyncClient(
                    timeout=min(3.0, remaining),
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                ) as poll_client:
                    poll_response = await poll_client.get(
                        FAST_SEARCH_TEST_URL + "/api/metadata-jobs/" + quote(job_id, safe=""),
                    )
                    if poll_response.status_code == 404:
                        return None
                    poll_response.raise_for_status()
                    poll = poll_response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                continue

            job = poll.get("job") if isinstance(poll, dict) else None
            if not isinstance(job, dict):
                continue
            if str(job.get("status") or "").lower() == "completed":
                candidate = job.get("metadata")
                if isinstance(candidate, dict) and isinstance(candidate.get("files"), list):
                    metadata = candidate
                    break
            if str(job.get("status") or "").lower() == "failed":
                return None

        if metadata is None:
            return None

    returned_hash = str(metadata.get("infoHash") or metadata.get("hash") or "").strip().lower()
    if returned_hash and returned_hash != info_hash_value.lower():
        logger.info(
            "Fast WebTorrent metadata hash mismatch: wanted %s, got %s",
            info_hash_value,
            returned_hash,
        )
        return None

    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return None

    files: list[dict[str, Any]] = []
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        if not path:
            continue
        files.append({
            "index": int(item.get("index") if item.get("index") is not None else index),
            "name": str(item.get("name") or path),
            "size": int(float(item.get("size") or 0)),
            "path": path,
            "type": "file",
            "priority": 1,
        })
    if not files:
        return None

    result = {
        "name": str(metadata.get("name") or files[0]["name"]),
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item["size"]) for item in files),
        "source": "webtorrent_fast_test",
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    return result


def _metadata_from_torrent_bytes(raw: bytes, info_hash_value: str, source: str) -> dict[str, Any]:
    meta = decode_torrent_metadata(raw)
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent metadata has no info dictionary")

    def btext(value: Any) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")

    name = btext(info.get(b"name")) or f"Torrent {info_hash_value[:8]}"
    files: list[dict[str, Any]] = []
    multi = info.get(b"files")
    if isinstance(multi, list):
        for index, item in enumerate(multi):
            if not isinstance(item, dict):
                continue
            parts = item.get(b"path") or []
            relative = "/".join(btext(part) for part in parts) if isinstance(parts, list) else btext(parts)
            files.append({
                "index": index,
                "name": relative or name,
                "size": int(item.get(b"length") or 0),
                "path": relative or name,
                "type": "file",
                "priority": 1,
            })
    else:
        files.append({
            "index": 0,
            "name": name,
            "size": int(info.get(b"length") or 0),
            "path": name,
            "type": "file",
            "priority": 1,
        })

    result = {
        "name": name,
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item.get("size") or 0) for item in files),
        "source": source,
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    _metadata_cache[info_hash_value.lower()] = result
    _save_metadata_cache()
    return result


async def _get_libtorrent_session() -> Any:
    global _libtorrent_session, _libtorrent_session_lock
    if _libtorrent_session is not None:
        return _libtorrent_session
    if _libtorrent_session_lock is None:
        _libtorrent_session_lock = asyncio.Lock()
    async with _libtorrent_session_lock:
        if _libtorrent_session is not None:
            return _libtorrent_session
        session = lt.session()
        session.apply_settings({
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_tcp": True,
            "enable_outgoing_utp": True,
            "enable_incoming_tcp": True,
            "enable_incoming_utp": True,
            "listen_interfaces": "0.0.0.0:0",
            "dht_bootstrap_nodes": "",
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connection_speed": 50,
            "handshake_timeout": 10,
        })
        _libtorrent_session = session
        logger.info("Started persistent libtorrent metadata session")
        return session


def _metadata_from_libtorrent_sync(magnet: str, info_hash_value: str) -> dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="torrent-metadata-lt-")
    session = _libtorrent_session
    if session is None:
        raise RuntimeError("libtorrent session is not initialized")

    handle = None
    try:
        atp = lt.parse_magnet_uri(_metadata_magnet_with_trackers(magnet))
        atp.save_path = tmp
        atp.flags = atp.flags | lt.torrent_flags.upload_mode
        atp.flags = atp.flags & ~lt.torrent_flags.auto_managed
        handle = session.add_torrent(atp)

        deadline = time.monotonic() + TORRENT_METADATA_JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if handle.has_metadata():
                ti = handle.torrent_file()
                if ti is None:
                    raise RuntimeError("libtorrent returned metadata without torrent info")
                fs = ti.layout()
                files = []
                for index in range(fs.num_files()):
                    path = str(fs.file_path(index))
                    files.append({
                        "index": index,
                        "name": path,
                        "size": int(fs.file_size(index)),
                        "path": path,
                        "type": "file",
                        "priority": 1,
                    })
                return {
                    "name": str(ti.name() or f"Torrent {info_hash_value[:8]}"),
                    "hash": info_hash_value.lower(),
                    "files": files,
                    "totalSize": sum(int(item["size"]) for item in files),
                    "source": "libtorrent_metadata",
                    "pending": False,
                    "createdPreview": False,
                    "message": "Torrent metadata loaded without starting Seedr.",
                }

            for alert in session.pop_alerts():
                message = str(alert)
                if "error" in message.lower() or "tracker" in message.lower():
                    logger.info("libtorrent metadata alert: %s", message[:500])
            time.sleep(0.2)

        status = handle.status()
        raise TimeoutError(
            f"Metadata is still resolving (state={status.state}, "
            f"peers={status.num_peers}, seeds={status.num_seeds})"
        )
    finally:
        try:
            if handle is not None and handle.is_valid():
                session.remove_torrent(handle)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


async def _schedule_metadata_job_expiry(job_id: str) -> None:
    try:
        await asyncio.sleep(TORRENT_METADATA_JOB_RETENTION_SECONDS)
    except asyncio.CancelledError:
        return
    job = _metadata_jobs.get(job_id)
    if job and job.get("status") in {"ready", "error"}:
        _metadata_jobs.pop(job_id, None)


async def _run_metadata_job(job_id: str, magnet: str, info_hash_value: str) -> None:
    """Keep retrying independent metadata sources for up to the configured background TTL."""
    job = _metadata_jobs[job_id]
    started_at = float(job.get("startedAt") or time.time())
    deadline_at = float(job.get("deadlineAt") or (started_at + TORRENT_METADATA_BACKGROUND_TTL_SECONDS))
    job.update({
        "status": "resolving",
        "startedAt": started_at,
        "updatedAt": time.time(),
        "deadlineAt": deadline_at,
        "rounds": int(job.get("rounds") or 0),
    })

    async def resolve_libtorrent() -> dict[str, Any] | None:
        session = await _get_libtorrent_session()
        return await asyncio.to_thread(
            _metadata_from_libtorrent_sync,
            magnet,
            info_hash_value,
        )

    while time.time() < deadline_at:
        job["rounds"] = int(job.get("rounds") or 0) + 1
        job["updatedAt"] = time.time()

        # Shared public cache is the fastest path and costs no torrent peer work.
        cached_result = await _fetch_itorrents_metadata(info_hash_value)
        if cached_result:
            job.update({
                "status": "ready",
                "result": cached_result,
                "error": None,
                "updatedAt": time.time(),
            })
            logger.info("Metadata job %s resolved via iTorrents cache on round %s", job_id, job["rounds"])
            asyncio.create_task(_schedule_metadata_job_expiry(job_id))
            return

        remote_task = asyncio.create_task(
            _fetch_remote_torrent_metadata(magnet, info_hash_value)
        )
        knaben_task = asyncio.create_task(_lookup_knaben_by_hash(info_hash_value))
        webtorrent_task = asyncio.create_task(
            _fetch_fast_test_metadata(
                _metadata_magnet_with_trackers(magnet),
                info_hash_value,
            )
        )
        libtorrent_task = asyncio.create_task(resolve_libtorrent())
        tasks = (remote_task, knaben_task, webtorrent_task, libtorrent_task)
        errors: list[str] = []
        winner: dict[str, Any] | None = None

        try:
            # Race the independent remote/indexer/libtorrent paths. The first
            # usable file list wins; the libtorrent worker finishes before a
            # retry round begins so the same torrent is never added twice.
            for task in asyncio.as_completed(tasks):
                try:
                    result = await task
                except TimeoutError as exc:
                    errors.append(str(exc))
                    continue
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                if result and isinstance(result, dict) and result.get("files"):
                    winner = result
                    break

            if winner:
                _metadata_cache[info_hash_value.lower()] = winner
                _save_metadata_cache()
                job.update({
                    "status": "ready",
                    "result": winner,
                    "error": None,
                    "updatedAt": time.time(),
                })
                logger.info(
                    "Metadata job %s resolved via %s on round %s",
                    job_id,
                    winner.get("source", "unknown"),
                    job["rounds"],
                )
                return
        finally:
            for task in (remote_task, knaben_task, webtorrent_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                remote_task,
                knaben_task,
                webtorrent_task,
                return_exceptions=True,
            )

            # asyncio.to_thread cannot force-stop the libtorrent worker.
            # When another resolver already won, let that worker finish in the
            # shared session without delaying the successful response. When the
            # round failed, wait for it before starting the next retry round so
            # the same torrent is never added twice concurrently.
            if winner is None:
                try:
                    await libtorrent_task
                except Exception as exc:
                    errors.append(str(exc))

        if time.time() >= deadline_at:
            break
        delay = min(
            max(0, TORRENT_METADATA_BACKGROUND_RETRY_SECONDS),
            max(0, deadline_at - time.time()),
        )
        job["updatedAt"] = time.time()
        if delay > 0:
            await asyncio.sleep(delay)

    job.update({
        "status": "error",
        "error": "Torrent metadata could not be resolved within 6 hours.",
        "updatedAt": time.time(),
    })
    logger.warning("Metadata job %s exhausted its background deadline", job_id)
    asyncio.create_task(_schedule_metadata_job_expiry(job_id))


_load_metadata_cache()


def seedr_data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value

def normalize_seedr_token(value: str) -> str:
    """Normalize common Seedr API Console token copy formats without logging it."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    for _ in range(2):
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
            continue

        # A copied JSON response may be either an object or a JSON string.
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                raw = parsed.strip()
                continue
            if isinstance(parsed, dict):
                candidate = (
                    parsed.get("access_token")
                    or parsed.get("token")
                    or parsed.get("personal_access_token")
                )
                if candidate:
                    raw = str(candidate).strip()
                    continue
        except Exception:
            pass
        break

    # Some older Torrent Studio deployments stored the OAuth token as a
    # base64-encoded JSON object.
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        payload = json.loads(decoded)
        if isinstance(payload, dict) and payload.get("access_token"):
            raw = str(payload["access_token"]).strip()
    except Exception:
        pass

    return raw.strip().strip('"').strip("'").strip()


def seedr_access_token() -> str:
    """Access token used only by Seedr's legacy resource.php API."""
    return normalize_seedr_token(current_seedr_token())


async def legacy_seedr_request(
    func: str,
    method: str = "POST",
    body: dict[str, Any] | None = None,
) -> Any:
    """Call Seedr's documented legacy resource API using form data."""
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    access_token = seedr_access_token()
    if not access_token:
        raise HTTPException(503, "Seedr access token is empty")

    url = "https://www.seedr.cc/oauth_test/resource.php"
    params = {
        "access_token": access_token,
        "func": str(func),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.request(
            method,
            url,
            params=params,
            data=body or {},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        raise SeedrError(
            *seedr_problem(response.status_code, data, raw)
        )

    if isinstance(data, dict):
        raw_error = str(data.get("error") or "").strip().lower()
        error_detail = str(
            data.get("error_description") or data.get("message") or data.get("error") or ""
        ).strip()
        normalized_error = error_detail.lower().replace("_", " ")
        if raw_error not in ("", "0"):
            if (
                "not enough space" in normalized_error
                or "insufficient space" in normalized_error
                or "not enough storage" in normalized_error
                or "storage full" in normalized_error
                or "not_enough_space" in raw_error
                or "insufficient_space" in raw_error
                or "not_enough_space" in normalized_error
                or normalized_error.startswith("not enough space")
                or normalized_error.startswith("insufficient space")
            ):
                raise SeedrError(
                    "SEEDR_INSUFFICIENT_SPACE",
                    413,
                    "Seedr does not have enough free space for this torrent.",
                )
            if "access_denied" in raw_error or "access denied" in normalized_error:
                raise SeedrError(
                    "SEEDR_LIBRARY_ACCESS_DENIED",
                    403,
                    "Seedr denied this account operation.",
                )
            if "unauthor" in raw_error or "invalid token" in normalized_error:
                raise SeedrError(
                    "SEEDR_TOKEN_REJECTED",
                    401,
                    "Seedr rejected the API token.",
                )
            # Seedr's legacy API often reports operation failures inside a 200
            # JSON response. Preserve the provider's error text instead of
            # treating that as a successful task creation.
            raise SeedrError(
                "SEEDR_API_ERROR",
                502,
                error_detail or "Seedr rejected the torrent.",
            )

    return data


async def legacy_seedr_list_contents(folder_id: str = "0") -> Any:
    """List a Seedr folder using the legacy resource endpoint.
    
    The free account/token used by Torrent Studio can deny the modern root
    filesystem endpoint while still permitting list_contents through Seedr's
    legacy resource API.
    """
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    access_token = seedr_access_token()
    if not access_token:
        raise HTTPException(503, "Seedr access token is empty")

    url = "https://www.seedr.cc/oauth_test/resource.php"
    params = {
        "access_token": access_token,
        "func": "list_contents",
    }
    form = {
        "content_type": "folder",
        "content_id": str(folder_id),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.post(
            url,
            params=params,
            data=form,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        raise HTTPException(
            response.status_code,
            seedr_error_message(response.status_code, data, raw),
        )

    if isinstance(data, dict) and str(data.get("error") or "").strip() not in ("", "0"):
        raw_error = str(data.get("error") or "").strip().lower()
        if "access_denied" in raw_error or "access denied" in raw_error:
            raise SeedrError(
                "SEEDR_LIBRARY_ACCESS_DENIED",
                403,
                "Seedr denied access to the account library. The token is recognized, but this library operation is not permitted.",
            )
        if "unauthor" in raw_error or "invalid token" in raw_error:
            raise SeedrError(
                "SEEDR_TOKEN_REJECTED",
                401,
                "Seedr rejected the API token. Generate a fresh Seedr API token and update SEEDR_API_TOKEN in Render.",
            )
        raise SeedrError(
            "SEEDR_API_ERROR",
            502,
            f"Seedr legacy library request failed: {data.get('error')}",
        )

    return data


async def seedr_root_request() -> Any:
    """Fetch the Seedr account root using Seedr's dedicated root endpoint."""
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    url = "https://www.seedr.cc/api/folder"
    headers = {"Accept": "application/json"}
    params = {"access_token": seedr_access_token()}

    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        detail = raw
        if isinstance(data, dict):
            detail = (
                data.get("error_description")
                or data.get("reason_phrase")
                or data.get("message")
                or data.get("error")
                or raw
            )
        raise HTTPException(response.status_code, str(detail or "Seedr root request failed"))

    return data


def seedr_problem(status_code: int, data: Any, raw: str) -> tuple[str, int, str]:
    reason = ""
    if isinstance(data, dict):
        reason = str(
            data.get("reason_phrase")
            or data.get("error_description")
            or data.get("message")
            or data.get("error")
            or data.get("detail")
            or ""
        ).strip()

    normalized = reason.lower().replace("_", " ").strip()

    if status_code == 401 or "unauthorized" in normalized or normalized in {"invalid token", "token expired", "token invalid"}:
        return (
            "SEEDR_TOKEN_REJECTED",
            401,
            "Seedr rejected the API token. Generate a fresh Seedr API token and update SEEDR_API_TOKEN in Render.",
        )

    if status_code == 403 or normalized in {"access denied", "forbidden", "access_denied"}:
        return (
            "SEEDR_LIBRARY_ACCESS_DENIED",
            403,
            "Seedr denied access to this library operation. The API token is valid, but this operation/folder is not permitted.",
        )

    if status_code == 413 or normalized == "not enough space" or "not enough space" in normalized:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            413,
            "Seedr reports insufficient storage space for this operation.",
        )

    if status_code == 429:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            429,
            "Seedr rate limit reached. Please wait a moment and try again.",
        )

    if status_code >= 500:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            status_code,
            "Seedr is temporarily unavailable. Please try again in a moment.",
        )

    return (
        "SEEDR_API_ERROR",
        status_code,
        reason or raw[:500] or f"Seedr API request failed (HTTP {status_code})",
    )


def seedr_error_message(status_code: int, data: Any, raw: str) -> str:
    return seedr_problem(status_code, data, raw)[2]

async def seedr_request(path: str, method: str = "GET", body: Any = None, form: bool = False, base_url: str = SEEDR_BASE) -> Any:
    if not current_seedr_token():
        raise SeedrError(
            "SEEDR_TOKEN_MISSING",
            503,
            "No Seedr account is connected to this browser session.",
        )

    request_path = str(path).lstrip("/")
    selected_base_url = base_url
    if base_url == SEEDR_BASE and _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat":
        selected_base_url = SEEDR_PAT_BASE
    url = f"{str(selected_base_url).rstrip("/")}/{request_path}"
    token = normalize_seedr_token(current_seedr_token())

    kwargs: dict[str, Any] = {}
    if body is not None:
        if form:
            headers_base = {"Content-Type": "application/x-www-form-urlencoded"}
            kwargs["data"] = body
        else:
            headers_base = {"Content-Type": "application/json"}
            kwargs["json"] = body
    else:
        headers_base = {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        tried_tokens: list[str] = []
        # Never fall back from one browser session to another Seedr credential.
        candidates = [token]

        last_status = 0
        last_data: Any = None
        last_raw = ""

        for candidate in candidates:
            if not candidate or candidate in tried_tokens:
                continue
            tried_tokens.append(candidate)

            headers = {
                "Authorization": f"Bearer {candidate}",
                "Accept": "application/json",
                **headers_base,
            }
            request_started = time.monotonic()
            logger.info(
                "Seedr API request start: session=%s mode=%s method=%s url=%s "
                "token_present=%s token_length=%s token_fingerprint=%s body_keys=%s",
                _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                method,
                url,
                bool(token),
                len(token),
                hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "none",
                sorted(body.keys()) if isinstance(body, dict) else [],
            )
            try:
                response = await client.request(method, url, headers=headers, **kwargs)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - request_started) * 1000)
                logger.exception(
                    "Seedr API transport failure: session=%s mode=%s method=%s url=%s "
                    "elapsed_ms=%s exception_type=%s exception=%s",
                    _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                    _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                    method,
                    url,
                    elapsed_ms,
                    type(exc).__name__,
                    str(exc)[:1000],
                )
                raise SeedrError(
                    "SEEDR_NETWORK_ERROR",
                    502,
                    f"Seedr request could not be completed: {type(exc).__name__}: {str(exc)[:500]}",
                ) from exc

            elapsed_ms = int((time.monotonic() - request_started) * 1000)
            raw = response.text
            try:
                data = response.json() if raw else None
            except Exception:
                data = raw

            response_request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("x-correlation-id")
                or response.headers.get("cf-ray")
                or ""
            )
            logger.info(
                "Seedr API response: session=%s mode=%s method=%s path=%s status=%s "
                "elapsed_ms=%s response_bytes=%s content_type=%s provider_request_id=%s",
                _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                method,
                request_path,
                response.status_code,
                elapsed_ms,
                len(raw.encode("utf-8", errors="ignore")),
                response.headers.get("content-type", ""),
                response_request_id[:200],
            )

            if response.status_code < 400:
                if isinstance(data, dict):
                    soft = str(data.get("reason_phrase") or "").strip().lower()
                    if soft == "not_enough_space":
                        raise SeedrError(
                            "SEEDR_QUOTA_UNAVAILABLE",
                            413,
                            "Seedr reports insufficient storage space for this operation.",
                        )
                return data

            last_status, last_data, last_raw = response.status_code, data, raw
            logger.warning(
                "Seedr API request failed: session=%s mode=%s method=%s path=%s url=%s "
                "status=%s elapsed_ms=%s token_length=%s token_fingerprint=%s "
                "provider_request_id=%s response_json=%s response_body=%s",
                _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                method,
                request_path,
                url,
                response.status_code,
                elapsed_ms,
                len(token),
                hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "none",
                response_request_id[:200],
                json.dumps(data, ensure_ascii=False, default=str)[:4000] if isinstance(data, (dict, list)) else "<not-json>",
                raw[:4000] if raw else "<empty>",
            )

            break

    code, status_code, detail = seedr_problem(last_status, last_data, last_raw)
    raise SeedrError(code, status_code, detail)


def arr(value: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return []

def unwrap_seedr_task(value: Any) -> dict[str, Any]:
    """Normalize task responses that may be wrapped in data/task objects."""
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return {}
        nested = current.get("task")
        if isinstance(nested, dict):
            current = nested
            continue
        return current
    return current if isinstance(current, dict) else {}


def seedr_task_folder_id(task: dict[str, Any]) -> str:
    nested_torrent = task.get("torrent") if isinstance(task.get("torrent"), dict) else {}
    payload = task.get("torrent_payload") if isinstance(task.get("torrent_payload"), dict) else {}

    return str(
        task.get("folder_created_id")
        or task.get("folder_created")
        or task.get("folder_id")
        or task.get("folderId")
        or nested_torrent.get("folder_created_id")
        or nested_torrent.get("folder_id")
        or nested_torrent.get("folderId")
        or payload.get("folder_created_id")
        or payload.get("folder_id")
        or payload.get("folderId")
        or ""
    ).strip()


def magnet_display_name(value: Any) -> str:
    """Extract the torrent display name (dn) from a magnet-like value."""
    raw = str(value or "").strip()
    if not raw.lower().startswith("magnet:?"):
        return ""
    try:
        params = parse_qs(urlsplit(raw).query, keep_blank_values=True)
        return str((params.get("dn") or [""])[0]).strip()
    except Exception:
        return ""


def task_complete(task: dict[str, Any]) -> bool:
    """Return True only when Seedr reports a task as finished."""
    if not isinstance(task, dict):
        return False

    # Explicit completion flags are authoritative.
    for key in ("completed", "complete", "finished", "done", "is_completed", "isComplete"):
        value = task.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, (int, float)) and value == 1:
            return True

    state = str(
        task.get("state")
        or task.get("status")
        or task.get("phase")
        or ""
    ).strip().lower().replace("-", "_")

    try:
        progress = float(task.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0.0

    # A new Prepare must be able to replace stuck, queued, paused, or
    # metadata/seed-discovery tasks, even if the provider calls them "seeding".
    # Keep these as unfinished unless Seedr's explicit completion flags above
    # confirm completion. This avoids treating "Collecting Seeds" as completed.
    active_states = {
        "paused", "paused_dl", "pauseddl", "paused_download",
        "queued", "queued_dl", "queueddl", "queued_download",
        "downloading", "download", "collecting_seeds", "collecting seeds",
        "waiting", "metadata", "fetching_metadata", "checking",
    }
    if state in active_states:
        return False

    completed_states = {"complete", "completed", "finished", "done", "success"}
    if state in completed_states:
        return True

    # "seeding" normally means the payload is complete, but some Seedr task
    # variants use it for a stalled seed-discovery state without progress.
    if state == "seeding":
        return progress >= 100

    # A numeric 100% is authoritative even when the state field is absent or
    # uses a provider-specific value.
    return progress >= 100

def seedr_task_name(task: dict[str, Any]) -> str:
    nested_torrent = task.get("torrent") if isinstance(task.get("torrent"), dict) else {}
    payload = task.get("torrent_payload") if isinstance(task.get("torrent_payload"), dict) else {}
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    nested_link = (
        task.get("torrent_magnet")
        or task.get("magnet")
        or task.get("magnet_url")
        or nested_torrent.get("torrent_magnet")
        or nested_torrent.get("magnet")
        or payload.get("torrent_magnet")
        or payload.get("magnet")
        or ""
    )
    magnet_name = magnet_display_name(nested_link)

    # A magnet's dn is the closest match to the title the user selected from
    # search. Prefer it before Seedr's own generated/provider title.
    return str(
        task.get("torrent_name")
        or magnet_name
        or nested_torrent.get("torrent_name")
        or payload.get("torrent_name")
        or task.get("torrent_title")
        or task.get("torrentTitle")
        or nested_torrent.get("title")
        or nested_torrent.get("name")
        or payload.get("title")
        or payload.get("name")
        or task.get("title")
        or task.get("name")
        or meta.get("torrent_name")
        or meta.get("title")
        or ""
    ).strip()


def normalize_magnet(magnet: str) -> str:
    """Normalize the BTIH while preserving the magnet's tracker and name parameters."""
    value = re.sub(r"[\r\n\t]+", "", str(magnet or "").strip())
    decoded = unquote(value)
    if decoded.lower().startswith("magnet:?"):
        value = decoded
    if not value.lower().startswith("magnet:?"):
        return value

    try:
        parsed = urlsplit(value)
        params = parse_qs(parsed.query, keep_blank_values=True)
        valid_hash = ""
        for raw in params.get("xt", []):
            raw = unquote(raw)
            match = re.fullmatch(r"urn:btih:([A-Za-z0-9]{32,40})", raw, re.I)
            if not match:
                continue
            candidate = match.group(1)
            if len(candidate) == 32:
                candidate = base64.b32decode(
                    candidate.upper() + "=" * ((8 - len(candidate) % 8) % 8)
                ).hex()
            if len(candidate) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
                valid_hash = candidate.lower()
                break

        if not valid_hash:
            return value

        # Keep the human-readable name and all supplied trackers. Some
        # metadata-only peers are reachable only through the trackers present
        # in the original magnet.
        rebuilt: list[tuple[str, str]] = [("xt", "urn:btih:" + valid_hash)]
        for key in ("dn", "tr"):
            for item in params.get(key, []):
                text_value = str(item or "").strip()
                if text_value:
                    rebuilt.append((key, text_value))

        return "magnet:?" + urlencode(rebuilt, doseq=True)
    except Exception:
        return value

def info_hash(magnet: str) -> str:
    for _ in range(3):
        m = re.search(r"(?:urn:btih:|btih:)([A-Za-z0-9]{32,40})", magnet, re.I)
        if m:
            h = m.group(1)
            if len(h) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", h):
                return h.lower()
            if len(h) == 32:
                try:
                    return base64.b32decode(h.upper() + "=" * ((8-len(h)%8)%8)).hex()
                except Exception:
                    pass
        magnet = unquote(magnet)
    return ""

def task_id(task: dict[str, Any]) -> str:
    return str(task.get("user_torrent_id") or task.get("id") or task.get("task_id") or "").strip()

async def rename_seedr_folder(folder_id: str, name: str) -> bool:
    # Folder rename is deliberately not part of the free-account download path.
    # Seedr can expose the generated folder name asynchronously.
    return False


async def add_task(magnet: str, folder_id: int = 0) -> dict[str, Any]:
    # Forward the exact magnet URI supplied by the caller.
    # Do not parse, rebuild, decode, lowercase, or add/remove query parameters.
    logger.info(
        "Seedr direct add: POST /tasks folder_id=%s magnet_length=%s",
        int(folder_id),
        len(magnet),
    )
    result = await seedr_request(
        "/tasks",
        method="POST",
        body={
            "torrent_magnet": magnet,
            "folder_id": int(folder_id),
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(502, "Seedr did not return a valid task response")
    return result


def normalize_file(item: Any, folder_id: str = "") -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"id": "", "name": "Unnamed file", "size": 0, "folderId": folder_id}
    return {
        "id": str(item.get("id") or item.get("file_id") or ""),
        # Seedr V2 presentation endpoints use the canonical file id.
        # Keep legacy folder_file_id only as a fallback for older API shapes.
        "streamId": str(
            item.get("id")
            or item.get("file_id")
            or item.get("folder_file_id")
            or item.get("folderFileId")
            or ""
        ),
        "name": str(item.get("name") or item.get("title") or "Unnamed file"),
        "size": int(float(item.get("size") or 0)),
        "folderId": str(item.get("folder_id") or item.get("folderId") or folder_id),
    }

async def task_contents(tid: str) -> list[dict[str, Any]]:
    payload = seedr_data(await seedr_request(f"/tasks/{quote(tid)}/contents"))
    if not isinstance(payload, dict):
        return []
    files = [normalize_file(x, str(payload.get("folder_created_id") or "")) for x in arr(payload, ("files", "items"))]
    folder = str(payload.get("folder_created_id") or "").strip()
    if folder and (not files or any(not x["id"] for x in files)):
        try:
            folder_payload = seedr_data(await seedr_request(f"/fs/folder/{quote(folder)}/contents"))
            folder_files = arr(folder_payload, ("files", "items"))
            if folder_files:
                files = [normalize_file(x, folder) for x in folder_files]
        except HTTPException:
            pass
    return files

async def folder_name(folder_id: str) -> str:
    for endpoint in (f"/fs/folder/{quote(folder_id)}", f"/fs/folder/{quote(folder_id)}/contents"):
        try:
            payload = seedr_data(await seedr_request(endpoint))
            if isinstance(payload, dict):
                for key in ("name", "title", "folder_name", "folderName", "path"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        return Path(value.rstrip("/")).name
                for key in ("folder", "directory"):
                    child = payload.get(key)
                    if isinstance(child, dict):
                        for name_key in ("name", "title", "folder_name", "folderName", "path"):
                            value = str(child.get(name_key) or "").strip()
                            if value:
                                return Path(value.rstrip("/")).name
        except HTTPException:
            continue
    return ""

SEEDR_FOLDER_CONCURRENCY = 8
_seedr_folder_semaphore = asyncio.Semaphore(SEEDR_FOLDER_CONCURRENCY)

# Library metadata is shared between the Files explorer and the Seedr Library
# so opening the Files tab does not trigger the same Seedr tree walk twice.
SEEDR_METADATA_CACHE_SECONDS = 5
SEEDR_FOLDER_CACHE_SECONDS = 15
_seedr_metadata_cache: tuple[float, dict[str, Any]] | None = None
_seedr_metadata_task: asyncio.Task | None = None
_seedr_folder_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_seedr_torrent_names: dict[str, str] = {}
_seedr_torrent_names_by_task: dict[str, str] = {}

SEEDR_AUTO_DELETE_SECONDS = 2 * 60 * 60
SEEDR_CLEANUP_FILE = Path("/app/.seedr_cleanup.json")
_seedr_cleanup_jobs: dict[str, dict[str, Any]] = {}
_seedr_cleanup_worker_task: asyncio.Task | None = None


def _load_seedr_cleanup_jobs() -> None:
    global _seedr_cleanup_jobs
    try:
        raw = SEEDR_CLEANUP_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            _seedr_cleanup_jobs = {
                str(k): v for k, v in data.items()
                if isinstance(v, dict) and float(v.get("deleteAt") or 0) > 0
            }
    except Exception:
        _seedr_cleanup_jobs = {}


def _save_seedr_cleanup_jobs() -> None:
    try:
        SEEDR_CLEANUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEDR_CLEANUP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_seedr_cleanup_jobs, separators=(",", ":")), encoding="utf-8")
        tmp.replace(SEEDR_CLEANUP_FILE)
    except Exception as exc:
        logger.info("Could not persist Seedr cleanup schedule: %s", exc)


def schedule_seedr_cleanup(
    task_id_value: str,
    torrent_name: str,
    folder_id: str = "",
    added_at: float | None = None,
) -> None:
    tid = str(task_id_value or "").strip()
    if not tid:
        return

    now = time.time()
    started = float(added_at or now)
    _seedr_cleanup_jobs[tid] = {
        "taskId": tid,
        "torrentName": str(torrent_name or "").strip() or f"Torrent {tid}",
        "folderId": str(folder_id or "").strip(),
        "addedAt": started,
        "deleteAt": started + SEEDR_AUTO_DELETE_SECONDS,
    }
    _save_seedr_cleanup_jobs()


async def _cleanup_seedr_job(tid: str, job: dict[str, Any]) -> bool:
    folder_id = str(job.get("folderId") or "").strip()
    torrent_name = str(job.get("torrentName") or f"Torrent {tid}").strip()

    # Seedr may expose the folder only after the task starts.
    if not folder_id:
        try:
            raw = seedr_data(await seedr_request(f"/tasks/{quote(tid)}"))
            task = unwrap_seedr_task(raw)
            folder_id = seedr_task_folder_id(task)
            if folder_id:
                job["folderId"] = folder_id
                _save_seedr_cleanup_jobs()
        except HTTPException as exc:
            if exc.status_code == 404:
                _seedr_cleanup_jobs.pop(tid, None)
                _save_seedr_cleanup_jobs()
                return True
            return False
        except Exception:
            return False

    if not folder_id or not folder_id.isdigit() or folder_id == "0":
        return False

    # Stop the Seedr task first so an active transfer cannot keep rebuilding
    # files while the folder is being removed.
    try:
        await seedr_request(f"/tasks/{quote(tid)}", "DELETE")
    except HTTPException as exc:
        if exc.status_code != 404:
            logger.info("Seedr task cleanup failed for %s: HTTP %s", tid, exc.status_code)
            return False
    except Exception as exc:
        logger.info("Seedr task cleanup failed for %s: %s", tid, exc)
        return False

    try:
        await seedr_folder_delete(folder_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            logger.info("Seedr folder cleanup failed for %s (%s): HTTP %s", torrent_name, folder_id, exc.status_code)
            return False
    except Exception as exc:
        logger.info("Seedr folder cleanup failed for %s (%s): %s", torrent_name, folder_id, exc)
        return False

    _seedr_cleanup_jobs.pop(tid, None)
    _save_seedr_cleanup_jobs()
    logger.info("Auto-deleted Seedr torrent after 2 hours: %s (task=%s folder=%s)", torrent_name, tid, folder_id)
    return True


async def _seedr_cleanup_worker() -> None:
    while True:
        try:
            now = time.time()
            due = [
                (tid, job)
                for tid, job in list(_seedr_cleanup_jobs.items())
                if float(job.get("deleteAt") or 0) <= now
            ]
            for tid, job in due:
                await _cleanup_seedr_job(tid, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Seedr cleanup worker error: %s", exc)
        await asyncio.sleep(30)



async def collect_folder(folder_id: str, path: str = "/", depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []

    async with _seedr_folder_semaphore:
        try:
            payload = seedr_data(await seedr_request(f"/fs/folder/{quote(folder_id)}/contents"))
        except HTTPException as exc:
            if exc.status_code == 404:
                return []
            raise

    if not isinstance(payload, dict):
        return []

    files: list[dict[str, Any]] = []
    for raw in arr(payload, ("files", "items")):
        item = normalize_file(raw, folder_id)
        item["folderPath"] = path
        item["url"] = None
        files.append(item)

    child_jobs: list[asyncio.Future] = []
    for raw in arr(payload, ("folders", "directories")):
        child_id = str(raw.get("id") or raw.get("folder_id") or "") if isinstance(raw, dict) else ""
        if not child_id:
            continue

        child_name = (
            str(raw.get("name") or raw.get("title") or child_id)
            if isinstance(raw, dict)
            else child_id
        )
        child_path = path.rstrip("/") + "/" + child_name
        child_jobs.append(collect_folder(child_id, child_path, depth + 1))

    if child_jobs:
        children = await asyncio.gather(*child_jobs, return_exceptions=True)
        for child in children:
            if isinstance(child, list):
                files.extend(child)

    return files

def _safe_download_filename(filename: str, fallback: str) -> str:
    value = str(filename or "").strip().replace("\r", "").replace("\n", "")
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:240] or fallback


async def _stream_seedr_download(url: str):
    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    request = client.build_request("GET", url, headers={"Accept": "*/*"})
    response = await client.send(request, stream=True)

    if response.status_code >= 400:
        try:
            detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
        finally:
            await response.aclose()
            await client.aclose()
        raise HTTPException(response.status_code, detail or "Seedr download request failed")

    content_type = response.headers.get("content-type") or "application/octet-stream"
    content_length = response.headers.get("content-length")
    content_range = response.headers.get("content-range")

    async def body():
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": "",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "no-store",
    }
    if content_length:
        headers["Content-Length"] = content_length
    if content_range:
        headers["Content-Range"] = content_range

    return body, content_type, headers


async def download_url(file_id: str) -> dict[str, str]:
    payload = seedr_data(await seedr_request(f"/download/file/{quote(file_id)}/url"))
    if isinstance(payload, dict):
        url = str(payload.get("url") or payload.get("download_url") or payload.get("downloadUrl") or payload.get("direct_url") or "")
        name = str(payload.get("name") or payload.get("filename") or "")
    else:
        url, name = str(payload or ""), ""
    if not url:
        raise HTTPException(502, "Seedr did not return a download URL")
    return {"url": url, "name": name}

def _srt_to_webvtt(text: str) -> str:
    """Convert a UTF-8/legacy SRT subtitle into browser-compatible WebVTT."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = normalized.split("\n")
    output = ["WEBVTT", ""]
    for line in lines:
        if "-->" in line:
            line = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", line)
        output.append(line)
    result = "\n".join(output)
    return result if result.endswith("\n") else result + "\n"


async def _seedr_subtitle_text(file_id: str, filename: str) -> tuple[str, str]:
    result = await download_url(file_id)
    url = result["url"]
    lower = (filename or result.get("name") or "").lower()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        raw = response.content

    if lower.endswith(".vtt"):
        text = raw.decode("utf-8-sig", errors="replace")
        if not text.lstrip().startswith("WEBVTT"):
            text = _srt_to_webvtt(text)
    elif lower.endswith(".srt"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        text = _srt_to_webvtt(text)
    else:
        raise HTTPException(415, "Only SRT and WebVTT subtitle files are supported")
    return text, "text/vtt; charset=utf-8"


@app.get("/api/seedr/files/{file_id}/subtitle")
async def seedr_file_subtitle(
    file_id: str,
    filename: str = Query(""),
):
    """Proxy a Seedr SRT/VTT file as WebVTT for the browser <track> element."""
    text, content_type = await _seedr_subtitle_text(file_id, filename)
    return Response(
        content=text,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


def _search_tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token]


def _media_search_parts(value: str) -> tuple[str, int | None, int | None]:
    """Split a media query into title text plus optional season/episode constraints."""
    q = re.sub(r"\s+", " ", value.strip())
    season_match = re.search(r"\b(?:season|series)\s*(\d{1,2})\b", q, re.I)
    episode_match = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", q, re.I)
    season = int(season_match.group(1)) if season_match else (
        int(episode_match.group(1)) if episode_match else None
    )
    episode = (
        int(season_match.group(1)) if False else None
    )
    if episode_match and episode_match.group(2):
        episode = int(episode_match.group(2))

    title = q
    title = re.sub(r"\b(?:season|series)\s*\d{1,2}\b", " ", title, flags=re.I)
    title = re.sub(r"\bS\d{1,2}(?:E\d{1,3})?\b", " ", title, flags=re.I)
    title = re.sub(
        r"\b(?:19|20)\d{2}\b|"
        r"\b(?:2160p|1440p|1080p|720p|480p|4k|8k)\b|"
        r"\b(?:webrip|web-dl|bluray|brrip|x264|x265|h264|h265|hevc|hdr)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip()
    return title, season, episode


def _season_episode_match(title: str, season: int | None, episode: int | None) -> bool:
    if season is None:
        return True

    upper = title.upper()
    match = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", upper)
    if match:
        if int(match.group(1)) != season:
            return False
        if episode is not None and (not match.group(2) or int(match.group(2)) != episode):
            return False
        return True

    season_word = re.search(r"\bSEASON[\s._-]*(\d{1,2})\b", upper)
    if season_word:
        if int(season_word.group(1)) != season:
            return False
        return episode is None

    return False


def _tvmaze_query(value: str) -> str:
    value = re.sub(r"\bS\d{1,2}(?:E\d{1,3})?.*$", "", value, flags=re.I)
    value = re.sub(r"\b(?:season|series)\s*\d+\b", "", value, flags=re.I)
    value = re.sub(r"\b(?:19|20)\d{2}\b", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_title(value: str) -> str:
    return " ".join(_search_tokens(value))


X1337_HOSTS = [
    "1337x.to",
    "1337x.st",
    "x1337x.ws",
    "x1337x.eu",
    "x1337x.cc",
]

# 1337x subcategory IDs represented by the icon at the left of each result.
# The icon describes the release type; it is not a quality filter.
X1337_MOVIE_SUBCATEGORIES = {"1","2","3","4","42","54","55","66","70","73","76"}
X1337_TV_SUBCATEGORIES = {"5","6","7","9","41","71","74","75"}
X1337_MEDIA_SUBCATEGORIES = X1337_MOVIE_SUBCATEGORIES | X1337_TV_SUBCATEGORIES


def _x1337_rows(html_text: str) -> list[dict[str, str]]:
    """Parse 1337x search rows across mirror HTML variations."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    rows: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for link in soup.find_all("a", href=re.compile(r"^/torrent/")):
        href = str(link.get("href") or "").strip()
        if not href or href in seen_paths:
            continue
        row = link.find_parent("tr")
        if row is None:
            continue
        title = link.get_text(" ", strip=True)
        if not title:
            continue
        cells = row.find_all("td")
        cell_text = [cell.get_text(" ", strip=True) for cell in cells]
        row_text = " ".join(cell_text) or row.get_text(" ", strip=True)
        size = ""
        seeders = "0"
        leechers = "0"
        for cell in cells:
            classes = " ".join(cell.get("class") or []).lower()
            text = cell.get_text(" ", strip=True)
            size_match = re.search(r"([\d.]+\s*[KMGT]i?B)", text, re.I)
            if not size and size_match:
                size = size_match.group(1)
            number = re.search(r"\d[\d,]*", text)
            if number and "seed" in classes:
                seeders = number.group(0).replace(",", "")
            elif number and ("leech" in classes or "leeches" in classes):
                leechers = number.group(0).replace(",", "")
        if not size:
            size_match = re.search(r"([\d.]+\s*[KMGT]i?B)", row_text, re.I)
            if size_match:
                size = size_match.group(1)
        numeric_cells = [re.sub(r"[^\d,]", "", text) for text in cell_text if re.fullmatch(r"\s*[\d,]+\s*", text or "")]
        if seeders == "0" and len(numeric_cells) >= 2:
            seeders = numeric_cells[-2] or "0"
        if leechers == "0" and len(numeric_cells) >= 1:
            leechers = numeric_cells[-1] or "0"
        seen_paths.add(href)
        sub_href = ""
        sub_id = ""
        if cells:
            sub_link = cells[0].find("a", href=re.compile(r"^/sub/"))
            if sub_link is not None:
                sub_href = str(sub_link.get("href") or "").strip()
                sub_match = re.search(r"^/sub/(\d+)/", sub_href)
                if sub_match:
                    sub_id = sub_match.group(1)

        rows.append({
            "title": title,
            "path": href,
            "size": size or "0 B",
            "seeders": seeders,
            "leechers": leechers,
            "subcategory_path": sub_href,
            "subcategory_id": sub_id,
        })
    return rows

async def search_1337x_direct(
    query: str,
    limit: int = 50,
    pages: int = 2,
    category: str | None = None,
    provider_query: str | None = None,
) -> list[dict[str, Any]]:
    """Primary 1337x search using media categories and cheap listing-page scraping.

    The unsorted category route is the reliable base path. The server-side
    seeders-sorted route is fetched as an optional second listing because 1337x
    integrations document that sorted keyword searches can fail under load.
    """
    q, season, episode = _media_search_parts(query)
    if not q:
        return []
    q = re.sub(r"\s+", " ", provider_query or _media_provider_query(query)).strip()
    if not q:
        return []

    encoded = quote(q, safe="").replace("%20", "+")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # Match Home's shared 100 MB–5 GB search window.
    minimum_size, maximum_size = 100 * 1024 * 1024, MAX_SEARCH_RESULT_SIZE_BYTES
    categories = (
        [category]
        if category in {"Movies", "TV"}
        else (["TV"] if season is not None or episode is not None else ["Movies", "TV"])
    )

    async with httpx.AsyncClient(
        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:

        async def find_category_page(category_name: str) -> tuple[str, str]:
            # Probe only the normal category-search route. One cheap route is
            # enough to identify a usable mirror; do not serially probe both
            # normal and sorted routes across every mirror.
            path = f"/category-search/{encoded}/{category_name}/1/"
            for host in X1337_HOSTS:
                try:
                    response = await client.get(f"https://{host}{path}")
                    if response.status_code < 400 and _x1337_rows(response.text):
                        return f"https://{host}", response.text
                except httpx.HTTPError:
                    continue
            return "", ""

        category_pages = await asyncio.gather(
            *(find_category_page(name) for name in categories),
            return_exceptions=False,
        )

        # Generic search is also queried because language searches can surface
        # Hindi/dual-audio releases there that are not near the top of a
        # category search. Every generic row is classified through /sub/<id>/
        # so non-media categories cannot leak into the app.
        generic_base = next((base for base, _ in category_pages if base), "")
        generic_pages: list[str] = []
        if generic_base:
            generic_urls = [f"{generic_base}/search/{encoded}/1/"]
            if pages > 1:
                generic_urls.append(f"{generic_base}/search/{encoded}/2/")

            async def fetch_generic(url: str) -> str:
                try:
                    response = await client.get(url)
                    if response.status_code < 400 and _x1337_rows(response.text):
                        return response.text
                except httpx.HTTPError:
                    pass
                return ""

            generic_pages = [
                value for value in await asyncio.gather(
                    *(fetch_generic(url) for url in generic_urls),
                    return_exceptions=False,
                )
                if value
            ]

        async def fetch_category(
            category_name: str,
            base: str,
            first_page: str,
        ) -> list[dict[str, str]]:
            if not base or not first_page:
                return []

            # Normal page 1 + sorted page 1 are fetched together. Sorted is
            # treated as enrichment, never as a requirement.
            paths = [
                ("normal", first_page),
                (
                    "sorted",
                    f"{base}/sort-category-search/{encoded}/{category_name}/seeders/desc/1/",
                ),
            ]
            if pages > 1:
                paths.append((
                    "normal2",
                    f"{base}/category-search/{encoded}/{category_name}/2/",
                ))

            async def fetch_one(kind: str, value: str):
                if kind == "normal":
                    return value
                try:
                    response = await client.get(value)
                    if response.status_code < 400 and _x1337_rows(response.text):
                        return response.text
                except httpx.HTTPError:
                    pass
                return ""

            fetched = await asyncio.gather(
                *(fetch_one(kind, value) for kind, value in paths),
                return_exceptions=False,
            )
            pages_html = [text for text in fetched if isinstance(text, str) and text]
            return pages_html

        fetched_by_category = await asyncio.gather(
            *(
                fetch_category(name, base, first_page)
                for name, (base, first_page) in zip(categories, category_pages)
            ),
            return_exceptions=False,
        )

        kind_hint = "tv" if (season is not None or episode is not None) else "both"

        async def parse_candidates(
            category_name: str,
            html_pages: list[str],
            generic_search: bool = False,
        ) -> list[dict[str, str]]:
            target = _media_provider_query(query)
            tokens = _search_tokens(target)
            compact_query = "".join(tokens)
            candidates, seen_paths = [], set()

            for html_text in html_pages:
                for row in _x1337_rows(html_text):
                    path = row.get("path") or ""
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)

                    normalized = _normalize_title(row["title"])
                    compact_normalized = normalized.replace(" ", "")
                    if tokens and not (
                        all(token in normalized for token in tokens)
                        or (compact_query and compact_query in compact_normalized)
                    ):
                        continue
                    if not _season_episode_match(row["title"], season, episode):
                        continue

                    if generic_search:
                        sub_id = str(row.get("subcategory_id") or "")
                        if sub_id not in X1337_MEDIA_SUBCATEGORIES:
                            continue
                        if kind_hint == "tv" and sub_id not in X1337_TV_SUBCATEGORIES:
                            continue

                    size_match = re.match(
                        r"([\d.]+)\s*([KMGT]i?B)",
                        row.get("size", ""),
                        re.IGNORECASE,
                    )
                    if not size_match:
                        continue
                    units = {
                        "KB": 1024, "KIB": 1024,
                        "MB": 1024**2, "MIB": 1024**2,
                        "GB": 1024**3, "GIB": 1024**3,
                        "TB": 1024**4, "TIB": 1024**4,
                    }
                    size = int(
                        float(size_match.group(1))
                        * units[size_match.group(2).upper()]
                    )
                    if not minimum_size <= size <= maximum_size:
                        continue

                    row["size_bytes"] = str(size)
                    row["media_category"] = category_name
                    candidates.append(row)

            candidates.sort(
                key=lambda row: (
                    _title_relevance(row["title"], query)[0],
                    int(row.get("seeders") or 0),
                    int(row.get("leechers") or 0),
                ),
                reverse=True,
            )
            return candidates

        candidate_lists = await asyncio.gather(
            *(
                parse_candidates(name, html_pages)
                for name, html_pages in zip(categories, fetched_by_category)
            ),
            return_exceptions=False,
        )

        candidates = [
            row
            for values in candidate_lists
            for row in values
        ]

        if generic_pages:
            candidates.extend(
                await parse_candidates(
                    "TV" if kind_hint == "tv" else "Movies",
                    generic_pages,
                    generic_search=True,
                )
            )

        unique: dict[str, dict[str, str]] = {}
        for row in candidates:
            key = row.get("path") or _normalize_title(row.get("title", ""))
            unique.setdefault(key, row)

        candidates = list(unique.values())
        candidates.sort(
            key=lambda row: (
                _title_relevance(row["title"], query)[0],
                int(row.get("seeders") or 0),
                int(row.get("leechers") or 0),
            ),
            reverse=True,
        )
        # Detail pages are the expensive part. Keep lookup fan-out bounded
        # on Render Free while letting DVD/HD/HEVC/dual-audio/h.264 and other
        # valid 1337x release types compete.
        candidates = candidates[:min(max(limit, 1), 20)]

        async def fetch_detail(row):
            # Keep useful listing results even when the provider's detail page
            # is slow or blocks server-side requests. Magnets are resolved on
            # demand by /api/search/resolve-magnet when a user clicks Prepare.
            detail_url = base_for_row(row) + row["path"]
            return {
                "guid": "1337x-" + hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:20],
                "title": row["title"],
                "size": int(row["size_bytes"]),
                "seeders": int(row.get("seeders") or 0),
                "leechers": int(row.get("leechers") or 0),
                "indexer": "1337x",
                "protocol": "torrent",
                "publishDate": "",
                "magnetUrl": None,
                "infoHash": "",
                "downloadUrl": None,
                "infoUrl": detail_url,
                "sourceUrl": detail_url,
                "descriptorUrl": "",
                "category": row.get("media_category") or "Video",
            }

        # Preserve the base URL selected for each category without doing a
        # second mirror probe before opening detail pages.
        row_bases: dict[str, str] = {}
        for category_name, (base, _) in zip(categories, category_pages):
            if base:
                for row in candidate_lists[categories.index(category_name)]:
                    row_bases[row["path"]] = base

        def base_for_row(row: dict[str, str]) -> str:
            return row_bases.get(row["path"], category_pages[0][0] if category_pages else "")

        fetched = await asyncio.gather(
            *(fetch_detail(row) for row in candidates),
            return_exceptions=True,
        )
    return [item for item in fetched if isinstance(item, dict)]

async def search_yts_movies(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Search YTS directly so movie searches are not lost in aggregate ranking."""
    movie_query = _media_provider_query(query)
    if not movie_query:
        return []

    payload = None
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for host in YTS_API_HOSTS:
            try:
                response = await client.get(
                    f"https://{host}/api/v2/list_movies.json",
                    params={"query_term": movie_query, "limit": "50"},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
                    break
            except (httpx.HTTPError, ValueError):
                continue

    if not isinstance(payload, dict):
        return []

    target_tokens = _search_tokens(movie_query)
    results: list[dict[str, Any]] = []
    for movie in (payload.get("data") or {}).get("movies") or []:
        if not isinstance(movie, dict):
            continue
        title = str(movie.get("title_long") or movie.get("title") or "").strip()
        if not title:
            continue
        title_tokens = _normalize_title(title)
        if target_tokens and not all(token in title_tokens for token in target_tokens):
            continue

        released = movie.get("date_uploaded_unix")
        try:
            from datetime import datetime, timezone
            published = datetime.fromtimestamp(
                int(released), tz=timezone.utc
            ).isoformat() if released else ""
        except Exception:
            published = ""

        for torrent in movie.get("torrents") or []:
            if not isinstance(torrent, dict):
                continue
            h = str(torrent.get("hash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            quality = str(torrent.get("quality") or "").strip()
            kind = str(torrent.get("type") or "").strip()
            suffix = " ".join(x for x in (quality, kind) if x)
            display_title = f"{title} [{suffix}]" if suffix else title
            magnet = (
                f"magnet:?xt=urn:btih:{h}&dn={quote(display_title, safe='')}"
            )
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet += "&tr=" + quote(tracker, safe="")

            results.append({
                "guid": f"yts-{h}",
                "title": display_title,
                "mediaTitle": title,
                "year": int(movie.get("year") or 0) or None,
                "rating": float(movie.get("rating") or 0) or None,
                "genres": [str(g).strip() for g in (movie.get("genres") or []) if str(g).strip()],
                "posterUrl": f"/api/poster?title={quote(title, safe='')}&year={quote(str(movie.get('year') or ''), safe='')}",
                "quality": suffix,
                "size": int(float(torrent.get("size_bytes") or 0)),
                "seeders": int(torrent.get("seeds") or 0),
                "leechers": int(torrent.get("peers") or 0),
                "indexer": "yts.mx",
                "protocol": "torrent",
                "publishDate": published,
                "magnetUrl": magnet,
                "infoHash": h,
                "downloadUrl": magnet,
                "infoUrl": "",
                "sourceUrl": "",
            })

    results.sort(
        key=lambda row: (row["seeders"] + row["leechers"], row["publishDate"]),
        reverse=True,
    )
    return results[:limit]


async def search_tv_eztv(query: str, limit: int = 30) -> list[dict[str, Any]]:
    tv_query, _season, _episode = _media_search_parts(query)
    tv_query = _tvmaze_query(tv_query)
    if not tv_query:
        return []

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                "https://api.tvmaze.com/search/shows",
                params={"q": tv_query},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            matches = response.json()
    except (httpx.HTTPError, ValueError):
        return []

    if not isinstance(matches, list):
        return []

    target = _normalize_title(tv_query)
    imdb_id = ""
    for match in matches[:10]:
        show = match.get("show") if isinstance(match, dict) else None
        if not isinstance(show, dict):
            continue
        name = str(show.get("name") or "").strip()
        external = show.get("externals")
        candidate = str(external.get("imdb") or "").strip() if isinstance(external, dict) else ""
        if candidate and _normalize_title(name) == target:
            imdb_id = candidate
            break

    if not imdb_id:
        return []

    payload: dict[str, Any] | None = None
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for host in ("eztv.yt", "eztvx.to"):
            try:
                response = await client.get(
                    f"https://{host}/api/get-torrents",
                    params={"imdb_id": imdb_id, "limit": "100", "page": "1"},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
                    break
            except (httpx.HTTPError, ValueError):
                continue

    if not payload:
        return []

    results: list[dict[str, Any]] = []
    for item in payload.get("torrents") or []:
        if not isinstance(item, dict):
            continue
        h = str(item.get("hash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", h):
            continue
        title = str(item.get("filename") or item.get("title") or "").strip()
        if not title:
            continue
        magnet = str(item.get("magnet_url") or "").strip()
        if not magnet:
            magnet = f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}"
        try:
            from datetime import datetime, timezone
            published = datetime.fromtimestamp(
                int(item.get("date_released_unix") or 0), tz=timezone.utc
            ).isoformat() if item.get("date_released_unix") else ""
        except Exception:
            published = ""
        results.append({
            "guid": f"eztv-{h}",
            "title": title,
            "size": int(float(item.get("size_bytes") or 0)),
            "seeders": int(item.get("seeds") or 0),
            "leechers": int(item.get("peers") or 0),
            "indexer": "eztv.yt",
            "protocol": "torrent",
            "publishDate": published,
            "magnetUrl": magnet,
            "infoHash": h,
            "downloadUrl": magnet,
            "infoUrl": "",
            "sourceUrl": "",
        })
    _, season, episode = _media_search_parts(query)
    if season is not None:
        results = [
            row for row in results
            if _season_episode_match(row["title"], season, episode)
        ]

    results.sort(
        key=lambda row: (row["seeders"] + row["leechers"], row["publishDate"]),
        reverse=True,
    )
    return results[:limit]



LIMETORRENTS_HOSTS = tuple(
    host.strip()
    for host in os.getenv(
        "LIMETORRENTS_HOSTS",
        "www.limetorrents.fun,www.limetorrents.lol,limetorrents.fun,limetorrents.lol",
    ).split(",")
    if host.strip()
)


def _limetorrents_rows(html_text: str, base_url: str) -> list[dict[str, str]]:
    """Parse LimeTorrents rows using the current table structure."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    # LimeTorrents result rows may be direct children of table.table2
    # rather than tbody children, and may not carry a bgcolor attribute.
    # Avoid relying on either HTML detail.
    for row in soup.select("table.table2 tr"):
        # Prefer the visible title link over the empty download-icon link.
        title_anchor = row.select_one(".tt-name a[href*='-torrent-']")
        if title_anchor is None:
            title_anchor = next(
                (
                    anchor
                    for anchor in row.select(".tt-name a[href]")
                    if anchor.get_text(" ", strip=True)
                    and str(anchor.get("href") or "").lower().endswith(".html")
                ),
                None,
            )
        if title_anchor is None:
            continue

        title = title_anchor.get_text(" ", strip=True)
        detail_href = str(title_anchor.get("href") or "").strip()
        if not title or not detail_href:
            continue

        # LimeTorrents exposes the BTIH hash on the small download-icon
        # link in the listing row. Prefer this hash so Prepare can submit a
        # real magnet to Seedr without relying only on a detail-page scrape.
        download_anchor = (
            row.select_one(".tt-name a.csprite_dl14[href]")
            or row.select_one(".tt-name a[href*='/download/']")
        )
        download_href = str(download_anchor.get("href") or "").strip() if download_anchor else ""
        hash_match = re.search(
            r"(?i)(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
            download_href,
        )
        torrent_hash = hash_match.group(1).lower() if hash_match else ""
        magnet_url = ""
        if torrent_hash:
            magnet_url = f"magnet:?xt=urn:btih:{torrent_hash}&dn={quote(title, safe='')}"
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet_url += "&tr=" + quote(tracker, safe="")

        seed_cell = row.select_one(".tdseed")
        leech_cell = row.select_one(".tdleech")
        seeders = "0"
        leechers = "0"
        if seed_cell is not None:
            match = re.search(r"\d[\d,]*", seed_cell.get_text(" ", strip=True))
            if match:
                seeders = match.group(0).replace(",", "")
        if leech_cell is not None:
            match = re.search(r"\d[\d,]*", leech_cell.get_text(" ", strip=True))
            if match:
                leechers = match.group(0).replace(",", "")

        cells = row.find_all("td")
        size = ""
        if len(cells) >= 3:
            match = re.search(r"([\d.]+\s*[KMGT]i?B)", cells[2].get_text(" ", strip=True), re.I)
            if match:
                size = match.group(1)
        if not size:
            match = re.search(r"([\d.]+\s*[KMGT]i?B)", row.get_text(" ", strip=True), re.I)
            if match:
                size = match.group(1)

        source_url = urljoin(base_url.rstrip("/") + "/", detail_href.lstrip("/"))
        key = source_url.lower()
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "title": title,
            "detail_url": source_url,
            "size": size,
            "seeders": seeders,
            "leechers": leechers,
            "info_hash": torrent_hash,
            "magnet_url": magnet_url,
        })

    return rows


async def search_limetorrents(
    query: str,
    limit: int = 50,
    pages: int = 2,
) -> list[dict[str, Any]]:
    """Search LimeTorrents concurrently across mirrors; resolve magnets on demand."""
    _title_query, season, episode = _media_search_parts(query)
    provider_query = _media_provider_query(query)

    # Keep language terms in the provider query so "thor hindi" searches the
    # Hindi result pool directly instead of searching only for "thor".
    _, languages = _search_query_constraints(query)
    language_terms = [
        language
        for language in languages
        if language in {
            "hindi", "tamil", "telugu", "malayalam", "kannada",
            "bengali", "marathi", "punjabi", "gujarati", "urdu",
        }
    ]
    provider_query = re.sub(
        r"\s+",
        " ",
        f"{provider_query} {' '.join(language_terms)}".strip(),
    )
    if not provider_query:
        return []

    category = "tv" if season is not None or episode is not None else "movies"
    encoded = quote(provider_query, safe="")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(
        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:
        async def fetch_host(host: str) -> list[tuple[str, str]]:
            base = f"https://{host}"
            urls = [f"{base}/search/all/{encoded}/seeds/1/"]
            if pages > 1:
                urls.append(f"{base}/search/all/{encoded}/seeds/2/")

            async def fetch_page(url: str) -> str:
                try:
                    response = await client.get(url)
                    if response.status_code < 400 and response.text:
                        return response.text
                except httpx.HTTPError:
                    pass
                return ""

            bodies = await asyncio.gather(
                *(fetch_page(url) for url in urls),
                return_exceptions=False,
            )
            return [(base, body) for body in bodies if body]

        # Probe all mirrors together. Previously a dead first mirror could
        # consume the timeout sequentially before a healthy mirror was tried.
        mirror_pages = await asyncio.gather(
            *(fetch_host(host) for host in LIMETORRENTS_HOSTS),
            return_exceptions=True,
        )
        pages_html: list[tuple[str, str]] = []
        for value in mirror_pages:
            if isinstance(value, list):
                pages_html.extend(value)

        raw_rows: list[dict[str, str]] = []
        for base, html_text in pages_html:
            raw_rows.extend(_limetorrents_rows(html_text, base))

        if not raw_rows:
            logger.warning(
                "LimeTorrents unavailable for '%s' (hosts=%s)",
                query,
                ",".join(LIMETORRENTS_HOSTS),
            )
            return []

        units = {
            "KB": 1024, "KIB": 1024,
            "MB": 1024**2, "MIB": 1024**2,
            "GB": 1024**3, "GIB": 1024**3,
            "TB": 1024**4, "TIB": 1024**4,
        }

        # Merge repeated rows from mirror domains, retaining the highest
        # reported seed/peer counts for each title and size.
        unique_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in raw_rows:
            size_match = re.match(r"([\d.]+)\s*([KMGT]i?B)", row.get("size", ""), re.I)
            if not size_match:
                continue
            size = int(float(size_match.group(1)) * units[size_match.group(2).upper()])
            key = (_normalize_title(str(row.get("title") or "")), str(size))
            candidate = {**row, "size_bytes": size}
            previous = unique_rows.get(key)
            if previous is None or (
                int(candidate.get("seeders") or 0), int(candidate.get("leechers") or 0)
            ) > (
                int(previous.get("seeders") or 0), int(previous.get("leechers") or 0)
            ):
                unique_rows[key] = candidate

        parsed_rows = list(unique_rows.values())
        parsed_rows.sort(
            key=lambda row: (
                int(row.get("seeders") or 0),
                int(row.get("leechers") or 0),
            ),
            reverse=True,
        )

        results: list[dict[str, Any]] = []
        for row in parsed_rows[:min(max(limit, 1), 30)]:
            detail_url = str(row.get("detail_url") or "").strip()
            if not detail_url:
                continue
            results.append({
                "guid": "limetorrents-" + hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:20],
                "title": str(row.get("title") or ""),
                "size": int(row.get("size_bytes") or 0),
                "seeders": int(row.get("seeders") or 0),
                "leechers": int(row.get("leechers") or 0),
                "indexer": "LimeTorrents",
                "protocol": "torrent",
                "publishDate": "",
                "magnetUrl": str(row.get("magnet_url") or "").strip() or None,
                "infoHash": str(row.get("info_hash") or "").strip(),
                "downloadUrl": str(row.get("magnet_url") or "").strip() or None,
                "infoUrl": detail_url,
                "sourceUrl": detail_url,
                "descriptorUrl": "",
                "category": "TV" if category == "tv" else "Movies",
            })

    logger.warning(
        "LimeTorrents listing '%s': %d results; top seeders=%s",
        query,
        len(results),
        ",".join(str(item.get("seeders") or 0) for item in results[:5]),
    )
    return results


async def search_knaben(
    query: str,
    limit: int = 100,
    provider_query: str | None = None,
) -> list[dict[str, Any]]:
    """Search Knaben with a title-only provider query and strict local filtering."""
    title_query, season, episode = _media_search_parts(query)
    title_query = re.sub(r"\s+", " ", provider_query or _media_provider_query(query)).strip()
    if not title_query:
        return []

    # Search the provider by the actual title only. Qualifiers such as year
    # and language are applied locally so "Spider-Man 2026" does not get
    # reduced to an unqualified search that can return old movies.
    target_tokens = _search_tokens(title_query)
    # Keep the full Knaben candidate pool. The previous working Vercel
    # implementation requested 300 before applying local filtering.
    request_size = 500

    body = {
        "search_type": "100%",
        "search_field": "title",
        "query": title_query,
        "order_by": "seeders",
        "order_direction": "desc",
        "from": 0,
        "size": request_size,
        "hide_unsafe": True,
        "hide_xxx": True,
        "seconds_since_last_seen": 604800,
    }

    try:
        async with httpx.AsyncClient(timeout=SEARCH_SOURCE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                KNABEN_API_URL,
                json=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
            logger.info(
                "Knaben HTTP %s for '%s': %d hits",
                response.status_code,
                query,
                len(payload.get("hits", [])) if isinstance(payload, dict) and isinstance(payload.get("hits"), list) else 0,
            )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Knaben search failed for '%s': %s", query, exc)
        return []

    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        return []

    results: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue

        title = str(hit.get("title") or "").strip()
        category = str(hit.get("category") or "").strip()
        if not title:
            continue

        normalized_title = _normalize_title(title)
        if target_tokens and not all(token in normalized_title for token in target_tokens):
            continue
        if category and any(
            blocked in category.lower()
            for blocked in ("anime", "games", "music", "software", "books", "porn", "xxx", "adult")
        ):
            continue
        if category and not any(
            allowed in category.lower()
            for allowed in ("video", "movie", "tv", "television", "series")
        ):
            continue
        if season is not None and not _season_episode_match(title, season, episode):
            continue

        magnet = str(hit.get("magnetUrl") or "").strip()
        h = str(hit.get("hash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", h):
            h = info_hash(magnet)

        results.append({
            "guid": f"knaben-{h or hit.get('id') or title}",
            "title": title,
            "size": int(hit.get("bytes") or 0),
            "seeders": int(hit.get("seeders") or 0),
            "leechers": int(hit.get("peers") or 0),
            "indexer": str(hit.get("cachedOrigin") or hit.get("tracker") or "Knaben").strip(),
            "protocol": "torrent",
            "publishDate": str(hit.get("date") or ""),
            "magnetUrl": magnet or None,
            "infoHash": h if re.fullmatch(r"[0-9a-f]{40}", h, re.I) else "",
            "downloadUrl": magnet or None,
            "infoUrl": str(hit.get("details") or ""),
            "sourceUrl": str(hit.get("details") or ""),
            "descriptorUrl": str(hit.get("link") or ""),
            "category": category,
        })

    target_text = _normalize_title(title_query)
    results.sort(
        key=lambda row: (
            1 if _normalize_title(str(row["title"])).startswith(target_text) else 0,
            int(row.get("seeders") or 0),
        ),
        reverse=True,
    )
    logger.info("Knaben search '%s': %d relevant results", query, len(results))
    return results[:limit]



async def search_torrents_csv(query: str, limit: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=SEARCH_SOURCE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(
                "https://torrents-csv.com/service/search",
                params={
                    "q": _media_provider_query(query),
                    "size": min(limit, 50),
                    "type": "torrent",
                },
                headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
            )
            response.raise_for_status()
            payload = response.json()

        rows = payload if isinstance(payload, list) else (
            payload.get("torrents", []) if isinstance(payload, dict) else []
        )
        results: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            h = str(item.get("infohash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            title = str(item.get("name") or "Untitled").strip()
            if not title:
                continue
            results.append({
                "guid": f"torrents-csv-{h}",
                "title": title,
                "size": int(float(item.get("size_bytes") or 0)),
                "seeders": int(item.get("seeders") or 0),
                "leechers": int(item.get("leechers") or 0),
                "indexer": "torrents-csv",
                "protocol": "torrent",
                "publishDate": (
                    datetime.fromtimestamp(
                        int(item.get("created_unix") or 0), tz=timezone.utc
                    ).isoformat()
                    if item.get("created_unix") else ""
                ),
                "infoHash": h,
                "magnetUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "downloadUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "infoUrl": "",
                "sourceUrl": "",
                "descriptorUrl": "",
            })
        return {"source": "torrents-csv", "elapsedMs": round((time.monotonic() - started) * 1000), "results": results}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Torrents-CSV search failed for '%s': %s", query, exc)
        return {"source": "torrents-csv", "elapsedMs": round((time.monotonic() - started) * 1000), "results": [], "error": str(exc)}


async def search_apibay(query: str, limit: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=SEARCH_SOURCE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(
                "https://apibay.org/q.php",
                params={"q": _media_provider_query(query), "cat": "0"},
                headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
            )
            response.raise_for_status()
            payload = response.json()

        rows = payload if isinstance(payload, list) else []
        results: list[dict[str, Any]] = []
        for item in rows[:limit]:
            if not isinstance(item, dict):
                continue
            h = str(item.get("info_hash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            title = str(item.get("name") or "Untitled").strip()
            if not title:
                continue
            results.append({
                "guid": f"apibay-{h}",
                "title": title,
                "size": int(float(item.get("size") or 0)),
                "seeders": int(item.get("seeders") or 0),
                "leechers": int(item.get("leechers") or 0),
                "indexer": "apibay",
                "protocol": "torrent",
                "publishDate": "",
                "infoHash": h,
                "magnetUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "downloadUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "infoUrl": "",
                "sourceUrl": "",
                "descriptorUrl": "",
            })
        return {"source": "apibay", "elapsedMs": round((time.monotonic() - started) * 1000), "results": results}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("APIBay search failed for '%s': %s", query, exc)
        return {"source": "apibay", "elapsedMs": round((time.monotonic() - started) * 1000), "results": [], "error": str(exc)}


def _search_media_kind(query: str) -> str:
    """Classify only enough to select strict media sources; never opens generic categories."""
    q = query.strip()
    if re.search(r"\b(?:S\d{1,2}(?:E\d{1,3})?|season\s*\d+|episode\s*\d+)\b", q, re.I):
        return "tv"
    return "both"


def _expand_compound_search_title(value: str) -> str:
    title = str(value or "")
    for compact, canonical in SEARCH_COMPOUND_ALIASES.items():
        title = re.sub(
            rf"(?<![a-z]){re.escape(compact)}(?![a-z])",
            canonical,
            title,
            flags=re.I,
        )
    return title


def _media_provider_query(value: str) -> str:
    """Return the title portion used for provider searches, excluding qualifiers."""
    title, _season, _episode = _media_search_parts(value)
    _year, languages = _search_query_constraints(value)
    for language in sorted(languages, key=len, reverse=True):
        title = re.sub(
            rf"(?<![a-z]){re.escape(language)}(?![a-z])",
            " ",
            title,
            flags=re.I,
        )
    title = _expand_compound_search_title(title)
    title = re.sub(r"\bspider[- ]?man\b", "spider man", title, flags=re.I)
    title = re.sub(r"\bant[- ]?man\b", "ant man", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip()


def _search_query_constraints(value: str) -> tuple[int | None, list[str]]:
    """Extract constraints that should affect matching but not title search."""
    years = re.findall(r"\b((?:19|20)\d{2})\b", value)
    year = int(years[-1]) if years else None

    language_aliases = (
        "hindi", "tamil", "telugu", "malayalam", "kannada",
        "bengali", "marathi", "punjabi", "gujarati", "urdu",
        "dual audio", "multi audio", "dubbed", "dub",
    )
    lower = value.lower()
    languages = [term for term in language_aliases if re.search(
        rf"(?<![a-z]){re.escape(term)}(?![a-z])", lower
    )]
    return year, languages


def _constraint_matches_title(title: str, query: str) -> bool:
    """Match year/language qualifiers without making normal title search brittle."""
    year, languages = _search_query_constraints(query)
    normalized = _normalize_title(title)
    lower = title.lower()

    if year is not None:
        title_years = {int(value) for value in re.findall(r"\b((?:19|20)\d{2})\b", title)}
        if year not in title_years:
            return False

    language_aliases = {
        "hindi": (r"(?<![a-z])hindi(?![a-z])", r"(?<![a-z])hin(?![a-z])"),
        "tamil": (r"(?<![a-z])tamil(?![a-z])", r"(?<![a-z])tam(?![a-z])"),
        "telugu": (r"(?<![a-z])telugu(?![a-z])", r"(?<![a-z])tel(?![a-z])"),
        "malayalam": (r"(?<![a-z])malayalam(?![a-z])", r"(?<![a-z])mal(?![a-z])"),
        "kannada": (r"(?<![a-z])kannada(?![a-z])", r"(?<![a-z])kan(?![a-z])"),
        "bengali": (r"(?<![a-z])bengali(?![a-z])", r"(?<![a-z])ben(?![a-z])"),
        "marathi": (r"(?<![a-z])marathi(?![a-z])", r"(?<![a-z])mar(?![a-z])"),
        "punjabi": (r"(?<![a-z])punjabi(?![a-z])", r"(?<![a-z])pun(?![a-z])"),
        "gujarati": (r"(?<![a-z])gujarati(?![a-z])", r"(?<![a-z])guj(?![a-z])"),
        "urdu": (r"(?<![a-z])urdu(?![a-z])", r"(?<![a-z])urd(?![a-z])"),
    }

    for language in languages:
        patterns = language_aliases.get(language)
        if patterns:
            if not any(re.search(pattern, lower, re.I) for pattern in patterns):
                return False
        elif language not in lower and language.replace(" ", "") not in normalized.replace(" ", ""):
            return False

    return True


def _search_quality_filter(item: dict[str, Any], query: str) -> bool:
    """Hard media/size gate plus title and qualifier relevance."""
    title = str(item.get("title") or "").strip()
    if not title:
        return False

    normalized = _normalize_title(title)
    normalized_tokens = set(normalized.split())
    _query_title, season, episode = _media_search_parts(query)
    # Language/codec/year qualifiers are validated separately below. The title
    # matcher should use only the actual media title, otherwise a release using
    # "Hin"/"Dub" instead of the exact query word "Hindi" gets discarded even
    # though the provider found it correctly.
    provider_title = _media_provider_query(query)
    target_tokens = _search_tokens(provider_title)

    # Match whole normalized tokens, not arbitrary substrings. This keeps
    # "thor" from matching "Thoroughbreds" while allowing punctuation variants
    # such as "Spider-Man" / "Spider Man" after title normalization.
    if target_tokens and not all(token in normalized_tokens for token in target_tokens):
        return False

    if not _constraint_matches_title(title, query):
        return False

    if season is not None and not _season_episode_match(title, season, episode):
        return False

    category = str(item.get("category") or "").lower()
    blocked = (
        "game", "software", "application", "music", "audio",
        "book", "ebook", "porn", "xxx", "adult", "anime",
    )
    if any(word in category for word in blocked):
        return False

    size = int(item.get("size") or 0)
    return 100 * 1024 * 1024 <= size <= MAX_SEARCH_RESULT_SIZE_BYTES


def _title_relevance(title: str, query: str) -> tuple[int, int]:
    """Prefer titles that begin with the searched title, ignoring leading articles."""
    normalized = _normalize_title(title)
    target = _normalize_title(_media_search_parts(query)[0])
    if not target:
        return (0, 0)

    # Indexers often add "The" before a movie title. Do not demote an otherwise
    # direct match just because the article is present.
    candidate = re.sub(r"^(?:the|a|an)\s+", "", normalized)
    if candidate == target:
        return (4, len(target))
    if candidate.startswith(target + " "):
        return (3, len(target))

    candidate_tokens = candidate.split()
    target_tokens = target.split()
    if not target_tokens:
        return (0, 0)

    # Exact consecutive title tokens anywhere in the name are better than
    # tokens that only happen to appear in a longer related title (e.g. LEGO).
    for index in range(max(0, len(candidate_tokens) - len(target_tokens) + 1)):
        if candidate_tokens[index:index + len(target_tokens)] == target_tokens:
            return (2, len(target))
    if all(token in candidate_tokens for token in target_tokens):
        return (1, len(target))
    return (0, 0)


def _release_quality_score(title: str) -> int:
    """Estimate release quality from common filename tags; not a safety/trust score."""
    lower = unescape(str(title or "")).lower()
    score = 0

    # Resolution is a strong, broadly available signal.
    if re.search(r"\b(?:2160p|4k|uhd)\b", lower):
        score += 8
    elif re.search(r"\b1080p\b", lower):
        score += 6
    elif re.search(r"\b720p\b", lower):
        score += 4
    elif re.search(r"\b480p\b", lower):
        score += 2

    # Prefer modern digital/Blu-ray sources over older disc rips.
    if re.search(r"\b(?:blu[ .-]?ray|bdrip|brrip|remux)\b", lower):
        score += 4
    elif re.search(r"\b(?:web[ .-]?dl|webdl|webrip)\b", lower):
        score += 3
    elif re.search(r"\bhdtv\b", lower):
        score += 2
    elif re.search(r"\b(?:dvdrip|dvd)\b", lower):
        score += 1

    if re.search(r"\b(?:x265|h265|hevc|av1)\b", lower):
        score += 1
    if re.search(r"\b(?:x264|h264)\b", lower):
        score += 1
    if re.search(r"\b(?:hdr10?|dolby[ .]?vision|10bit)\b", lower):
        score += 1

    # Strongly demote low-quality theatrical captures and screeners without
    # removing them, so users can still find smaller/older releases if needed.
    if re.search(r"\b(?:hdcam|camrip|cam|telesync|telecine|ts|dvdscr|screener)\b", lower):
        score -= 8
    elif re.search(r"\br5\b", lower):
        score -= 2

    return score


def _media_provider_queries(value: str) -> list[str]:
    """Build a tiny title-variant set without exploding provider traffic.

    The first query is canonicalized (e.g. Spider-Man -> Spider Man). A second
    spelling is used only when the fast path returns too few usable results.
    For long titles, a third short anchor can recover indexers that struggle
    with punctuation while local filtering still requires the full user query.
    """
    title, _season, _episode = _media_search_parts(value)
    _year, languages = _search_query_constraints(value)

    raw_title = title
    for language in sorted(languages, key=len, reverse=True):
        raw_title = re.sub(
            rf"(?<![a-z]){re.escape(language)}(?![a-z])",
            " ",
            raw_title,
            flags=re.I,
        )
    raw_title = re.sub(r"\s+", " ", raw_title).strip()

    canonical = _media_provider_query(value)
    language_terms = [
        language for language in languages
        if language in {
            "hindi", "tamil", "telugu", "malayalam", "kannada",
            "bengali", "marathi", "punjabi", "gujarati", "urdu",
        }
    ]
    language_suffix = " ".join(language_terms)
    canonical_with_language = re.sub(
        r"\s+",
        " ",
        f"{canonical} {language_suffix}".strip(),
    )

    variants: list[str] = []
    seen: set[str] = set()
    for candidate in (
        canonical_with_language,
        canonical,
        raw_title,
        f"{raw_title} {language_suffix}".strip(),
    ):
        candidate = re.sub(r"\s+", " ", candidate or "").strip()
        lowered = candidate.lower()
        if candidate and lowered not in seen:
            seen.add(lowered)
            variants.append(candidate)

    tokens = _search_tokens(raw_title)
    if len(tokens) >= 4:
        anchor = " ".join(tokens[:3])
        if anchor and anchor.lower() not in {x.lower() for x in variants}:
            variants.append(anchor)

    return variants[:3]


def _search_cache_key(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip()).lower()


def _trim_search_cache() -> None:
    while len(_search_cache) > max(10, SEARCH_CACHE_MAX_ENTRIES):
        oldest_key = min(
            _search_cache.items(),
            key=lambda pair: pair[1][0],
        )[0]
        _search_cache.pop(oldest_key, None)


async def _search_1337x_uncached(
    query: str,
    allow_series_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Perform one bounded live media search.

    This is intentionally conservative for Render Free: only the primary
    1337x/Knaben pair runs on the first pass. More expensive alternate queries
    and specialist providers are activated only when fewer than 8 usable
    results survive the hard media and 100 MB-5 GB gates.
    """
    limit = 50
    kind = _search_media_kind(query)
    variants = _media_provider_queries(query)
    primary_provider_query = variants[0] if variants else _media_provider_query(query)

    async def run_1337x(provider_query: str):
        try:
            return await asyncio.wait_for(
                search_1337x_direct(
                    query,
                    limit=20,
                    pages=2,
                    category="TV" if kind == "tv" else None,
                    provider_query=provider_query,
                ),
                timeout=max(4.5, SEARCH_TOTAL_TIMEOUT_SECONDS + 1.0),
            )
        except Exception as exc:
            logger.info("1337x search failed for '%s' using '%s': %s", query, provider_query, exc)
            return []

    async def run_lime(provider_query: str):
        try:
            lime_results = await asyncio.wait_for(
                search_limetorrents(
                    query,
                    limit=20,
                    pages=1,
                ),
                timeout=10.0,
            )
            logger.warning(
                "LimeTorrents primary source '%s': %d results",
                query,
                len(lime_results),
            )
            return lime_results
        except Exception as exc:
            logger.warning(
                "LimeTorrents search failed for '%s' using '%s': %s: %s",
                query,
                provider_query,
                type(exc).__name__,
                str(exc) or "<no message>",
            )
            return []

    async def run_knaben(provider_query: str):
        try:
            return await search_knaben(
                query,
                50,
                provider_query=provider_query,
            )
        except Exception as exc:
            logger.info("Knaben search failed for '%s' using '%s': %s", query, provider_query, exc)
            return []

    yts_task = (
        asyncio.create_task(
            asyncio.wait_for(
                search_yts_movies(query, min(limit, 20)),
                timeout=SEARCH_SOURCE_TIMEOUT_SECONDS + 0.75,
            )
        )
        if kind == "both"
        else None
    )

    primary_1337x, primary_lime, primary_knaben = await asyncio.gather(
        run_1337x(primary_provider_query),
        run_lime(primary_provider_query),
        run_knaben(primary_provider_query),
        return_exceptions=False,
    )

    yts_primary: list[dict[str, Any]] = []
    if yts_task is not None:
        try:
            yts_primary = await yts_task
        except Exception as exc:
            logger.info("YTS primary search failed for '%s': %s", query, exc)

    results: list[dict[str, Any]] = []

    def add_filtered(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if _search_quality_filter(item, query):
                results.append(item)

    add_filtered(primary_1337x)
    add_filtered(primary_lime)
    add_filtered(primary_knaben)
    add_filtered(yts_primary)

    yts_metadata: dict[str, dict[str, Any]] = {}
    for item in yts_primary:
        media_title = _normalize_title(str(item.get("mediaTitle") or ""))
        if media_title:
            yts_metadata.setdefault(media_title, item)

    def enrich_movie_metadata(items: list[dict[str, Any]]) -> None:
        for item in items:
            provider_title = str(item.get("mediaTitle") or item.get("title") or "")
            media_title = _normalize_title(_media_provider_query(provider_title))
            if not media_title:
                continue
            metadata = yts_metadata.get(media_title)
            if not metadata:
                compact = media_title.replace(" ", "")
                metadata = next(
                    (
                        candidate for key, candidate in yts_metadata.items()
                        if compact and key.replace(" ", "") == compact
                    ),
                    None,
                )
            if metadata:
                for field in ("mediaTitle", "year", "rating", "genres", "posterUrl"):
                    value = metadata.get(field)
                    if value not in (None, "", []):
                        item[field] = value

    enrich_movie_metadata(results)

    # When the primary title spelling cannot get enough usable hits, retry
    # with the alternate spelling and a short anchor in parallel. This is the
    # cheap way to handle Spider-Man/Spiderman-style provider differences
    # without always doubling Render traffic.
    if allow_series_fallback and len(results) < min(8, limit):
        secondary_queries = variants[1:3]

        variant_tasks = [
            asyncio.create_task(run_1337x(provider_query))
            for provider_query in secondary_queries
        ]
        variant_tasks.extend(
            asyncio.create_task(run_knaben(provider_query))
            for provider_query in secondary_queries
        )

        fallback_tasks: list[asyncio.Task] = [
            asyncio.create_task(
                search_torrents_csv(
                    query,
                    min(limit, 50),
                )
            ),
            asyncio.create_task(
                search_apibay(
                    query,
                    min(limit, 50),
                )
            ),
        ]

        if kind in {"tv", "both"}:
            fallback_tasks.append(
                asyncio.create_task(
                    asyncio.wait_for(
                        search_tv_eztv(query, min(limit, 20)),
                        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS + 0.5,
                    )
                )
            )
        if kind == "both":
            fallback_tasks.append(
                asyncio.create_task(
                    asyncio.wait_for(
                        search_yts_movies(query, min(limit, 20)),
                        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS + 0.5,
                    )
                )
            )

        values = await asyncio.gather(
            *(variant_tasks + fallback_tasks),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, list):
                add_filtered(value)
            elif isinstance(value, dict):
                add_filtered(value.get("results", []))

        enrich_movie_metadata(results)

    merged: dict[str, dict[str, Any]] = {}
    for item in results:
        digest = str(item.get("infoHash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", digest):
            digest = info_hash(str(item.get("magnetUrl") or ""))

        if re.fullmatch(r"[0-9a-f]{40}", digest, re.I):
            key = "hash:" + digest.lower()
        else:
            # Listing-page results now resolve magnets lazily, so use a
            # normalized title + size to collapse the same release returned
            # by more than one provider.
            normalized_title = _normalize_title(str(item.get("title") or ""))
            if not normalized_title:
                continue
            size = int(item.get("size") or 0)
            key = f"title-size:{normalized_title}|{size}"

        previous = merged.get(key)
        if previous is None or (
            int(item.get("seeders") or 0),
            int(item.get("leechers") or 0),
            1 if item.get("magnetUrl") else 0,
        ) > (
            int(previous.get("seeders") or 0),
            int(previous.get("leechers") or 0),
            1 if previous.get("magnetUrl") else 0,
        ):
            merged[key] = item

    results = list(merged.values())
    enrich_movie_metadata(results)

    source_counts: dict[str, int] = {}
    for item in results:
        source_name = str(item.get("indexer") or "unknown").strip() or "unknown"
        source_counts[source_name] = source_counts.get(source_name, 0) + 1
    logger.warning(
        "Search result sources for '%s': %s",
        query,
        ", ".join(f"{name}={count}" for name, count in sorted(source_counts.items())),
    )

    # Poster artwork is resolved lazily by /api/poster so search latency is
    # unaffected. Every normal movie result gets a cleaned poster URL,
    # regardless of whether it came from YTS, Knaben, 1337x, or another source.
    if kind == "both":
        for item in results:
            if not str(item.get("posterUrl") or "").strip():
                poster_url = _poster_url_for_release(str(item.get("title") or ""))
                if poster_url:
                    item["posterUrl"] = poster_url

    # Do not let questionable seed counts from one indexer dominate the list.
    # Rank direct title matches first, then recognizable release quality, and
    # use swarm size/date as secondary signals within comparable results.
    results.sort(
        key=lambda item: (
            _title_relevance(str(item.get("title") or ""), query)[0],
            _release_quality_score(str(item.get("title") or "")),
            min(int(item.get("seeders") or 0), 5000),
            int(item.get("leechers") or 0),
            str(item.get("publishDate") or ""),
        ),
        reverse=True,
    )

    return results[:limit]


async def search_1337x(
    query: str,
    limit: int = 50,
    allow_series_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Fast media-only search with stale cache and in-flight request sharing."""
    query = re.sub(r"\s+", " ", query.strip())
    if not query:
        return []

    limit = min(max(int(limit or 50), 1), 50)
    cache_key = _search_cache_key(query)
    now = time.monotonic()

    cached = _search_cache.get(cache_key)
    if cached and len(cached[1]) < SEARCH_CACHE_MIN_RESULTS:
        _search_cache.pop(cache_key, None)
        cached = None

    if cached:
        cached_at, cached_results = cached
        age = max(0.0, now - cached_at)
        ttl = SEARCH_CACHE_SECONDS

        if age < ttl:
            return cached_results[:limit]

        if age < SEARCH_CACHE_STALE_SECONDS:
            # Serve stale results immediately and refresh only once per query.
            existing = _search_inflight.get(cache_key)
            if existing is None or existing.done():
                refresh = asyncio.create_task(
                    _search_1337x_uncached(
                        query,
                        allow_series_fallback=allow_series_fallback,
                    )
                )
                _search_inflight[cache_key] = refresh

                def _finish_refresh(task: asyncio.Task, key: str = cache_key):
                    _search_inflight.pop(key, None)
                    if task.cancelled():
                        return
                    try:
                        refreshed = task.result()
                    except Exception as exc:
                        logger.info("Background search refresh failed for '%s': %s", query, exc)
                        return
                    if len(refreshed) >= SEARCH_CACHE_MIN_RESULTS:
                        _search_cache[key] = (time.monotonic(), refreshed)
                        _trim_search_cache()

                refresh.add_done_callback(_finish_refresh)
            return cached_results[:limit]

    # Share the same live request among concurrent users. This is particularly
    # useful on Render Free where multiple browser tabs/devices can otherwise
    # fan out identical provider requests.
    task = _search_inflight.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(
            _search_1337x_uncached(
                query,
                allow_series_fallback=allow_series_fallback,
            )
        )
        _search_inflight[cache_key] = task

    try:
        results = await task
    except asyncio.CancelledError:
        # The provider task may still be useful to other callers; do not leave
        # a dead task in the inflight map.
        if _search_inflight.get(cache_key) is task:
            _search_inflight.pop(cache_key, None)
        raise
    except Exception as exc:
        if _search_inflight.get(cache_key) is task:
            _search_inflight.pop(cache_key, None)
        logger.warning("Media search failed for '%s': %s", query, exc)
        return cached[1][:limit] if cached else []

    if _search_inflight.get(cache_key) is task:
        _search_inflight.pop(cache_key, None)

    if len(results) >= SEARCH_CACHE_MIN_RESULTS:
        _search_cache[cache_key] = (time.monotonic(), results)
        _trim_search_cache()
    return results[:limit]

def parse_size(value: str) -> int:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)", value or "", re.I)
    if not m:
        return 0
    n = float(m.group(1))
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(n * units[m.group(2).upper()])

_POSTER_CACHE_SECONDS = 6 * 60 * 60
_POSTER_NEGATIVE_CACHE_SECONDS = 2 * 60
_CINEMETA_BASE_URL = "https://v3-cinemeta.strem.io"
_poster_cache: dict[str, tuple[float, str | None]] = {}
_poster_inflight: dict[str, asyncio.Task[str | None]] = {}


def _poster_normalize_title(value: str) -> str:
    value = str(value or "").lower().replace("’", "'")
    # Torrent sources frequently vary only in punctuation/spelling. Treat
    # possessives as the same words and normalize common title variants.
    value = value.replace("&", " and ")
    value = value.replace("'", "")
    tokens = re.findall(r"[a-z0-9]+", value)
    token_aliases = {
        "v": "vs",
        "vs": "vs",
        "carribean": "caribbean",
    }
    tokens = [token_aliases.get(token, token) for token in tokens]
    normalized = " ".join(tokens).strip()
    return re.sub(r"^(?:the|a|an)\s+", "", normalized)




def _poster_title_matches(wanted: str, candidate: str) -> bool:
    """Accept only exact normalized titles to avoid unrelated poster artwork."""
    return bool(wanted and candidate and wanted == candidate)

def _poster_prepare_lookup_title(title: str, year: str = "") -> str:
    """Strip common release artifacts before querying poster providers."""
    value = str(title or "").strip()
    clean_year = str(year or "").strip()

    # The frontend can send both "(YEAR)" inside title and year=YEAR.
    if clean_year:
        value = re.sub(
            rf"\s*[\(\[\-:]?\s*{re.escape(clean_year)}\s*[\)\]]?\s*$",
            "",
            value,
            flags=re.I,
        )

    # Frontend poster candidates can end with a lone opening delimiter after
    # removing release information (e.g. "Batman Returns (").
    value = value.rstrip(" ([{").strip(" -._")

    # Strip season/episode information and everything after it. This lets a
    # series release such as "Batman The Animated Series S03E05 Time Out of Joint"
    # resolve to the series poster instead of treating the episode title as a movie.
    value = re.sub(
        r"\s+S\d{1,2}(?:E\d{1,3})?\b.*$",
        "",
        value,
        flags=re.I,
    ).strip(" -._([")

    return value.strip()

def _poster_title_aliases(title: str, year: str = "") -> list[str]:
    """Return provider-friendly title aliases for common torrent shorthand."""
    clean_title = str(title or "").strip()
    clean_year = str(year or "").strip()
    normalized = _poster_normalize_title(clean_title)
    aliases = [clean_title] if clean_title else []

    # Torrent sources often shorten sequel titles to a franchise number.
    # Keep this deliberately narrow so numeric titles do not create broad false matches.
    pirates_sequels = {
        "1": "Pirates of the Caribbean: The Curse of the Black Pearl",
        "2": "Pirates of the Caribbean: Dead Man's Chest",
        "3": "Pirates of the Caribbean: At World's End",
        "4": "Pirates of the Caribbean: On Stranger Tides",
        "5": "Pirates of the Caribbean: Dead Men Tell No Tales",
    }
    match = re.fullmatch(r"pirates of the caribbean ([1-5])", normalized)
    if match:
        alias = pirates_sequels[match.group(1)]
        if alias not in aliases:
            aliases.append(alias)

    # Common LimeTorrents naming variants differ from catalog titles. These
    # explicit aliases preserve exact matching and do not reopen broad substring
    # matches that previously returned unrelated artwork.
    known_aliases = {
        "avengers 2": ("Avengers: Age of Ultron", "2015"),
        "marvels the avengers": ("The Avengers", "2012"),
        "marvel s the avengers": ("The Avengers", "2012"),
    }
    known = known_aliases.get(normalized)
    if known:
        alias, expected_year = known
        if not clean_year or clean_year == expected_year:
            if alias not in aliases:
                aliases.append(alias)

    return aliases

def _poster_title_parts(raw_title: str) -> tuple[str, str]:
    """Extract the movie title/year while removing common release-name debris."""
    value = str(raw_title or "").replace(".", " ").replace("_", " ")
    year_match = re.search(r"\b((?:19|20)\d{2})\b", value)
    year = year_match.group(1) if year_match else ""

    if year_match:
        value = value[:year_match.start()]

    # Clean bracketed tags before splitting on codec/quality markers. Doing
    # this in the opposite order leaves a dangling "[" in titles such as
    # "The Avengers 2 [1080p]", which then prevents an exact poster match.
    value = re.sub(r"\[[^\]]*(?:\]|$)", " ", value)
    value = re.sub(r"\([^)]*(?:\)|$)", " ", value)

    if not year_match:
        value = re.split(
            r"\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|"
            r"web[- ]?dl|web[- ]?rip|webrip|bluray|brrip|hdrip|"
            r"dvdrip|dvd|cam|hdcam|telesync|telecine|scr|r5|r6|"
            r"x264|x265|h264|h265|hevc|xvid|divx|dts|aac|ac3|ddp|"
            r"eng|english|nlsub|proper|repack|remux|hdr10|hdr)\b",
            value,
            maxsplit=1,
            flags=re.I,
        )[0]

    # A few common LimeTorrents uploader tags don't have a preceding codec
    # marker, so remove them if they remain as the final token/group.
    value = re.sub(
        r"\s+(?:BlueLady(?:RG)?|Jaybob|HAGGiS|Voltage|DTRG|DOCUMENT|ViSiON|"
        r"SONiDO|SAiMORNY|LTT)$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip(" -._[](){}")
    return value, year


def _poster_url_for_release(raw_title: str) -> str:
    title, year = _poster_title_parts(raw_title)
    if not title:
        return ""
    return f"/api/poster?{urlencode({'title': title, 'year': year})}"


async def _cinemeta_poster_lookup(title: str, year: str = "") -> str | None:
    """Resolve title/year through Cinemeta's catalog, then use its stable ID metadata."""
    clean_title = str(title or "").strip()
    clean_year = str(year or "").strip()
    if not clean_title:
        return None

    title_aliases = _poster_title_aliases(clean_title, clean_year)
    wanted_titles = [_poster_normalize_title(value) for value in title_aliases]
    queries = []
    for alias in title_aliases:
        for query_value in (
            f"{alias} {clean_year}".strip(),
            alias,
        ):
            if query_value and query_value not in queries:
                queries.append(query_value)

    try:
        async with httpx.AsyncClient(timeout=4, follow_redirects=True) as client:
            for media_type in ("movie", "series"):
                for query_value in queries:
                    encoded = quote(query_value, safe="")
                    response = await client.get(
                        f"{_CINEMETA_BASE_URL}/catalog/{media_type}/top/search={encoded}.json",
                        headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
                    )
                    if response.status_code != 200:
                        continue

                    data = response.json()
                    metas = data.get("metas") if isinstance(data, dict) else None
                    if not isinstance(metas, list):
                        continue

                    candidates: list[tuple[int, str, str]] = []
                    for row in metas:
                        if not isinstance(row, dict):
                            continue
                        name = str(row.get("name") or "").strip()
                        candidate = _poster_normalize_title(name)
                        poster = str(row.get("poster") or "").strip()
                        imdb_id = str(row.get("id") or "").strip()
                        if not name or not any(_poster_title_matches(wanted, candidate) for wanted in wanted_titles):
                            continue

                        candidate_year = str(
                            row.get("releaseInfo")
                            or row.get("year")
                            or ""
                        ).strip()

                        # Once the torrent/search result gives us a year, do not
                        # allow a different-year title just because its name is a
                        # fuzzy/contained match (e.g. "Justice League" 2017 must
                        # never satisfy "Justice League: War" 2014).
                        if clean_year:
                            candidate_year_value = candidate_year[:4]
                            if not candidate_year_value or candidate_year_value != clean_year:
                                continue

                        score = 100
                        if clean_year and candidate_year:
                            if candidate_year == clean_year or candidate_year.startswith(clean_year):
                                score += 120
                            else:
                                score -= 80
                        if poster:
                            score += 30
                        if imdb_id.startswith("tt"):
                            score += 10
                        candidates.append((score, poster, imdb_id))

                    if not candidates:
                        continue

                    candidates.sort(key=lambda item: item[0], reverse=True)
                    _score, poster, imdb_id = candidates[0]
                    if poster:
                        return poster

                    if imdb_id.startswith("tt"):
                        meta_response = await client.get(
                            f"{_CINEMETA_BASE_URL}/meta/{media_type}/{quote(imdb_id, safe='')}.json",
                            headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
                        )
                        if meta_response.status_code == 200:
                            meta_data = meta_response.json()
                            meta = meta_data.get("meta") if isinstance(meta_data, dict) else None
                            poster = str(meta.get("poster") or "").strip() if isinstance(meta, dict) else ""
                            if poster:
                                return poster
    except Exception:
        pass

    return None


async def _poster_lookup_uncached(clean_title: str, clean_year: str, cache_key: str) -> str | None:
    now = time.time()
    poster = None

    try:
        title_aliases = _poster_title_aliases(clean_title, clean_year)
        wanted_titles = [_poster_normalize_title(value) for value in title_aliases]
        wanted_year = clean_year
        candidates: list[tuple[int, str]] = []
        async with httpx.AsyncClient(timeout=4, follow_redirects=True) as client:
            for alias in title_aliases:
                query = quote((alias + " " + clean_year).strip(), safe="")
                response = await client.get(
                    f"https://v3.sg.media-imdb.com/suggestion/titles/x/{query}.json?includeVideos=0",
                    headers={"Accept": "application/json"},
                )
                if response.status_code != 200:
                    continue
                data = response.json()
                for row in data.get("d") or []:
                    if not isinstance(row, dict):
                        continue
                    image = str((row.get("i") or {}).get("imageUrl") or "").strip()
                    candidate = _poster_normalize_title(str(row.get("l") or ""))
                    candidate_year = str(row.get("y") or "")
                    # Ignore episodes, shorts, games, and other unrelated title types.
                    # Search suggestions can contain similarly named fan clips with
                    # artwork that is not the poster for the requested movie/series.
                    media_kind = str(row.get("qid") or "").lower()
                    if media_kind not in {"movie", "tvseries", "tvminiseries", "tvmovie"}:
                        continue
                    if not image or not any(_poster_title_matches(wanted, candidate) for wanted in wanted_titles):
                        continue
                    if wanted_year:
                        if not candidate_year or candidate_year != wanted_year:
                            continue
                    score = 100
                    if wanted_year and candidate_year == wanted_year:
                        score += 100
                    if str(row.get("qid") or "") == "movie":
                        score += 10
                    score += max(0, 20 - int(row.get("rank") or 20))
                    candidates.append((score, image))
                if candidates:
                    candidates.sort(key=lambda pair: pair[0], reverse=True)
                    poster = candidates[0][1]
    except Exception:
        pass

    if not poster:
        try:
            title_aliases = _poster_title_aliases(clean_title, clean_year)
            wanted_titles = [_poster_normalize_title(value) for value in title_aliases]
            wanted_year = clean_year
            candidates: list[tuple[int, str]] = []
            async with httpx.AsyncClient(timeout=4, follow_redirects=True) as client:
                for alias in title_aliases:
                    response = await client.get(
                        "https://itunes.apple.com/search",
                        params={"term": (alias + " " + clean_year).strip(), "limit": "25"},
                        headers={"Accept": "application/json"},
                    )
                    if response.status_code != 200:
                        continue
                    data = response.json()
                    for row in data.get("results") or []:
                        if str(row.get("kind") or "") != "feature-movie" or not row.get("artworkUrl100"):
                            continue
                        candidate = _poster_normalize_title(str(row.get("trackName") or row.get("collectionName") or ""))
                        candidate_year = str(row.get("releaseDate") or "")[:4]
                        if not any(_poster_title_matches(wanted, candidate) for wanted in wanted_titles):
                            continue
                        if wanted_year:
                            if not candidate_year or candidate_year != wanted_year:
                                continue
                        score = 100
                        if wanted_year and candidate_year == wanted_year:
                            score += 100
                        candidates.append((score, str(row["artworkUrl100"]).replace("100x100bb", "600x600bb")))
                if candidates:
                    candidates.sort(key=lambda pair: pair[0], reverse=True)
                    poster = candidates[0][1]
        except Exception:
            pass

    # Cinemeta is intentionally last: it is a metadata/identity fallback and
    # never sits on the torrent-search critical path.
    if not poster:
        poster = await _cinemeta_poster_lookup(clean_title, clean_year)

    _poster_cache[cache_key] = (now, poster)
    return poster


async def _poster_lookup(title: str, year: str = "") -> str | None:
    clean_year = str(year or "").strip()
    clean_title = _poster_prepare_lookup_title(title, clean_year)
    if not clean_title:
        return None

    cache_key = f"{_poster_normalize_title(clean_title)}|{clean_year}"
    now = time.time()
    cached = _poster_cache.get(cache_key)
    cache_ttl = _POSTER_CACHE_SECONDS if cached and cached[1] else _POSTER_NEGATIVE_CACHE_SECONDS
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    existing = _poster_inflight.get(cache_key)
    if existing is not None and not existing.done():
        try:
            return await asyncio.shield(existing)
        except Exception:
            return None

    task = asyncio.create_task(_poster_lookup_uncached(clean_title, clean_year, cache_key))
    _poster_inflight[cache_key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _poster_inflight.get(cache_key) is task:
            _poster_inflight.pop(cache_key, None)


@app.get("/api/poster")
async def api_poster(title: str = Query(..., min_length=1), year: str = Query("")):
    poster = await _poster_lookup(title, year)
    if not poster:
        raise HTTPException(404, "Poster not found")
    return RedirectResponse(poster, status_code=302)


@app.get("/api/poster/resolve")
async def api_poster_resolve(title: str = Query(..., min_length=1), year: str = Query("")):
    poster = await _poster_lookup(title, year)
    if not poster:
        raise HTTPException(404, "Poster not found")
    return {"url": poster}

@app.get("/")
async def root():
    return {"name": APP_NAME, "status": "ok"}

@app.on_event("startup")
async def start_seedr_cleanup_worker():
    global _seedr_cleanup_worker_task
    _load_seedr_cleanup_jobs()
    if _seedr_cleanup_worker_task is None or _seedr_cleanup_worker_task.done():
        _seedr_cleanup_worker_task = asyncio.create_task(_seedr_cleanup_worker())


@app.get("/health")
async def health():
    return {"status": "ok", "seedrConfigured": bool(current_seedr_token()), "torrentSearchApi": TORRENT_SEARCH_API_URL}

@app.post("/api/search/resolve-magnet")
async def resolve_search_magnet(body: dict[str, Any]):
    """Resolve a provider detail page to a magnet only when the user prepares it."""
    info_url = str(body.get("infoUrl") or body.get("info_url") or "").strip()
    if not info_url:
        raise HTTPException(400, "A provider detail URL is required.")

    parsed = urlsplit(info_url)
    allowed_hosts = {
        str(host).lower().removeprefix("www.")
        for host in (*X1337_HOSTS, *LIMETORRENTS_HOSTS)
    }
    hostname = str(parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https" or hostname not in allowed_hosts:
        raise HTTPException(400, "The provider URL is not allowed.")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
            response = await client.get(info_url)
            final_url = urlsplit(str(response.url))
            final_host = str(final_url.hostname or "").lower().removeprefix("www.")
            if final_url.scheme != "https" or final_host not in allowed_hosts:
                raise HTTPException(400, "The provider redirected to an unapproved domain.")
            response.raise_for_status()
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.info("Lazy magnet resolution failed for provider URL %s: %s", info_url, exc)
        raise HTTPException(502, "Could not reach the torrent provider. Please try another result.") from exc

    # Prefer a real magnet anchor when the detail page includes one.
    # Parsing hrefs handles HTML-escaped ampersands better than scanning the
    # entire page for arbitrary strings.
    page = BeautifulSoup(response.text or "", "html.parser")
    magnet = ""
    for anchor in page.select('a[href^="magnet:"]'):
        candidate = unescape(str(anchor.get("href") or "")).strip()
        if info_hash(candidate):
            magnet = candidate
            break

    if not magnet:
        match = re.search(
            r"magnet:\?xt=urn:btih:[^\"<\s]+",
            response.text,
            re.IGNORECASE,
        )
        if match:
            candidate = unescape(match.group(0))
            if info_hash(candidate):
                magnet = candidate

    if not magnet:
        # LimeTorrents also exposes a hash on its download-icon/.torrent URL.
        # Recover a standard magnet when the detail page omits a magnet anchor.
        download_anchor = (
            page.select_one("a.csprite_dl14[href]")
            or page.select_one("a[href*='/download/']")
        )
        download_href = str(download_anchor.get("href") or "").strip() if download_anchor else ""
        hash_match = re.search(
            r"(?i)(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
            download_href,
        )
        if hash_match:
            digest = hash_match.group(1).lower()
            title_node = page.select_one("#content h1, h1, .tt-name")
            display_name = (
                title_node.get_text(" ", strip=True)
                if title_node is not None
                else Path(parsed.path).stem.replace("-", " ")
            )
            magnet = f"magnet:?xt=urn:btih:{digest}&dn={quote(display_name, safe='')}"
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet += "&tr=" + quote(tracker, safe="")

    digest = info_hash(magnet)
    if not digest:
        raise HTTPException(404, "The provider did not expose a usable magnet/hash for this result.")
    return {"magnet": magnet, "infoHash": digest}



# One-time Marvel catalogue. Results are shared by every visitor to this Home
# instance and never expire; the deployment mounts /app/data on persistent VPS
# storage so a container replacement does not force another provider crawl.
MARVEL_CATALOGUE_VERSION = 3
MARVEL_CATALOGUE_PATH = Path(os.getenv("MARVEL_CATALOGUE_PATH", "/app/data/marvel_catalogue.json"))
MARVEL_MOVIE_SEARCHES: tuple[tuple[str, int], ...] = (
    ("Spider-Man: Brand New Day", 2026),
    ("The Fantastic Four: First Steps", 2025),
    ("Thunderbolts", 2025),
    ("Captain America: Brave New World", 2025),
    ("Deadpool & Wolverine", 2024),
    ("Venom: The Last Dance", 2024),
    ("Kraven the Hunter", 2024),
    ("Madame Web", 2024),
    ("The Marvels", 2023),
    ("Guardians of the Galaxy Vol. 3", 2023),
    ("Spider-Man: Across the Spider-Verse", 2023),
    ("Ant-Man and the Wasp: Quantumania", 2023),
    ("Black Panther: Wakanda Forever", 2022),
    ("Thor: Love and Thunder", 2022),
    ("Doctor Strange in the Multiverse of Madness", 2022),
    ("Morbius", 2022),
    ("Spider-Man: No Way Home", 2021),
    ("Venom: Let There Be Carnage", 2021),
    ("Eternals", 2021),
    ("Shang-Chi and the Legend of the Ten Rings", 2021),
    ("Black Widow", 2021),
    ("The New Mutants", 2020),
    ("Dark Phoenix", 2019),
    ("Spider-Man: Far From Home", 2019),
    ("Avengers: Endgame", 2019),
    ("Captain Marvel", 2019),
    ("Spider-Man: Into the Spider-Verse", 2018),
    ("Venom", 2018),
    ("Ant-Man and the Wasp", 2018),
    ("Deadpool 2", 2018),
    ("Avengers: Infinity War", 2018),
    ("Black Panther", 2018),
    ("Thor: Ragnarok", 2017),
    ("Logan", 2017),
    ("Spider-Man: Homecoming", 2017),
    ("Guardians of the Galaxy Vol. 2", 2017),
    ("Doctor Strange", 2016),
    ("X-Men: Apocalypse", 2016),
    ("Deadpool", 2016),
    ("Captain America: Civil War", 2016),
    ("Fantastic Four", 2015),
    ("Ant-Man", 2015),
    ("Avengers: Age of Ultron", 2015),
    ("X-Men: Days of Future Past", 2014),
    ("The Amazing Spider-Man 2", 2014),
    ("Captain America: The Winter Soldier", 2014),
    ("Guardians of the Galaxy", 2014),
    ("Big Hero 6", 2014),
    ("Avengers Confidential: Black Widow & Punisher", 2014),
    ("Iron Man: Rise of Technovore", 2013),
    ("The Wolverine", 2013),
    ("Iron Man 3", 2013),
    ("The Amazing Spider-Man", 2012),
    ("The Avengers", 2012),
    ("Ghost Rider: Spirit of Vengeance", 2011),
    ("X-Men: First Class", 2011),
    ("Captain America: The First Avenger", 2011),
    ("Thor", 2011),
    ("Thor: Tales of Asgard", 2011),
    ("Iron Man 2", 2010),
    ("Planet Hulk", 2010),
    ("X-Men Origins: Wolverine", 2009),
    ("Hulk Vs.", 2009),
    ("Punisher: War Zone", 2008),
    ("The Incredible Hulk", 2008),
    ("Iron Man", 2008),
    ("Next Avengers: Heroes of Tomorrow", 2008),
    ("Spider-Man 3", 2007),
    ("Ghost Rider", 2007),
    ("Fantastic Four: Rise of the Silver Surfer", 2007),
    ("X-Men: The Last Stand", 2006),
    ("Ultimate Avengers 2", 2006),
    ("Ultimate Avengers", 2006),
    ("Elektra", 2005),
    ("Fantastic Four", 2005),
    ("Man-Thing", 2005),
    ("Spider-Man 2", 2004),
    ("The Punisher", 2004),
    ("Blade: Trinity", 2004),
    ("X2: X-Men United", 2003),
    ("Daredevil", 2003),
    ("Hulk", 2003),
    ("Spider-Man", 2002),
    ("Blade II", 2002),
    ("X-Men", 2000),
    ("Blade", 1998),
    ("Nick Fury: Agent of S.H.I.E.L.D.", 1998),
    ("Generation X", 1996),
    ("The Trial of the Incredible Hulk", 1989),
    ("The Punisher", 1989),
    ("The Incredible Hulk Returns", 1988),
    ("Howard the Duck", 1986),
    ("Captain America II: Death Too Soon", 1979),
    ("Captain America", 1979),
    ("Dr. Strange", 1978),
)
_MARVEL_CATALOGUE_CONCURRENCY = 4
_marvel_catalogue_task: asyncio.Task | None = None
_marvel_catalogue_retry_after = 0.0
_marvel_catalogue_state: dict[str, Any] = {
    "status": "idle",
    "completed": 0,
    "total": len(MARVEL_MOVIE_SEARCHES),
    "resultCount": 0,
    "error": "",
}


def _read_marvel_catalogue() -> dict[str, Any] | None:
    try:
        payload = json.loads(MARVEL_CATALOGUE_PATH.read_text(encoding="utf-8"))
        if (
            isinstance(payload, dict)
            and payload.get("version") == MARVEL_CATALOGUE_VERSION
            and isinstance(payload.get("results"), list)
            and payload["results"]
        ):
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_marvel_catalogue(payload: dict[str, Any]) -> None:
    MARVEL_CATALOGUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="marvel-catalogue-",
            suffix=".tmp",
            dir=str(MARVEL_CATALOGUE_PATH.parent),
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, MARVEL_CATALOGUE_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _marvel_result_matches_title(item: dict[str, Any], title: str, year: int) -> bool:
    """Prevent related Marvel releases from being mislabeled as another movie."""
    raw_title = str(item.get("title") or "").strip()
    if not raw_title:
        return False

    actual_tokens = set(_search_tokens(raw_title))
    expected_tokens = _search_tokens(title)
    if expected_tokens and expected_tokens[0] in {"the", "a", "an"}:
        expected_tokens = expected_tokens[1:]
    if not expected_tokens or not all(token in actual_tokens for token in expected_tokens):
        return False

    # If a release explicitly carries a year, don't cross-wire remakes/sequels.
    title_years = {
        int(value) for value in re.findall(r"\b((?:19|20)\d{2})\b", raw_title)
    }
    if title_years and year not in title_years:
        return False

    metadata_year = item.get("year")
    if metadata_year not in (None, "") and not title_years:
        try:
            if int(metadata_year) != year:
                return False
        except (TypeError, ValueError):
            pass

    return True


def _extra_result_matches_language(item: dict[str, Any], language: str | None) -> bool:
    """Separate English-original and Hindi/dubbed torrent releases for shared caches."""
    normalized_language = str(language or "").strip().lower()
    if normalized_language not in {"english", "hindi"}:
        return True

    release_text = " ".join(
        str(item.get(field) or "")
        for field in ("title", "quality", "audio", "language", "languages", "audioLanguage", "releaseName")
    )
    hindi_marker = re.compile(
        r"\b(?:hindi|hin|dual[\s._-]*audio|multi[\s._-]*audio|hindi[\s._-]*(?:dubbed|audio))\b",
        re.I,
    )
    other_language_marker = re.compile(
        r"\b(?:hindi|hin|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|"
        r"dual[\s._-]*audio|multi[\s._-]*audio|multi[\s._-]*language|dubbed)\b",
        re.I,
    )
    if normalized_language == "hindi":
        return bool(hindi_marker.search(release_text))
    return not bool(other_language_marker.search(release_text))


async def _build_marvel_catalogue() -> None:
    global _marvel_catalogue_retry_after
    semaphore = asyncio.Semaphore(_MARVEL_CATALOGUE_CONCURRENCY)
    poster_semaphore = asyncio.Semaphore(2)
    rows_by_hash: dict[str, dict[str, Any]] = {}
    found_movies: set[str] = set()

    async def resolve_movie_poster(title: str, year: int) -> str:
        async with poster_semaphore:
            try:
                poster = await asyncio.wait_for(
                    _poster_lookup(title, str(year)),
                    timeout=14.0,
                )
                if poster:
                    return poster
            except Exception as exc:
                logger.debug("Marvel poster lookup failed for '%s (%s)': %s", title, year, exc)
        return _poster_url_for_release(f"{title} {year}")

    async def search_movie(title: str, year: int) -> list[dict[str, Any]]:
        async with semaphore:
            try:
                query = f"{title} {year}"
                # Use Home's full provider fan-out first. Some providers return
                # zero rows for older/animated Marvel titles, so fall back to
                # the dedicated YTS catalogue search for those titles.
                try:
                    primary = await asyncio.wait_for(
                        search_1337x(query, limit=50, allow_series_fallback=False),
                        timeout=16.0,
                    )
                except Exception as exc:
                    logger.info("Marvel provider search timed out for '%s': %s", query, exc)
                    primary = []

                def validate_items(items: Any) -> list[dict[str, Any]]:
                    accepted: list[dict[str, Any]] = []
                    if not isinstance(items, list):
                        return accepted
                    for original in items:
                        if (
                            not isinstance(original, dict)
                            or not _marvel_result_matches_title(original, title, year)
                            or not _extra_result_matches_language(original, "english")
                        ):
                            continue
                        item = dict(original)
                        try:
                            size = int(float(item.get("size") or 0))
                            seeders = int(float(item.get("seeders") or 0))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if size < 100 * 1024 * 1024 or size > MAX_SEARCH_RESULT_SIZE_BYTES or seeders <= 0:
                            continue
                        if not any(str(item.get(key) or "").strip() for key in (
                            "magnetUrl", "downloadUrl", "sourceUrl", "infoUrl", "infoHash"
                        )):
                            continue

                        # This field is the stable catalogue identity, not the
                        # raw torrent release name. Quality/magnet stay per variant.
                        item["mediaTitle"] = title
                        item["year"] = year
                        if not item.get("posterUrl"):
                            item["posterUrl"] = _poster_url_for_release(f"{title} {year}")
                        accepted.append(item)
                    return accepted

                accepted = validate_items(primary)
                if len(accepted) < 3:
                    try:
                        yts_items = await asyncio.wait_for(
                            search_yts_movies(query, limit=20),
                            timeout=8.0,
                        )
                        accepted.extend(validate_items(yts_items))
                    except Exception as exc:
                        logger.info("Marvel YTS fallback failed for '%s': %s", query, exc)

                # Deduplicate alternatives per movie before enriching shared poster
                # metadata. Keep the strongest copy of the same info hash.
                per_movie: dict[str, dict[str, Any]] = {}
                for item in accepted:
                    digest = str(item.get("infoHash") or "").strip().lower()
                    if not re.fullmatch(r"[0-9a-f]{40}", digest):
                        digest = info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
                    key = "hash:" + digest if re.fullmatch(r"[0-9a-f]{40}", digest, re.I) else (
                        f"title-size:{_normalize_title(str(item.get('title') or ''))}|{int(item.get('size') or 0)}"
                    )
                    previous = per_movie.get(key)
                    if previous is None or (
                        int(item.get("seeders") or 0),
                        int(item.get("leechers") or 0),
                        1 if str(item.get("magnetUrl") or "").startswith("magnet:") else 0,
                    ) > (
                        int(previous.get("seeders") or 0),
                        int(previous.get("leechers") or 0),
                        1 if str(previous.get("magnetUrl") or "").startswith("magnet:") else 0,
                    ):
                        per_movie[key] = item

                accepted = list(per_movie.values())
                if accepted:
                    found_movies.add(f"{_normalize_title(title)}|{year}")
                    poster_url = await resolve_movie_poster(title, year)
                    for item in accepted:
                        item["mediaTitle"] = title
                        item["year"] = year
                        item["posterUrl"] = poster_url
                        item["marvelCatalogueTitle"] = title

                return accepted
            except Exception as exc:
                logger.info("Marvel catalogue search failed for '%s (%s)': %s", title, year, exc)
                return []
            finally:
                _marvel_catalogue_state["completed"] = int(
                    _marvel_catalogue_state.get("completed") or 0
                ) + 1

    try:
        _marvel_catalogue_state.update({
            "status": "building",
            "completed": 0,
            "total": len(MARVEL_MOVIE_SEARCHES),
            "resultCount": 0,
            "movieCount": 0,
            "error": "",
        })
        tasks = [
            asyncio.create_task(search_movie(title, year))
            for title, year in MARVEL_MOVIE_SEARCHES
        ]
        for task in asyncio.as_completed(tasks):
            try:
                items = await task
            except Exception as exc:
                logger.info("Marvel catalogue worker failed: %s", exc)
                items = []

            for item in items:
                digest = str(item.get("infoHash") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", digest):
                    source = str(item.get("magnetUrl") or item.get("downloadUrl") or "")
                    digest = info_hash(source)
                if re.fullmatch(r"[0-9a-f]{40}", digest, re.I):
                    key = "hash:" + digest.lower()
                else:
                    normalized = _normalize_title(str(item.get("title") or ""))
                    if not normalized:
                        continue
                    key = f"title-size:{normalized}|{int(item.get('size') or 0)}"

                previous = rows_by_hash.get(key)
                candidate_score = (
                    1 if str(item.get("magnetUrl") or "").startswith("magnet:") else 0,
                    int(item.get("seeders") or 0),
                    int(item.get("leechers") or 0),
                )
                previous_score = (
                    1 if previous and str(previous.get("magnetUrl") or "").startswith("magnet:") else 0,
                    int(previous.get("seeders") or 0) if previous else 0,
                    int(previous.get("leechers") or 0) if previous else 0,
                )
                if previous is None or candidate_score > previous_score:
                    rows_by_hash[key] = item
            _marvel_catalogue_state["resultCount"] = len(rows_by_hash)
            _marvel_catalogue_state["movieCount"] = len(found_movies)

        results = list(rows_by_hash.values())
        movie_count = len({
            f"{_normalize_title(str(item.get('mediaTitle') or item.get('title') or ''))}|{item.get('year') or ''}"
            for item in results
        })
        # Don't permanently cache a partial crawl like a list of just a handful
        # of recent releases. A shared catalogue must contain a useful spread.
        if movie_count < 12:
            raise RuntimeError(
                f"Only {movie_count} Marvel movie titles produced usable results; refusing to cache an incomplete catalogue."
            )

        def catalogue_sort_key(item: dict[str, Any]) -> tuple[int, float, int, str]:
            source = str(item.get("mediaTitle") or item.get("title") or "")
            embedded_year = re.search(r"\b((?:19|20)\d{2})\b", source)
            try:
                year = int(item.get("year") or (embedded_year.group(1) if embedded_year else 0))
            except (TypeError, ValueError):
                year = int(embedded_year.group(1)) if embedded_year else 0
            try:
                rating = float(item.get("rating") or 0)
            except (TypeError, ValueError):
                rating = 0.0
            return (year, rating, int(item.get("seeders") or 0), source.lower())

        results.sort(key=catalogue_sort_key, reverse=True)
        payload = {
            "version": MARVEL_CATALOGUE_VERSION,
            "builtAt": datetime.now(timezone.utc).isoformat(),
            "titlesQueried": len(MARVEL_MOVIE_SEARCHES),
            "movieCount": movie_count,
            "resultCount": len(results),
            "results": results,
        }
        _write_marvel_catalogue(payload)
        _marvel_catalogue_state.update({
            "status": "ready",
            "completed": len(MARVEL_MOVIE_SEARCHES),
            "total": len(MARVEL_MOVIE_SEARCHES),
            "resultCount": len(results),
            "movieCount": movie_count,
            "error": "",
            "builtAt": payload["builtAt"],
        })
        logger.info(
            "Built persistent Marvel catalogue: %d movie titles, %d torrent options from %d title searches",
            movie_count,
            len(results),
            len(MARVEL_MOVIE_SEARCHES),
        )
    except Exception as exc:
        _marvel_catalogue_retry_after = time.time() + 60
        _marvel_catalogue_state.update({
            "status": "failed",
            "error": "The first Marvel catalogue build failed. Please try again in a minute.",
        })
        logger.warning("Marvel catalogue build failed: %s", exc)




# Additional shared catalogues. Curated DC lists are long-lived; latest-release
# catalogues are refreshed monthly and keep serving the previous good cache during refresh.
_EXTRA_CATALOGUES: dict[str, dict[str, Any]] = {
    "dc-live-action": {
        "file": "dc_live_action_catalogue.json", "version": 3, "refresh_days": 36500,
        "minimum_movies": 5, "mode": "fixed", "language": "english",
        "titles": [
            ("Superman", 2025), ("Supergirl", 2026), ("The Batman Part II", 2027),
            ("Joker: Folie à Deux", 2024), ("The Batman", 2022), ("The Suicide Squad", 2021),
            ("Batman: The Brave and the Bold", 2028), ("Clayface", 2026),
            ("Superman: Legacy", 2025), ("The Batman - Part II", 2027),
            ("Batman Forever", 1995), ("Batman & Robin", 1997), ("Batman: Mask of the Phantasm", 1993),
            ("Superman III", 1983), ("Superman IV: The Quest for Peace", 1987),
            ("Superman: The Movie", 1978), ("Steel", 1997), ("Road to Perdition", 2002),
            ("A History of Violence", 2005), ("The Losers", 2010), ("RED", 2010),
            ("RED 2", 2013), ("The Kitchen", 2019), ("The Old Guard", 2020),
            ("The Old Guard 2", 2025), ("Stardust", 2007), ("The Crow", 1994),
            ("The Crow", 2024), ("Spawn", 1997), ("Jonah Hex", 2010),
            ("Superman II: The Richard Donner Cut", 2006), ("Watchmen: Chapter I", 2024),
            ("Watchmen: Chapter II", 2024), ("The Spirit", 2008), ("Swamp Thing", 1982),
            ("Zack Snyder's Justice League", 2021), ("Wonder Woman 1984", 2020),
            ("Birds of Prey", 2020), ("Joker", 2019), ("Shazam!", 2019),
            ("Aquaman", 2018), ("Aquaman and the Lost Kingdom", 2023),
            ("Wonder Woman", 2017), ("Justice League", 2017), ("Man of Steel", 2013),
            ("Batman v Superman: Dawn of Justice", 2016), ("Suicide Squad", 2016),
            ("Green Lantern", 2011), ("The Dark Knight Rises", 2012),
            ("The Dark Knight", 2008), ("Batman Begins", 2005), ("Batman Returns", 1992),
            ("Batman", 1989), ("Superman Returns", 2006), ("Superman II", 1980),
            ("Superman", 1978), ("Constantine", 2005), ("Watchmen", 2009),
            ("V for Vendetta", 2005), ("The League of Extraordinary Gentlemen", 2003),
            ("Catwoman", 2004), ("The Flash", 2023), ("Black Adam", 2022),
            ("Blue Beetle", 2023), ("Shazam! Fury of the Gods", 2023),
        ],
    },
    "dc-animated": {
        "file": "dc_animated_catalogue.json", "version": 1, "refresh_days": 36500,
        "minimum_movies": 5, "mode": "fixed",
        "titles": [
            ("Justice League: Crisis on Infinite Earths Part One", 2024),
            ("Justice League: Crisis on Infinite Earths Part Two", 2024),
            ("Justice League: Crisis on Infinite Earths Part Three", 2024),
            ("Justice League: Warworld", 2023), ("Legion of Super-Heroes", 2023),
            ("Batman: The Doom That Came to Gotham", 2023), ("Green Lantern: Beware My Power", 2022),
            ("Battle of the Super Sons", 2022), ("Catwoman: Hunted", 2022),
            ("Injustice", 2021), ("Batman: The Long Halloween Part Two", 2021),
            ("Batman: The Long Halloween Part One", 2021), ("Justice Society: World War II", 2021),
            ("Superman: Man of Tomorrow", 2020), ("Justice League Dark: Apokolips War", 2020),
            ("Wonder Woman: Bloodlines", 2019), ("Batman: Hush", 2019),
            ("Reign of the Supermen", 2019), ("The Death of Superman", 2018),
            ("Suicide Squad: Hell to Pay", 2018), ("Batman: Gotham by Gaslight", 2018),
            ("Justice League Dark", 2017), ("Teen Titans: The Judas Contract", 2017),
            ("Batman and Harley Quinn", 2017), ("Justice League vs. Teen Titans", 2016),
            ("Batman: Bad Blood", 2016), ("Justice League: Gods and Monsters", 2015),
            ("Batman: The Killing Joke", 2016), ("Justice League: Throne of Atlantis", 2015),
            ("Batman: Assault on Arkham", 2014), ("Son of Batman", 2014),
            ("Justice League: The Flashpoint Paradox", 2013), ("Batman: Under the Red Hood", 2010),
            ("Superman/Batman: Public Enemies", 2009), ("Wonder Woman", 2009),
            ("Justice League: The New Frontier", 2008), ("Batman: Mask of the Phantasm", 1993),
            ("Batman Beyond: Return of the Joker", 2000), ("Superman: Doomsday", 2007),
            ("All-Star Superman", 2011), ("Batman: Year One", 2011),
            ("Batman: The Dark Knight Returns Part 1", 2012), ("Batman: The Dark Knight Returns Part 2", 2013),
            ("Justice League: Doom", 2012), ("Superman vs. The Elite", 2012),
        ],
    },
    "latest-hollywood": {
        # Query a broad set of recent theatrical titles, not just the few with current swarm activity.
        "file": "latest_hollywood_catalogue.json", "version": 6, "refresh_days": 30,
        "minimum_movies": 1, "mode": "latest",
        "queries": [
            "Project Hail Mary 2026", "Disclosure Day 2026", "Mortal Kombat II 2026",
            "Supergirl 2026", "Spider Man Brand New Day 2026", "The Odyssey 2026",
            "The Super Mario Galaxy Movie 2026", "Scream 7 2026", "The Devil Wears Prada 2 2026",
            "Superman 2025", "Fantastic Four First Steps 2025", "F1 2025",
            "Jurassic World Rebirth 2025", "Sinners 2025", "A Minecraft Movie 2025",
            "Mission Impossible Final Reckoning 2025", "Thunderbolts 2025",
            "Captain America Brave New World 2025", "How to Train Your Dragon 2025",
            "Final Destination Bloodlines 2025", "Predator Badlands 2025",
            "The Conjuring Last Rites 2025", "Avatar Fire and Ash 2025",
            "Zootopia 2 2025", "Wicked For Good 2025", "Lilo and Stitch 2025",
        ],
    },
    "latest-bollywood": {
        # Search a broader set of recent Hindi films and use movie-only torrent providers.
        "file": "latest_bollywood_catalogue.json", "version": 6, "refresh_days": 30,
        "minimum_movies": 1, "mode": "latest",
        "queries": [
            "Dhurandhar The Revenge 2026", "Border 2 2026", "Drishyam The Conclusion 2026",
            "Bhooth Bangla 2026", "Dhurandhar 2026", "O Romeo 2026", "Subedaar 2026",
            "Tera Yaar Hoon Main 2026", "Happy Patel Khatarnak Jasoos 2026",
            "Ikkis 2026", "Alpha 2026", "Mardaani 3 2026",
            "Dhurandhar 2025", "Chhaava 2025", "Saiyaara 2025", "Sitaare Zameen Par 2025",
            "Raid 2 2025", "War 2 2025", "Housefull 5 2025", "Mahavatar Narsimha 2025",
            "Kesari Chapter 2 2025", "Sikandar 2025", "Bhool Chuk Maaf 2025",
            "Metro In Dino 2025", "Deva 2025", "Emergency 2025",
            "De De Pyaar De 2 2025", "Thamma 2025", "Tere Ishk Mein 2025",
        ],
    },
}
# Separate persistent caches keep English-original and Hindi/dubbed options
# independent so neither language can pollute the other catalogue.
_EXTRA_CATALOGUES["dc-live-action-hindi"] = {
    **_EXTRA_CATALOGUES["dc-live-action"],
    "file": "dc_live_action_hindi_catalogue.json",
    "version": 2,
    "language": "hindi",
    "query_suffix": "Hindi dubbed",
    "titles": list(_EXTRA_CATALOGUES["dc-live-action"]["titles"]),
}
_EXTRA_CATALOGUES["marvel-hindi"] = {
    "file": "marvel_hindi_catalogue.json",
    "version": 1,
    "refresh_days": 36500,
    "minimum_movies": 5,
    "mode": "fixed",
    "language": "hindi",
    "query_suffix": "Hindi dubbed",
    "titles": list(MARVEL_MOVIE_SEARCHES),
}

_EXTRA_CATALOGUE_PATHS = {
    key: Path(os.getenv("CATALOGUE_" + key.upper().replace("-", "_") + "_PATH", "/app/data/" + config["file"]))
    for key, config in _EXTRA_CATALOGUES.items()
}
_extra_catalogue_tasks: dict[str, asyncio.Task | None] = {key: None for key in _EXTRA_CATALOGUES}
_extra_catalogue_states: dict[str, dict[str, Any]] = {
    key: {"status": "idle", "completed": 0, "total": len(config.get("titles", config.get("queries", []))),
          "resultCount": 0, "movieCount": 0, "error": ""}
    for key, config in _EXTRA_CATALOGUES.items()
}


def _read_extra_catalogue(key: str) -> dict[str, Any] | None:
    path = _EXTRA_CATALOGUE_PATHS[key]
    config = _EXTRA_CATALOGUES[key]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") == config["version"] and isinstance(payload.get("results"), list) and payload["results"]:
            return payload
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _write_extra_catalogue(key: str, payload: dict[str, Any]) -> None:
    path = _EXTRA_CATALOGUE_PATHS[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=key + "-", suffix=".tmp",
                                         dir=str(path.parent), delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _extra_catalogue_is_stale(key: str, payload: dict[str, Any] | None) -> bool:
    if not payload:
        return True
    days = int(_EXTRA_CATALOGUES[key]["refresh_days"])
    if days >= 36500:
        return False
    try:
        built = datetime.fromisoformat(str(payload.get("builtAt") or "").replace("Z", "+00:00"))
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - built).total_seconds() >= days * 86400
    except (ValueError, TypeError):
        return True


def _extra_item_year(item: dict[str, Any], fallback: int = 0) -> int:
    try:
        if item.get("year"):
            return int(item["year"])
    except (TypeError, ValueError):
        pass
    match = re.search(r"\b((?:19|20)\d{2})\b", str(item.get("title") or ""))
    return int(match.group(1)) if match else fallback


async def _build_extra_catalogue(key: str) -> None:
    config = _EXTRA_CATALOGUES[key]
    state = _extra_catalogue_states[key]
    fixed_mode = config["mode"] == "fixed"
    jobs = list(config.get("titles", [])) if fixed_mode else [(query, 0) for query in config["queries"]]
    semaphore = asyncio.Semaphore(3)
    poster_semaphore = asyncio.Semaphore(2)
    state.update({"status": "building", "completed": 0, "total": len(jobs), "resultCount": 0, "error": ""})
    rows: dict[str, dict[str, Any]] = {}
    found_movies: set[str] = set()

    async def search_job(title: str, year: int) -> list[dict[str, Any]]:
        async with semaphore:
            query = f"{title} {year}".strip() if year else title
            query_suffix = str(config.get("query_suffix") or "").strip()
            if query_suffix:
                query = query + " " + query_suffix
            try:
                if fixed_mode:
                    try:
                        items = await asyncio.wait_for(
                            search_1337x(query, limit=50, allow_series_fallback=False),
                            timeout=18,
                        )
                    except Exception as exc:
                        logger.info("Catalogue %s primary search failed for %s: %s", key, query, exc)
                        items = []
                    if len(items) < 2:
                        try:
                            fallback = await asyncio.wait_for(search_yts_movies(query, limit=20), timeout=9)
                            items = list(items or []) + list(fallback or [])
                        except Exception as exc:
                            logger.debug("Catalogue %s YTS fallback failed for %s: %s", key, query, exc)
                else:
                    # Search each known title directly in movie listings instead of
                    # passing it through the broad result filter (which requires year
                    # and all title tokens to occur literally in provider filenames).
                    year_match = re.search(r"\b((?:19|20)\d{2})\b", query)
                    expected_year = int(year_match.group(1)) if year_match else datetime.now(timezone.utc).year
                    provider_query = re.sub(r"\b((?:19|20)\d{2})\b", "", query).strip(" .-_")
                    calls = [
                        asyncio.wait_for(
                            search_1337x_direct(
                                query,
                                limit=50,
                                pages=2,
                                category="Movies",
                                provider_query=provider_query,
                            ),
                            timeout=20,
                        ),
                        asyncio.wait_for(
                            search_limetorrents(query, limit=30, pages=1),
                            timeout=14,
                        ),
                    ]
                    if key == "latest-hollywood":
                        calls.append(asyncio.wait_for(search_yts_movies(query, limit=20), timeout=12))
                    searched = await asyncio.gather(*calls, return_exceptions=True)
                    items = []
                    for value in searched:
                        if isinstance(value, list):
                            items.extend(value)
                        elif isinstance(value, Exception):
                            logger.info(
                                "Catalogue %s provider search failed for %s: %s: %s",
                                key, query, type(value).__name__, str(value) or "<no message>",
                            )

                accepted: list[dict[str, Any]] = []
                for original in items or []:
                    if not isinstance(original, dict):
                        continue
                    item = dict(original)
                    raw_title = str(item.get("title") or "").strip()
                    if not raw_title:
                        continue
                    if fixed_mode:
                        if (
                            not _marvel_result_matches_title(item, title, year)
                            or not _extra_result_matches_language(item, config.get("language"))
                        ):
                            continue
                        media_title, media_year = title, year
                    else:
                        normalized = _normalize_title(unescape(raw_title))
                        query_year_match = re.search(r"\b((?:19|20)\d{2})\b", query)
                        expected_year = int(query_year_match.group(1)) if query_year_match else datetime.now(timezone.utc).year
                        query_title = re.sub(r"\b((?:19|20)\d{2})\b", "", query).strip(" .-_")
                        query_tokens = [
                            token for token in _normalize_title(query_title).split()
                            if token not in {"the", "a", "an", "of", "and", "for", "to", "movie", "film"}
                        ]
                        result_tokens = set(normalized.split())
                        # Roman-numbered sequels are frequently indexed with Arabic numerals.
                        matched_tokens = sum(
                            1 if token in result_tokens or (
                                token in {"ii", "iii", "iv"} and
                                ({"ii": "2", "iii": "3", "iv": "4"}[token] in result_tokens)
                            ) else 0
                            for token in query_tokens
                        )
                        minimum_matches = max(1, len(query_tokens) - 1) if len(query_tokens) > 2 else len(query_tokens)
                        if query_tokens and matched_tokens < minimum_matches:
                            continue

                        category = str(item.get("category") or "").strip().lower()
                        if (
                            category in {"tv", "television", "series", "tv series", "television series"}
                            or "television series" in category
                            or re.search(r"\b(?:S\d{1,2}E\d{1,2}|season\s+\d+|complete\s+series|episode\s+\d+|web[\s.-]?series)\b", raw_title, re.I)
                        ):
                            continue

                        current_year = datetime.now(timezone.utc).year
                        title_years = [int(value) for value in re.findall(r"\b((?:19|20)\d{2})\b", raw_title)]
                        if title_years:
                            media_year = title_years[-1]
                        else:
                            media_year = _extra_item_year(item, fallback=expected_year)
                        if media_year < current_year - 1 or media_year > current_year:
                            continue

                        # The query title is the canonical display/group title for all of its torrent variants.
                        media_title = unescape(query_title)
                    try:
                        raw_size = item.get("size_bytes") or item.get("sizeBytes") or item.get("size") or 0
                        if isinstance(raw_size, (int, float)):
                            size = int(raw_size)
                        else:
                            size = parse_size(str(raw_size))
                        seeders = int(float(str(item.get("seeders") or 0).replace(",", "")))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if size < 100 * 1024 * 1024 or size > MAX_SEARCH_RESULT_SIZE_BYTES or seeders <= 0:
                        continue
                    if not any(str(item.get(field) or "").strip() for field in ("magnetUrl", "downloadUrl", "sourceUrl", "infoUrl", "infoHash")):
                        continue
                    item["mediaTitle"] = media_title
                    item["year"] = media_year or None
                    if not item.get("posterUrl"):
                        item["posterUrl"] = _poster_url_for_release(f"{media_title} {media_year or ''}")
                    accepted.append(item)

                # Resolve one poster per movie group and reuse it across quality variants.
                grouped: dict[str, list[dict[str, Any]]] = {}
                for item in accepted:
                    group_key = _normalize_title(str(item.get("mediaTitle") or item.get("title") or ""))
                    grouped.setdefault(group_key, []).append(item)
                for group_key, variants in grouped.items():
                    if not variants:
                        continue
                    poster = str(variants[0].get("posterUrl") or "")
                    if not poster:
                        async with poster_semaphore:
                            try:
                                poster = await asyncio.wait_for(_poster_lookup(str(variants[0]["mediaTitle"]), str(variants[0].get("year") or "")), timeout=10)
                            except Exception:
                                poster = ""
                    for item in variants:
                        item["posterUrl"] = poster or _poster_url_for_release(str(item.get("mediaTitle") or item.get("title") or ""))
                        item["catalogueKey"] = key
                        item["catalogueTitle"] = str(item.get("mediaTitle") or item.get("title") or "")
                    found_movies.update(group_key + "|" + str(v.get("year") or "") for v in variants for group_key in [_normalize_title(str(v.get("mediaTitle") or v.get("title") or ""))])
                return accepted
            finally:
                state["completed"] = int(state.get("completed") or 0) + 1

    try:
        tasks = [asyncio.create_task(search_job(title, year)) for title, year in jobs]
        for task in asyncio.as_completed(tasks):
            try:
                items = await task
            except Exception as exc:
                logger.info("Catalogue %s search task failed: %s", key, exc)
                items = []
            for item in items:
                digest = str(item.get("infoHash") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", digest):
                    digest = info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
                dedupe = "hash:" + digest if re.fullmatch(r"[0-9a-f]{40}", digest, re.I) else (
                    "title-size:" + _normalize_title(str(item.get("title") or "")) + "|" + str(item.get("size") or 0)
                )
                previous = rows.get(dedupe)
                if previous is None or (int(item.get("seeders") or 0), int(item.get("leechers") or 0)) > (int(previous.get("seeders") or 0), int(previous.get("leechers") or 0)):
                    rows[dedupe] = item
            state["resultCount"] = len(rows)
            state["movieCount"] = len(found_movies)

        results = list(rows.values())
        movie_count = len({ _normalize_title(str(item.get("mediaTitle") or item.get("title") or "")) + "|" + str(item.get("year") or "") for item in results })
        if movie_count < int(config["minimum_movies"]):
            raise RuntimeError(f"Only {movie_count} titles found; keeping the previous catalogue.")
        results.sort(key=lambda item: (_extra_item_year(item), float(item.get("rating") or 0), int(item.get("seeders") or 0)), reverse=True)
        payload = {"version": config["version"], "builtAt": datetime.now(timezone.utc).isoformat(),
                   "titlesQueried": len(jobs), "movieCount": movie_count, "resultCount": len(results), "results": results}
        _write_extra_catalogue(key, payload)
        state.update({"status": "ready", "completed": len(jobs), "total": len(jobs), "movieCount": movie_count,
                      "resultCount": len(results), "builtAt": payload["builtAt"], "error": ""})
        logger.info("Built %s catalogue: %d titles / %d torrent options", key, movie_count, len(results))
    except Exception as exc:
        state.update({
            "status": "failed",
            "error": "Catalogue refresh failed; previous cached results were retained.",
            "debugError": f"{type(exc).__name__}: {str(exc)[:240]}",
        })
        logger.exception("Catalogue %s build failed: %s", key, exc)


async def _ensure_extra_catalogue_refresh(key: str) -> None:
    cached = _read_extra_catalogue(key)
    if _extra_catalogue_is_stale(key, cached):
        task = _extra_catalogue_tasks.get(key)
        if task is None or task.done():
            _extra_catalogue_tasks[key] = asyncio.create_task(_build_extra_catalogue(key))


async def _extra_catalogue_monthly_scheduler() -> None:
    # Check once per day; a stale latest catalogue is refreshed in the background.
    while True:
        for key, config in _EXTRA_CATALOGUES.items():
            if int(config["refresh_days"]) < 36500:
                await _ensure_extra_catalogue_refresh(key)
        await asyncio.sleep(24 * 60 * 60)



def _tmdb_cache_disk_key(cache_key: tuple[str, int]) -> str:
    return cache_key[0] + ":" + str(cache_key[1])


def _read_tmdb_persistent_cache(cache_key: tuple[str, int]) -> tuple[float, dict[str, Any]] | None:
    try:
        payload = json.loads(TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
        row = (payload.get("catalogues") or {}).get(_tmdb_cache_disk_key(cache_key))
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return None
        saved_at = float(row.get("savedAt") or 0)
        if saved_at <= 0:
            return None
        return saved_at, row["payload"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_tmdb_persistent_cache(cache_key: tuple[str, int], payload: dict[str, Any]) -> None:
    try:
        TMDB_CATALOGUE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            disk = json.loads(TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(disk, dict):
                disk = {}
        except (OSError, ValueError, TypeError):
            disk = {}
        catalogues = disk.get("catalogues")
        if not isinstance(catalogues, dict):
            catalogues = {}
        catalogues[_tmdb_cache_disk_key(cache_key)] = {
            "savedAt": time.time(),
            "payload": payload,
        }
        # Keep the file bounded if an unusual client requests many pages.
        if len(catalogues) > 300:
            ordered = sorted(
                catalogues.items(),
                key=lambda pair: float((pair[1] or {}).get("savedAt") or 0),
                reverse=True,
            )
            catalogues = dict(ordered[:300])
        disk["catalogues"] = catalogues
        temp_path = TMDB_CATALOGUE_CACHE_FILE.with_name(
            TMDB_CATALOGUE_CACHE_FILE.name + "." + uuid.uuid4().hex + ".tmp"
        )
        temp_path.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, TMDB_CATALOGUE_CACHE_FILE)
    except (OSError, ValueError, TypeError) as exc:
        # Caching is best-effort; it must never turn a successful API response into a failure.
        logger.info("Could not write TMDB persistent cache: %s", exc)


def _tmdb_ott_disk_key(cache_key: tuple[int, str]) -> str:
    return str(cache_key[0]) + ":" + cache_key[1]


def _read_tmdb_ott_persistent_cache(cache_key: tuple[int, str]) -> tuple[float, dict[str, Any]] | None:
    try:
        payload = json.loads(TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
        row = (payload.get("items") or {}).get(_tmdb_ott_disk_key(cache_key))
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return None
        saved_at = float(row.get("savedAt") or 0)
        if saved_at <= 0:
            return None
        return saved_at, row["payload"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_tmdb_ott_persistent_cache(cache_key: tuple[int, str], payload: dict[str, Any]) -> None:
    try:
        TMDB_OTT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            disk = json.loads(TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(disk, dict):
                disk = {}
        except (OSError, ValueError, TypeError):
            disk = {}
        items = disk.get("items")
        if not isinstance(items, dict):
            items = {}
        items[_tmdb_ott_disk_key(cache_key)] = {
            "savedAt": time.time(),
            "payload": payload,
        }
        # Bound cache growth as users browse many different movie titles.
        if len(items) > 1500:
            ordered = sorted(
                items.items(),
                key=lambda pair: float((pair[1] or {}).get("savedAt") or 0),
                reverse=True,
            )
            items = dict(ordered[:1500])
        disk["items"] = items
        temp_path = TMDB_OTT_CACHE_FILE.with_name(
            TMDB_OTT_CACHE_FILE.name + "." + uuid.uuid4().hex + ".tmp"
        )
        temp_path.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, TMDB_OTT_CACHE_FILE)
    except (OSError, ValueError, TypeError) as exc:
        # A cache write failure must not hide otherwise usable availability data.
        logger.info("Could not write TMDB OTT cache: %s", exc)


def _tmdb_watch_provider_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("provider_name") or "").strip()
        provider_id = item.get("provider_id")
        if not name:
            continue
        identity = str(provider_id if provider_id is not None else name.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        logo_path = str(item.get("logo_path") or "").strip()
        rows.append({
            "providerId": provider_id,
            "name": name,
            "logoUrl": "https://image.tmdb.org/t/p/w92" + logo_path if logo_path else "",
        })
    return rows


async def _tmdb_fetch_movie_ott_availability(movie_id: int, region: str) -> dict[str, Any]:
    # Provider availability and regional release dates are separate TMDB resources.
    # Failure to fetch release dates should not discard a successful provider result.
    provider_result, release_result = await asyncio.gather(
        _tmdb_get_json(f"movie/{movie_id}/watch/providers"),
        _tmdb_get_json(f"movie/{movie_id}/release_dates"),
        return_exceptions=True,
    )
    if isinstance(provider_result, Exception):
        if isinstance(provider_result, HTTPException):
            raise provider_result
        raise HTTPException(status_code=502, detail="Could not load OTT provider availability.") from provider_result

    if isinstance(release_result, Exception):
        logger.info("Could not load digital release dates for TMDB movie %s: %s", movie_id, release_result)
        release_payload: dict[str, Any] = {}
    else:
        release_payload = release_result if isinstance(release_result, dict) else {}

    all_regions = (provider_result.get("results") or {}) if isinstance(provider_result, dict) else {}
    region_payload = all_regions.get(region) or {}
    streaming = (
        _tmdb_watch_provider_rows(region_payload.get("flatrate"))
        + _tmdb_watch_provider_rows(region_payload.get("free"))
        + _tmdb_watch_provider_rows(region_payload.get("ads"))
    )
    rent = _tmdb_watch_provider_rows(region_payload.get("rent"))
    buy = _tmdb_watch_provider_rows(region_payload.get("buy"))

    def merge_providers(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            for provider in group:
                identity = str(provider.get("providerId") or provider.get("name", "").casefold())
                if identity not in seen:
                    seen.add(identity)
                    merged.append(provider)
        return merged

    streaming = merge_providers([streaming])
    rent = merge_providers([rent])
    buy = merge_providers([buy])

    digital_dates: list[str] = []
    theatrical_dates: list[str] = []
    for region_release in release_payload.get("results") or []:
        if not isinstance(region_release, dict) or str(region_release.get("iso_3166_1") or "").upper() != region:
            continue
        for release in region_release.get("release_dates") or []:
            if not isinstance(release, dict):
                continue
            release_type = str(release.get("type") or "")
            if release_type not in {"2", "3", "4"}:
                continue
            day = str(release.get("release_date") or "").strip()[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                try:
                    datetime.strptime(day, "%Y-%m-%d")
                    if release_type == "4":
                        digital_dates.append(day)
                    else:
                        # Types 2 and 3 are limited and wide theatrical releases.
                        theatrical_dates.append(day)
                except ValueError:
                    continue

    digital_release_date = min(digital_dates) if digital_dates else None
    theatrical_release_date = min(theatrical_dates) if theatrical_dates else None
    today = datetime.now(timezone.utc).date().isoformat()
    if streaming:
        status = "streaming"
    elif rent or buy:
        status = "rent_buy"
    elif digital_release_date and digital_release_date > today:
        status = "digital_scheduled"
    elif digital_release_date:
        # A listed digital release date does not guarantee subscription streaming.
        status = "digital_release_known"
    else:
        status = "not_confirmed"

    provider_link = str(region_payload.get("link") or "").strip()
    if not provider_link:
        provider_link = f"https://www.themoviedb.org/movie/{movie_id}/watch?locale={region}"

    return {
        "movieId": movie_id,
        "region": region,
        "status": status,
        "streamingProviders": streaming,
        "rentProviders": rent,
        "buyProviders": buy,
        "theatricalReleaseDate": theatrical_release_date,
        "digitalReleaseDate": digital_release_date,
        "providerLink": provider_link,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "source": "TMDB",
        "providerAttribution": "Streaming availability data powered by JustWatch.",
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }


async def _tmdb_get_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    global _tmdb_api_connect_failure_until

    if not TMDB_READ_ACCESS_TOKEN and not TMDB_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "TMDB API credentials are not configured. Add TMDB_READ_ACCESS_TOKEN "
                "or TMDB_API_KEY to the server environment to enable paginated movie discovery."
            ),
        )

    cooldown_remaining = _tmdb_api_connect_failure_until - time.monotonic()
    if cooldown_remaining > 0:
        retry_in = max(1, int(cooldown_remaining + 0.999))
        raise HTTPException(
            status_code=503,
            detail=f"TMDB API connections are temporarily failing from this VPS; skipping retries for about {retry_in}s.",
        )

    query = dict(params or {})
    headers = {"Accept": "application/json"}
    if TMDB_READ_ACCESS_TOKEN:
        headers["Authorization"] = "Bearer " + TMDB_READ_ACCESS_TOKEN
    else:
        query["api_key"] = TMDB_API_KEY

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(12.0, connect=4.0),
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(f"{TMDB_API_BASE}/{path.lstrip('/')}", params=query)

            if response.status_code in {401, 403}:
                raise HTTPException(
                    status_code=503,
                    detail="TMDB rejected the configured credentials. Check TMDB_API_KEY or TMDB_READ_ACCESS_TOKEN.",
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    logger.warning(
                        "TMDB temporary HTTP %s for %s (attempt %s/3)",
                        response.status_code, path, attempt + 1,
                    )
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                if response.status_code == 429:
                    raise HTTPException(
                        status_code=503,
                        detail="TMDB rate-limited this request after retries. Please wait briefly and try again.",
                    )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=502, detail="TMDB returned an unexpected response.")
            return payload
        except HTTPException:
            raise
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning(
                "TMDB network request failed for %s (attempt %s/3): %s: %s",
                path, attempt + 1, type(exc).__name__, str(exc) or "<no message>",
            )
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
                _tmdb_api_connect_failure_until = max(
                    _tmdb_api_connect_failure_until,
                    time.monotonic() + TMDB_API_CONNECT_FAILURE_COOLDOWN_SECONDS,
                )
                logger.warning(
                    "TMDB API connection failure; opening %ss cooldown to avoid repeated retries",
                    TMDB_API_CONNECT_FAILURE_COOLDOWN_SECONDS,
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "TMDB API connection was reset before an HTTP response. "
                        "Requests are paused briefly to avoid repeated slow failures; cached catalogues will be used when available."
                    ),
                ) from exc
            if attempt < 2:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
        except httpx.HTTPStatusError as exc:
            logger.warning("TMDB returned HTTP %s for %s", exc.response.status_code, path)
            raise HTTPException(status_code=502, detail=f"TMDB returned HTTP {exc.response.status_code}.") from exc
        except ValueError as exc:
            logger.warning("TMDB returned invalid JSON for %s: %s", path, exc)
            raise HTTPException(status_code=502, detail="TMDB returned an invalid response. Try again shortly.") from exc

    logger.warning(
        "TMDB request exhausted retries for %s: %s",
        path,
        (type(last_error).__name__ + ": " + str(last_error)) if last_error else "upstream failure",
    )
    raise HTTPException(
        status_code=502,
        detail="TMDB is temporarily unreachable from the VPS after three attempts. Please try again shortly.",
    )


async def _tmdb_discovered_company_ids(franchise: str) -> list[str]:
    normalized = franchise.strip().lower()
    now = time.monotonic()
    cached = _tmdb_company_id_cache.get(normalized)
    if cached and now - cached[0] < TMDB_COMPANY_CACHE_SECONDS:
        return cached[1]

    task = _tmdb_company_inflight.get(normalized)
    if task is None or task.done():
        task = asyncio.create_task(_tmdb_load_company_ids(normalized))
        _tmdb_company_inflight[normalized] = task
    try:
        return await task
    finally:
        if task.done() and _tmdb_company_inflight.get(normalized) is task:
            _tmdb_company_inflight.pop(normalized, None)


async def _tmdb_load_company_ids(franchise: str) -> list[str]:
    if franchise == "marvel":
        ids = {"420", "19551", "38679", "2301", "13252"}
        queries = ("Marvel Studios", "Marvel Entertainment", "Marvel Animation", "Marvel Productions")
    else:
        ids = {"9993"}
        queries = ("DC Entertainment", "DC Films", "DC Comics")

    async def search_company(query: str) -> list[str]:
        try:
            payload = await _tmdb_get_json("search/company", {"query": query, "page": 1})
        except HTTPException as exc:
            logger.info("TMDB company lookup failed for %s: %s", query, exc.detail)
            return []
        found: list[str] = []
        for row in payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            company_id = row.get("id")
            if not name or not isinstance(company_id, (int, str)):
                continue
            lower_name = name.casefold()
            if franchise == "marvel" and "marvel" not in lower_name:
                continue
            if franchise == "dc" and not (
                "dc entertainment" in lower_name
                or "dc films" in lower_name
                or "dc comics" in lower_name
                or lower_name.startswith("dc ")
            ):
                continue
            found.append(str(company_id))
        return found

    discovered = await asyncio.gather(*(search_company(query) for query in queries))
    for values in discovered:
        ids.update(values)
    result = sorted(ids, key=lambda value: (value not in {"420", "9993"}, int(value)))[:20]
    _tmdb_company_id_cache[franchise] = (time.monotonic(), result)
    return result


def _tmdb_release_date_is_released(value: Any, today: str) -> bool:
    """Return True only when a real ISO release date is on or before today's UTC date."""
    release_text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_text):
        return False
    try:
        release_date = datetime.strptime(release_text, "%Y-%m-%d").date()
        cutoff_date = datetime.strptime(today, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return release_date <= cutoff_date


async def _tmdb_fetch_movie_catalogue(catalogue_key: str, page: int, released_only: bool = True) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    params: dict[str, Any] = {
        "include_adult": "false",
        "include_video": "false",
        "language": "en-US",
        "page": page,
        "sort_by": "primary_release_date.desc",
    }
    if released_only:
        params["release_date.lte"] = today

    if catalogue_key in {"latest-hollywood", "popular-hollywood"}:
        params["with_original_language"] = "en"
        if catalogue_key == "popular-hollywood":
            params["sort_by"] = "popularity.desc"
    elif catalogue_key in {"latest-bollywood", "popular-bollywood"}:
        params["with_original_language"] = "hi"
        params["with_origin_country"] = "IN"
        if catalogue_key == "popular-bollywood":
            params["sort_by"] = "popularity.desc"
    elif catalogue_key in {"marvel", "dc-live-action", "dc-animated"}:
        franchise = "marvel" if catalogue_key == "marvel" else "dc"
        company_ids = await _tmdb_discovered_company_ids(franchise)
        params["with_companies"] = "|".join(company_ids)
        if catalogue_key == "dc-animated":
            params["with_genres"] = "16"
        elif catalogue_key == "dc-live-action":
            params["without_genres"] = "16"
    elif catalogue_key in {"trending-hollywood", "trending-bollywood"}:
        # TMDB's trending endpoint is ranked globally, so collect several upstream
        # pages and filter by original language here to keep the category useful for
        # Hollywood and Hindi cinema rather than showing unrelated languages.
        wanted_language = "en" if catalogue_key == "trending-hollywood" else "hi"
        source_pages_per_page = 6
        source_start = (page - 1) * source_pages_per_page + 1
        source_page_numbers = range(source_start, min(source_start + source_pages_per_page, 501))
        requests = [
            _tmdb_get_json("trending/movie/week", {"page": source_page, "language": "en-US"})
            for source_page in source_page_numbers
        ]
        payloads = await asyncio.gather(*requests, return_exceptions=True)
        successful_payloads = [value for value in payloads if isinstance(value, dict)]
        if not successful_payloads:
            failure = next((value for value in payloads if isinstance(value, Exception)), None)
            if isinstance(failure, HTTPException):
                raise failure
            raise HTTPException(status_code=502, detail="TMDB trending feed could not be reached.")

        raw_results = []
        seen_ids: set[str] = set()
        total_source_pages = 1
        for payload in successful_payloads:
            total_source_pages = max(total_source_pages, int(payload.get("total_pages") or 1))
            for item in payload.get("results") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("original_language") or "").lower() != wanted_language:
                    continue
                if released_only and not _tmdb_release_date_is_released(item.get("release_date"), today):
                    continue
                item_id = str(item.get("id") or "")
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                raw_results.append(item)

        results: list[dict[str, Any]] = []
        for item in raw_results:
            title = str(item.get("title") or item.get("original_title") or "").strip()
            if not title:
                continue
            release_date = str(item.get("release_date") or "").strip()
            year_match = re.match(r"^(\d{4})", release_date)
            poster_path = str(item.get("poster_path") or "").strip()
            backdrop_path = str(item.get("backdrop_path") or "").strip()
            results.append({
                "id": item.get("id"),
                "title": title,
                "year": int(year_match.group(1)) if year_match else None,
                "releaseDate": release_date,
                "overview": str(item.get("overview") or "").strip(),
                "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "",
                "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
                "rating": float(item.get("vote_average") or 0),
                "genres": [int(genre) for genre in (item.get("genre_ids") or []) if str(genre).isdigit()],
                "source": "TMDB",
            })

        return {
            "catalogue": catalogue_key,
            "provider": "TMDB Weekly Trending",
            "page": page,
            "totalPages": max(1, min(500, (total_source_pages + source_pages_per_page - 1) // source_pages_per_page)),
            # Trending does not expose a language-filtered total. This is the count collected
            # for the requested page, not a claim about every matching title in TMDB.
            "totalResults": len(results),
            "results": results,
            "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
        }
    else:
        raise HTTPException(status_code=404, detail="Unknown movie catalogue.")

    payload = await _tmdb_get_json("discover/movie", params)
    raw_results = payload.get("results") or []
    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("original_title") or "").strip()
        if not title:
            continue
        release_date = str(item.get("release_date") or "").strip()
        if released_only and not _tmdb_release_date_is_released(release_date, today):
            continue
        year_match = re.match(r"^(\d{4})", release_date)
        poster_path = str(item.get("poster_path") or "").strip()
        backdrop_path = str(item.get("backdrop_path") or "").strip()
        results.append({
            "id": item.get("id"),
            "title": title,
            "year": int(year_match.group(1)) if year_match else None,
            "releaseDate": release_date,
            "overview": str(item.get("overview") or "").strip(),
            "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "",
            "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
            "rating": float(item.get("vote_average") or 0),
            "genres": [int(genre) for genre in (item.get("genre_ids") or []) if str(genre).isdigit()],
            "source": "TMDB",
        })

    total_pages = max(1, min(500, int(payload.get("total_pages") or 1)))
    current_page = max(1, min(500, int(payload.get("page") or page)))
    return {
        "catalogue": catalogue_key,
        "releasedOnly": released_only,
        "provider": "TMDB",
        "page": current_page,
        "totalPages": total_pages,
        "totalResults": int(payload.get("total_results") or len(results)),
        "results": results,
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }


@app.post("/api/movies/catalogue/cache/clear")
async def api_clear_tmdb_movie_caches():
    """Clear all TMDB catalogue, OTT and company-discovery caches for a fresh lookup."""
    global _tmdb_cache_clear_last_at

    now = time.monotonic()
    # This endpoint is available to Home visitors, so keep a small global cooldown
    # to prevent accidental repeated cache flushes from exhausting the TMDB quota.
    if now - _tmdb_cache_clear_last_at < 15:
        raise HTTPException(status_code=429, detail="TMDB cache was cleared recently. Wait a few seconds before clearing it again.")
    _tmdb_cache_clear_last_at = now

    tasks = list(_tmdb_movie_catalogue_inflight.values()) + list(_tmdb_ott_inflight.values()) + list(_tmdb_company_inflight.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    _tmdb_movie_catalogue_cache.clear()
    _tmdb_movie_catalogue_inflight.clear()
    _tmdb_ott_cache.clear()
    _tmdb_ott_inflight.clear()
    _tmdb_company_id_cache.clear()
    _tmdb_company_inflight.clear()

    removed_files: list[str] = []
    for cache_file in (TMDB_CATALOGUE_CACHE_FILE, TMDB_OTT_CACHE_FILE):
        try:
            cache_file.unlink(missing_ok=True)
            removed_files.append(cache_file.name)
        except OSError as exc:
            logger.exception("Could not delete TMDB cache file %s", cache_file)
            raise HTTPException(
                status_code=500,
                detail="In-memory TMDB caches were cleared, but a persistent cache file could not be deleted: " + cache_file.name,
            ) from exc

    logger.info("Cleared all TMDB caches: %s", ", ".join(removed_files))
    return {
        "status": "cleared",
        "cacheTypes": ["movie catalogues", "OTT availability", "company discovery"],
        "persistentFiles": removed_files,
        "refetchRequired": True,
    }


@app.get("/api/movies/catalogue/{catalogue_key}")
async def api_tmdb_movie_catalogue(
    catalogue_key: str,
    page: int = Query(1, ge=1, le=500),
    released_only: bool = Query(True),
):
    allowed = {
        "latest-hollywood", "latest-bollywood",
        "popular-hollywood", "popular-bollywood",
        "trending-hollywood", "trending-bollywood",
        "marvel", "dc-live-action", "dc-animated",
    }
    if catalogue_key not in allowed:
        raise HTTPException(status_code=404, detail="Unknown movie catalogue.")

    # Keep released-only and include-upcoming responses in separate cache slots.
    cache_key = (catalogue_key + ("|released-v3" if released_only else "|upcoming-v2"), page)
    now = time.monotonic()
    cache_ttl = 30 * 60 if catalogue_key.startswith("trending-") else TMDB_CATALOGUE_CACHE_SECONDS
    cached = _tmdb_movie_catalogue_cache.get(cache_key)
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    # A disk cache survives container restarts, unlike the in-memory TTL cache.
    persistent = _read_tmdb_persistent_cache(cache_key)
    if persistent:
        saved_at, persistent_payload = persistent
        age_seconds = max(0.0, time.time() - saved_at)
        if age_seconds < cache_ttl:
            _tmdb_movie_catalogue_cache[cache_key] = (
                time.monotonic() - age_seconds,
                persistent_payload,
            )
            return persistent_payload

    task = _tmdb_movie_catalogue_inflight.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(_tmdb_fetch_movie_catalogue(catalogue_key, page, released_only))
        _tmdb_movie_catalogue_inflight[cache_key] = task
    try:
        payload = await task
        _tmdb_movie_catalogue_cache[cache_key] = (time.monotonic(), payload)
        _write_tmdb_persistent_cache(cache_key, payload)
        return payload
    except HTTPException as exc:
        # Prefer a stale response over an empty screen when TMDB has an outage.
        stale_payload = cached[1] if cached else (persistent[1] if persistent else None)
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = exc.detail
            logger.warning("Serving stale TMDB catalogue %s page %s after upstream failure", catalogue_key, page)
            return stale_response
        raise
    except Exception as exc:
        logger.exception("TMDB movie catalogue failed for %s page %s", catalogue_key, page)
        stale_payload = cached[1] if cached else (persistent[1] if persistent else None)
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = "TMDB is temporarily unavailable; showing the last cached catalogue."
            return stale_response
        raise HTTPException(status_code=502, detail="Could not load the movie catalogue from TMDB.") from exc
    finally:
        if task.done() and _tmdb_movie_catalogue_inflight.get(cache_key) is task:
            _tmdb_movie_catalogue_inflight.pop(cache_key, None)


@app.get("/api/movies/ott/{movie_id}")
async def api_tmdb_movie_ott_availability(
    movie_id: int,
    region: str = Query("IN", min_length=2, max_length=2),
):
    """Return cached streaming, rental, purchase and digital-release data for one movie."""
    if movie_id < 1:
        raise HTTPException(status_code=404, detail="Unknown movie.")

    region = region.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", region):
        raise HTTPException(status_code=422, detail="Region must be a two-letter country code.")

    cache_key = (movie_id, region)
    now = time.monotonic()
    cached = _tmdb_ott_cache.get(cache_key)
    if cached and now - cached[0] < TMDB_OTT_CACHE_SECONDS:
        return cached[1]

    persistent = _read_tmdb_ott_persistent_cache(cache_key)
    if persistent:
        saved_at, persistent_payload = persistent
        age_seconds = max(0.0, time.time() - saved_at)
        if age_seconds < TMDB_OTT_CACHE_SECONDS:
            _tmdb_ott_cache[cache_key] = (time.monotonic() - age_seconds, persistent_payload)
            return persistent_payload

    # Only use recent stale results during an outage; never retain old availability indefinitely.
    max_stale_seconds = 14 * 24 * 60 * 60
    stale_payload = None
    if cached and now - cached[0] <= max_stale_seconds:
        stale_payload = cached[1]
    elif persistent and time.time() - persistent[0] <= max_stale_seconds:
        stale_payload = persistent[1]
    task = _tmdb_ott_inflight.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(_tmdb_fetch_movie_ott_availability(movie_id, region))
        _tmdb_ott_inflight[cache_key] = task
    try:
        payload = await task
        _tmdb_ott_cache[cache_key] = (time.monotonic(), payload)
        _write_tmdb_ott_persistent_cache(cache_key, payload)
        return payload
    except HTTPException as exc:
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = exc.detail
            return stale_response
        raise
    except Exception as exc:
        logger.exception("TMDB OTT availability failed for movie %s in %s", movie_id, region)
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = "OTT availability is temporarily unavailable; showing the last cached result."
            return stale_response
        raise HTTPException(status_code=502, detail="Could not load OTT availability from TMDB.") from exc
    finally:
        if task.done() and _tmdb_ott_inflight.get(cache_key) is task:
            _tmdb_ott_inflight.pop(cache_key, None)


@app.get("/api/catalogue/extra/{catalogue_key}")
async def api_extra_catalogue(catalogue_key: str):
    if catalogue_key not in _EXTRA_CATALOGUES:
        raise HTTPException(404, "Unknown catalogue")
    cached = _read_extra_catalogue(catalogue_key)
    await _ensure_extra_catalogue_refresh(catalogue_key)
    state = _extra_catalogue_states[catalogue_key]
    # For monthly catalogues, stale-but-useful data is returned immediately while
    # the refresh runs; first-ever builds report progress for the UI to poll.
    if cached:
        return {"status": "ready", "builtAt": cached.get("builtAt"),
                "completed": int(cached.get("titlesQueried") or 0), "total": int(cached.get("titlesQueried") or 0),
                "resultCount": int(cached.get("resultCount") or len(cached["results"])),
                "movieCount": int(cached.get("movieCount") or 0), "refreshing": bool(_extra_catalogue_tasks.get(catalogue_key) and not _extra_catalogue_tasks[catalogue_key].done()),
                "results": cached["results"]}
    return {**state, "results": []}


async def _warm_extra_catalogues_on_startup() -> None:
    for key in _EXTRA_CATALOGUES:
        await _ensure_extra_catalogue_refresh(key)
    asyncio.create_task(_extra_catalogue_monthly_scheduler())


app.router.add_event_handler("startup", _warm_extra_catalogues_on_startup)


@app.get("/api/catalogue/marvel")
async def api_marvel_catalogue():
    # A completed catalogue is served straight from the persistent shared file:
    # no provider calls and no per-visitor refreshes.
    cached = _read_marvel_catalogue()
    if cached:
        return {
            "status": "ready",
            "builtAt": cached.get("builtAt"),
            "completed": int(cached.get("titlesQueried") or len(MARVEL_MOVIE_SEARCHES)),
            "total": int(cached.get("titlesQueried") or len(MARVEL_MOVIE_SEARCHES)),
            "resultCount": int(cached.get("resultCount") or len(cached["results"])),
            "movieCount": int(cached.get("movieCount") or 0),
            "results": cached["results"],
        }

    global _marvel_catalogue_task
    if _marvel_catalogue_task is None or _marvel_catalogue_task.done():
        if _marvel_catalogue_state.get("status") == "failed" and time.time() < _marvel_catalogue_retry_after:
            return {**_marvel_catalogue_state, "results": []}
        _marvel_catalogue_state.update({
            "status": "building",
            "completed": 0,
            "total": len(MARVEL_MOVIE_SEARCHES),
            "resultCount": 0,
            "error": "",
        })
        _marvel_catalogue_task = asyncio.create_task(_build_marvel_catalogue())

    return {
        **_marvel_catalogue_state,
        "total": len(MARVEL_MOVIE_SEARCHES),
        "results": [],
    }



async def _warm_marvel_catalogue_on_startup() -> None:
    """Start the one-time build during deployment, not only after a user clicks."""
    global _marvel_catalogue_task
    if _read_marvel_catalogue():
        logger.info("Persistent Marvel catalogue is already cached; startup warm-up skipped.")
        return
    if _marvel_catalogue_task is None or _marvel_catalogue_task.done():
        if _marvel_catalogue_state.get("status") == "failed" and time.time() < _marvel_catalogue_retry_after:
            return
        _marvel_catalogue_state.update({
            "status": "building",
            "completed": 0,
            "total": len(MARVEL_MOVIE_SEARCHES),
            "resultCount": 0,
            "error": "",
        })
        _marvel_catalogue_task = asyncio.create_task(_build_marvel_catalogue())
        logger.info("Started first-time Marvel catalogue warm-up in the background.")


app.router.add_event_handler("startup", _warm_marvel_catalogue_on_startup)


@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=50)):
    return await search_1337x(q, limit)

@app.get("/api/health")
async def api_health():
    return {"name": APP_NAME, "status": "ok"}

@app.get("/api/seedr/session")
async def seedr_session(request: Request):
    """Initialize/restore the browser session and expose only the CSRF token."""
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)
    token = current_seedr_token()
    connected = False

    if token:
        try:
            token_ctx = _seedr_request_token.set(token)
            try:
                await seedr_request("/tasks" if _seedr_session_auth_mode(session_id) == "pat" else "/user")
            finally:
                _seedr_request_token.reset(token_ctx)
            connected = True
        except Exception:
            _clear_seedr_session_token(session_id)

    return {
        "connected": connected,
        "csrfToken": str(session.get("csrf_token") or ""),
    }


async def _validate_seedr_pat(token: str) -> None:
    token = normalize_seedr_token(token)
    if not token:
        raise SeedrError("SEEDR_PAT_MISSING", 400, "A Seedr Personal Access Token is required.")

    endpoints = (
        f"{SEEDR_PAT_BASE}/fs/root/contents",
        f"{SEEDR_PAT_BASE}/tasks",
        f"{SEEDR_V2_BASE}/fs/root/contents",
        f"{SEEDR_V2_BASE}/tasks",
    )
    saw_unauthorized = False

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for url in endpoints:
            try:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                logger.info("Seedr PAT validation transport error: %s", exc)
                continue

            raw = response.text
            if 200 <= response.status_code < 300:
                logger.info("Seedr PAT validation succeeded")
                return

            if response.status_code == 401:
                saw_unauthorized = True
                continue

            if response.status_code in {404, 405, 403}:
                continue

            try:
                data = response.json() if raw else {}
                reason = str(
                    data.get("error_description")
                    or data.get("reason_phrase")
                    or data.get("message")
                    or data.get("error")
                    or ""
                ).strip()
            except Exception:
                reason = ""

            raise SeedrError(
                "SEEDR_PAT_REJECTED",
                502,
                reason or f"Seedr PAT validation failed (HTTP {response.status_code}).",
            )

    if saw_unauthorized:
        raise SeedrError(
            "SEEDR_PAT_REJECTED",
            401,
            "Seedr rejected the Personal Access Token. Copy a fresh PAT from Seedr and try again.",
        )

    raise SeedrError(
        "SEEDR_PAT_VALIDATION_FAILED",
        502,
        "Seedr could not validate the Personal Access Token right now.",
    )


@app.post("/api/seedr/connect/pat")
async def seedr_connect_pat(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)

    try:
        payload = await request.json()
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be JSON.")

    raw_pat = payload.get("pat") or payload.get("token")
    if not isinstance(raw_pat, str):
        raise HTTPException(400, "A Seedr Personal Access Token is required.")

    pat = normalize_seedr_token(raw_pat)
    if not pat:
        raise HTTPException(400, "A Seedr Personal Access Token is required.")

    await _validate_seedr_pat(pat)

    session["access_token"] = pat
    session["refresh_token"] = ""
    session["token_type"] = "Bearer"
    session["auth_mode"] = "pat"
    session["device"] = None
    session["last_seen"] = time.time()

    logger.info(
        "Seedr PAT connected: session=%s token_length=%s",
        _seedr_session_fingerprint(session_id),
        len(pat),
    )
    return {"status": "connected", "connected": True}


@app.post("/api/seedr/connect/start")
async def seedr_connect_start(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)

    logger.info(
        "Seedr connect start: session=%s cookie_present=%s has_device=%s has_token=%s",
        _seedr_session_fingerprint(session_id),
        bool(request.cookies.get(SEEDR_SESSION_COOKIE)),
        bool(isinstance(session.get("device"), dict)),
        bool(_seedr_session_token(session_id)),
    )

    token = current_seedr_token()
    if token:
        return {"status": "connected", "connected": True}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(
                SEEDR_DEVICE_CODE_URL,
                params={"client_id": SEEDR_DEVICE_CLIENT_ID},
                headers={"Accept": "application/json"},
            )
        raw = response.text
        try:
            data = response.json() if raw else {}
        except Exception:
            data = {}

        if response.status_code >= 400 or not isinstance(data, dict):
            detail = seedr_error_message(response.status_code, data, raw)
            raise SeedrError("SEEDR_DEVICE_CODE_FAILED", 502, detail)

        device_code = str(data.get("device_code") or data.get("deviceCode") or "").strip()
        user_code = str(data.get("user_code") or data.get("userCode") or "").strip()
        verification_url = str(
            data.get("verification_url")
            or data.get("verificationUrl")
            or "https://www.seedr.cc/devices"
        ).strip()
        expires_in = int(float(data.get("expires_in") or data.get("expiresIn") or 900))

        if not device_code or not user_code:
            raise SeedrError(
                "SEEDR_DEVICE_CODE_INVALID",
                502,
                "Seedr returned an incomplete device authorization response.",
            )

        session["device"] = {
            "device_code": device_code,
            "user_code": user_code,
            "verification_url": verification_url,
            "expires_at": time.time() + max(60, expires_in),
            "created_at": time.time(),
        }
        session["last_seen"] = time.time()

        poll_interval = int(float(data.get("interval") or 5))
        poll_interval = max(2, min(30, poll_interval))
        session["device"]["interval"] = poll_interval
        logger.info(
            "Seedr connect start stored device: session=%s expires_in=%s interval=%s",
            _seedr_session_fingerprint(session_id),
            expires_in,
            poll_interval,
        )
        return {
            "status": "pending",
            "connected": False,
            "userCode": user_code,
            "verificationUrl": verification_url,
            "expiresIn": expires_in,
            "interval": poll_interval,
        }
    except SeedrError:
        raise
    except Exception as exc:
        logger.warning("Seedr device-code request failed: %s", exc)
        raise SeedrError(
            "SEEDR_DEVICE_CODE_FAILED",
            502,
            "Could not start Seedr account authorization right now.",
        ) from exc


def _extract_seedr_access_token(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("access_token", "accessToken", "token"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                normalized = normalize_seedr_token(candidate)
                if normalized:
                    return normalized

        for key in ("data", "result", "response"):
            nested = payload.get(key)
            token = _extract_seedr_access_token(nested)
            if token:
                return token
    return ""


@app.get("/api/seedr/connect/status")
async def seedr_connect_status(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)
    token = _seedr_session_token(session_id)
    device = session.get("device")

    logger.info(
        "Seedr connect status: session=%s cookie_present=%s has_device=%s has_token=%s",
        _seedr_session_fingerprint(session_id),
        bool(request.cookies.get(SEEDR_SESSION_COOKIE)),
        bool(isinstance(device, dict)),
        bool(token),
    )

    if token:
        try:
            token_ctx = _seedr_request_token.set(token)
            try:
                await seedr_request("/user")
            finally:
                _seedr_request_token.reset(token_ctx)
            session["device"] = None
            return {"status": "connected", "connected": True}
        except SeedrError as exc:
            if exc.status_code == 401:
                _clear_seedr_session_token(session_id)
            else:
                return {"status": "pending", "connected": False}

    device = session.get("device")
    if not isinstance(device, dict):
        return {
            "status": "error",
            "connected": False,
            "code": "SEEDR_SESSION_LOST",
            "message": "The Seedr connection session was lost. Start the connection again.",
        }

    expires_at = float(device.get("expires_at") or 0)
    if expires_at and time.time() >= expires_at:
        session["device"] = None
        return {
            "status": "expired",
            "connected": False,
            "message": "The Seedr authorization code expired. Start a new connection.",
        }

    device_code = str(device.get("device_code") or "").strip()
    if not device_code:
        session["device"] = None
        return {
            "status": "error",
            "connected": False,
            "message": "Seedr did not provide a usable device code.",
        }

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            # Seedr's device authorization endpoint expects the original
            # device_code. The current Seedr client implementations pass only
            # device_code here; do not add client_id to this request.
            response = await client.get(
                SEEDR_DEVICE_AUTHORIZE_URL,
                params={"device_code": device_code},
                headers={"Accept": "application/json"},
            )

        raw = response.text
        try:
            data = response.json() if raw else {}
        except Exception:
            data = {}

        access_token = _extract_seedr_access_token(data)
        logger.info(
            "Seedr device authorization poll: http=%s token_present=%s error=%s message=%s",
            response.status_code,
            bool(access_token),
            str(data.get("error") or data.get("code") or "")[:120] if isinstance(data, dict) else "",
            str(
                (data.get("error_description") or data.get("message") or data.get("error") or "")
                if isinstance(data, dict) else ""
            )[:200],
        )
        if access_token:
            refresh_token = ""
            token_type = "Bearer"
            if isinstance(data, dict):
                refresh_token = str(data.get("refresh_token") or data.get("refreshToken") or "").strip()
                token_type = str(data.get("token_type") or "Bearer").strip() or "Bearer"
                for key in ("data", "result", "response"):
                    nested = data.get(key)
                    if isinstance(nested, dict):
                        refresh_token = refresh_token or str(
                            nested.get("refresh_token") or nested.get("refreshToken") or ""
                        ).strip()
                        token_type = str(nested.get("token_type") or token_type).strip() or token_type

            session["access_token"] = access_token
            session["refresh_token"] = refresh_token
            session["token_type"] = token_type
            session["device"] = None
            session["last_seen"] = time.time()

            token_ctx = _seedr_request_token.set(access_token)
            try:
                await seedr_request("/user")
            except SeedrError:
                _clear_seedr_session_token(session_id)
                raise
            finally:
                _seedr_request_token.reset(token_ctx)

            return {"status": "connected", "connected": True}

        error_code = ""
        error_message = ""
        if isinstance(data, dict):
            error_code = str(data.get("error") or data.get("code") or "").strip().lower()
            error_message = str(
                data.get("error_description")
                or data.get("message")
                or data.get("error")
                or ""
            ).strip()

        pending = (
            response.status_code in {400, 409, 428}
            and (
                not error_code
                or "pending" in error_code
                or "authorize" in error_code
                or "not_authorized" in error_code
                or "not approved" in error_message.lower()
            )
        )
        if pending:
            return {
                "status": "pending",
                "connected": False,
                "expiresIn": max(0, int(expires_at - time.time())),
                "interval": max(2, min(30, int(float(device.get("interval") or 5)))),
            }

        if response.status_code >= 400:
            if response.status_code == 401:
                session["device"] = None
                return {
                    "status": "error",
                    "connected": False,
                    "message": "Seedr rejected this authorization request.",
                }
            return {
                "status": "error",
                "connected": False,
                "message": error_message or seedr_error_message(response.status_code, data, raw),
            }

        return {
            "status": "pending",
            "connected": False,
            "expiresIn": max(0, int(expires_at - time.time())),
        }
    except SeedrError:
        raise
    except Exception as exc:
        logger.info("Seedr device authorization poll failed: %s", exc)
        return {
            "status": "pending",
            "connected": False,
            "expiresIn": max(0, int(expires_at - time.time())),
        }


@app.post("/api/seedr/connect/disconnect")
async def seedr_connect_disconnect(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    _clear_seedr_session_token(session_id)
    return {"status": "disconnected", "connected": False}


@app.get("/api/seedr/token-diagnostic")
async def seedr_token_diagnostic():
    if not current_seedr_token():
        return {
            "configured": False,
            "code": "SEEDR_TOKEN_MISSING",
            "tokenLength": 0,
            "tokenFingerprint": None,
            "checks": {},
        }

    token = normalize_seedr_token(current_seedr_token())
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else None

    async def check(path: str) -> dict[str, Any]:
        try:
            await seedr_request(path)
            return {"ok": True, "status": 200}
        except SeedrError as exc:
            return {"ok": False, "status": exc.status_code, "code": exc.code, "message": exc.detail}
        except Exception as exc:
            return {"ok": False, "status": 0, "code": "LOCAL_ERROR", "message": str(exc)[:200]}

    return {
        "configured": True,
        "code": "SEEDR_TOKEN_PRESENT",
        "tokenLength": len(token),
        "tokenFingerprint": fingerprint,
        "checks": {
            "user": await check("/tasks" if _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat" else "/user"),
        },
    }


@app.get("/api/seedr/auth-status")
async def seedr_auth_status():
    if not current_seedr_token():
        return {
            "configured": False,
            "authenticated": False,
            "code": "SEEDR_TOKEN_MISSING",
            "message": "Seedr API token is not configured in Render.",
        }

    try:
        # The old working integration treats a successful /user call as the
        # authentication test. Do the same here; /user is explicitly documented
        # by Seedr for Bearer-authenticated requests.
        await seedr_request(
            "/tasks" if _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat" else "/user"
        )
        return {
            "configured": True,
            "authenticated": True,
            "code": "SEEDR_AUTH_OK",
        }
    except SeedrError as exc:
        return {
            "configured": True,
            "authenticated": False,
            "code": exc.code,
            "message": exc.detail,
        }



@app.get("/api/seedr/quota")
async def seedr_quota():
    if not current_seedr_token():
        return {"configured": False, "maxSpace": 0, "usedSpace": 0, "remainingSpace": 0}

    try:
        is_pat = _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat"
        result = seedr_data(await seedr_request("/fs/root/contents" if is_pat else "/user"))
        storage = result.get("account", {}).get("storage", {}) if isinstance(result, dict) else {}
        if not isinstance(storage, dict):
            storage = result.get("storage", {}) if isinstance(result, dict) else {}
        result_dict = result if isinstance(result, dict) else {}
        if is_pat:
            max_space = int(float(
                result_dict.get("space_max", 0)
                or result_dict.get("max_space", 0)
                or result_dict.get("maxSpace", 0)
                or storage.get("limit", 0)
            ))
            used = int(float(
                result_dict.get("space_used", 0)
                or result_dict.get("used_space", 0)
                or result_dict.get("usedSpace", 0)
                or storage.get("used", 0)
            ))
        else:
            max_space = int(float(
                storage.get("limit")
                or storage.get("max_space")
                or storage.get("maxSpace")
                or result_dict.get("max_space", 0)
                or result_dict.get("space_max", 0)
                or 0
            ))
            used = int(float(
                storage.get("used")
                or storage.get("used_space")
                or storage.get("usedSpace")
                or result_dict.get("used_space", 0)
                or result_dict.get("space_used", 0)
                or 0
            ))
    except SeedrError:
        raise
    except Exception as exc:
        logger.warning("Seedr quota parsing failed: %s", exc)
        raise SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            503,
            "Seedr account storage information is temporarily unavailable.",
        ) from exc

    if not (max_space > 0) or used < 0 or used > max_space:
        raise SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            503,
            "Seedr account storage information is temporarily unavailable.",
        )

    return {
        "configured": True,
        "maxSpace": max_space,
        "usedSpace": used,
        "remainingSpace": max(0, max_space - used),
    }

async def _prepare_seedr_space(
    required_bytes: int,
    *,
    force_one: bool = False,
) -> list[dict[str, Any]]:
    """Free old completed Seedr folders; optionally force at least one deletion after a quota rejection."""
    required = max(0, int(required_bytes or 0))
    if required <= 0:
        return []

    quota = await seedr_quota()
    remaining = int(quota.get("remainingSpace") or 0)
    if remaining >= required and not force_one:
        return []

    library = await get_seedr_metadata_tree(force_refresh=True)
    folders = [
        folder for folder in (library.get("folders") or [])
        if isinstance(folder, dict)
        and str(folder.get("folderId") or folder.get("id") or "").strip()
        and str(folder.get("folderId") or folder.get("id") or "").strip() != "0"
    ]

    # Prefer actual Seedr task timestamps so the oldest completed torrent is
    # removed first. Fall back to the library order when a provider response
    # does not expose a timestamp.
    task_meta: dict[str, dict[str, Any]] = {}
    active_folder_ids: set[str] = set()
    try:
        tasks_payload = seedr_data(await seedr_request("/tasks"))
        for raw_task in arr(tasks_payload, ("tasks", "torrents", "items")):
            task = unwrap_seedr_task(seedr_data(raw_task))
            if not task:
                continue
            folder_id = seedr_task_folder_id(task)
            if not folder_id:
                continue
            if not task_complete(task):
                active_folder_ids.add(str(folder_id))
            task_meta[str(folder_id)] = task
    except (HTTPException, SeedrError):
        pass

    def task_timestamp(task: dict[str, Any]) -> float:
        for key in (
            "created_at", "createdAt", "created", "added_at", "addedAt",
            "time_added", "timeAdded", "timestamp", "date_added", "dateAdded",
        ):
            value = task.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value if value < 10_000_000_000 else value / 1000)
            if isinstance(value, str) and value.strip():
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return parsed.timestamp()
                except Exception:
                    continue
        return 0.0

    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for index, folder in enumerate(folders):
        folder_id = str(folder.get("folderId") or folder.get("id") or "").strip()
        if not folder_id or folder_id in active_folder_ids:
            continue
        task = task_meta.get(folder_id, {})
        candidates.append((index, task_timestamp(task), folder))

    candidates.sort(key=lambda item: (item[1] if item[1] > 0 else float("inf"), item[0]))

    deleted: list[dict[str, Any]] = []
    for _, _, folder in candidates:
        if remaining >= required and (not force_one or deleted):
            break

        folder_id = str(folder.get("folderId") or folder.get("id") or "").strip()
        folder_name = str(folder.get("torrentName") or folder.get("name") or folder_id).strip()
        folder_size = max(0, int(float(folder.get("totalSize") or 0)))

        try:
            await seedr_request(f"/fs/folder/{quote(folder_id)}", "DELETE")
            deleted.append({
                "folderId": folder_id,
                "name": folder_name,
                "size": folder_size,
            })
            remaining += folder_size
            _seedr_folder_cache.pop(folder_id, None)
        except (HTTPException, SeedrError) as exc:
            logger.warning(
                "Seedr automatic cleanup skipped folder=%s status=%s detail=%s",
                folder_id,
                getattr(exc, "status_code", 0),
                getattr(exc, "detail", str(exc)),
            )

    global _seedr_metadata_cache
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()

    if remaining < required:
        raise SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            413,
            "Seedr does not have enough space for this torrent, even after removing older completed files.",
        )

    logger.info(
        "Seedr automatic cleanup freed=%s required=%s deleted=%s",
        sum(int(item.get("size") or 0) for item in deleted),
        required,
        len(deleted),
    )
    return deleted


@app.post("/api/seedr/add")
async def seedr_add(request: Request):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    # Direct Seedr mode:
    # validate without changing the input, then forward that exact string.
    try:
        payload = await request.json()
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be JSON.")

    raw_magnet = payload.get("magnet")
    if not isinstance(raw_magnet, str) or not raw_magnet:
        raise HTTPException(400, "A magnet link is required.")

    if not info_hash(raw_magnet):
        raise HTTPException(400, "A valid BTIH magnet link is required")

    requested_folder = str(payload.get("folder_id") or "").strip()
    folder = int(requested_folder) if requested_folder.isdigit() else 0
    auto_cleanup = bool(payload.get("auto_cleanup"))
    required_bytes = max(0, int(float(payload.get("required_bytes") or 0)))

    session_id = _seedr_request_session_id.get().strip()
    logger.info(
        "Seedr add start: session=%s mode=%s magnet_length=%s magnet_info_hash=%s "
        "requested_folder=%s resolved_folder=%s",
        _seedr_session_fingerprint(session_id),
        _seedr_session_auth_mode(session_id),
        len(raw_magnet),
        info_hash(raw_magnet),
        requested_folder or "<none>",
        folder,
    )

    # Treat every new Seedr add as replacing the prior unfinished transfer.
    # Do not depend on frontend localStorage/current-notice state: an old task
    # may still exist after a page refresh or backend/container restart.
    replace_task_id = str(payload.get("replace_task_id") or "").strip()
    cancelled_tasks = await _cancel_active_seedr_tasks_for_replacement(replace_task_id)

    deleted_folders: list[dict[str, Any]] = [
        {
            "folderId": str(item.get("folderId") or ""),
            "name": str(item.get("name") or "Cancelled Seedr task"),
            "size": max(0, int(item.get("size") or 0)),
        }
        for item in cancelled_tasks
        if item.get("folderDeleted") and str(item.get("folderId") or "").strip()
    ]
    if auto_cleanup and required_bytes > 0:
        deleted_folders.extend(await _prepare_seedr_space(required_bytes))

    try:
        task = unwrap_seedr_task(await add_task(raw_magnet, folder))
    except SeedrError as exc:
        reason = str(exc.detail or "").lower()
        is_storage_shortage = (
            exc.status_code == 413
            or "not enough space" in reason
            or "insufficient storage" in reason
            or "insufficient space" in reason
        )
        if not (is_storage_shortage and auto_cleanup and required_bytes > 0):
            logger.exception(
                "Seedr add failed: session=%s mode=%s code=%s status=%s detail=%s",
                _seedr_session_fingerprint(session_id),
                _seedr_session_auth_mode(session_id),
                exc.code,
                exc.status_code,
                exc.detail,
            )
            raise

        # Seedr can reject a new magnet even when its quota endpoint appears to
        # have enough free bytes (for example, while an old transfer is stuck
        # reserving space). Cancelled tasks are already removed; now delete at
        # least one oldest completed folder and retry the same requested magnet
        # exactly once.
        logger.warning(
            "Seedr reported insufficient space after preflight; reclaiming one more completed folder before retry: required=%s",
            required_bytes,
        )
        deleted_folders.extend(await _prepare_seedr_space(required_bytes, force_one=True))
        try:
            task = unwrap_seedr_task(await add_task(raw_magnet, folder))
        except SeedrError as retry_exc:
            logger.exception(
                "Seedr retry after storage cleanup failed: session=%s code=%s status=%s detail=%s",
                _seedr_session_fingerprint(session_id),
                retry_exc.code,
                retry_exc.status_code,
                retry_exc.detail,
            )
            raise
    except Exception as exc:
        logger.exception(
            "Seedr add unexpected failure: session=%s mode=%s exception_type=%s exception=%s",
            _seedr_session_fingerprint(session_id),
            _seedr_session_auth_mode(session_id),
            type(exc).__name__,
            str(exc)[:2000],
        )
        raise
    tid = task_id(task)
    if not tid:
        raise HTTPException(502, "Seedr did not return a task id")

    task_folder_id = seedr_task_folder_id(task)
    task_name = seedr_task_name(task) or f"Torrent {tid}"
    schedule_seedr_cleanup(str(tid), task_name, task_folder_id)

    logger.info(
        "Seedr add success: session=%s mode=%s task_id=%s folder_id=%s task_name=%s",
        _seedr_session_fingerprint(session_id),
        _seedr_session_auth_mode(session_id),
        tid,
        task_folder_id or "<none>",
        task_name[:300],
    )

    return {
        "backend": "seedr",
        "task_id": int(tid) if tid.isdigit() else tid,
        "id": int(tid) if tid.isdigit() else tid,
        "torrent_name": task_name,
        "folder_id": task_folder_id,
        "deleted_folders": deleted_folders,
        "cancelled_tasks": cancelled_tasks,
    }

def _seedr_file_folder_id(files: list[dict[str, Any]]) -> str:
    """Return the actual torrent folder id exposed by completed file rows."""
    for file in files:
        folder_id = str(file.get("folderId") or "").strip()
        if folder_id:
            return folder_id
    return ""


def _seedr_effective_task_folder_id(
    task: dict[str, Any],
    files: list[dict[str, Any]] | None = None,
) -> str:
    """Prefer the created torrent folder over the destination parent folder."""
    task_folder_id = seedr_task_folder_id(task)
    file_folder_id = _seedr_file_folder_id(files or [])

    if file_folder_id and (
        not task_folder_id
        or file_folder_id != task_folder_id
        or task_folder_id == "0"
    ):
        return file_folder_id
    return task_folder_id


def _remember_seedr_cleanup_folder(tid: str, folder_id: str) -> None:
    folder_id = str(folder_id or "").strip()
    if not folder_id:
        return
    job = _seedr_cleanup_jobs.get(str(tid).strip())
    if not job:
        return
    if str(job.get("folderId") or "").strip() == folder_id:
        return
    job["folderId"] = folder_id
    _save_seedr_cleanup_jobs()


@app.get("/api/seedr/tasks/{tid}/progress")
async def seedr_task_progress(tid: str):
    """Fast polling endpoint: only fetch task state/progress from Seedr."""
    try:
        raw = seedr_data(await seedr_request(f"/tasks/{quote(tid)}"))
    except HTTPException as exc:
        if exc.status_code == 404:
            return {
                "taskId": tid,
                "status": "not_found",
                "progress": 0,
                "name": "",
                "folderId": "",
            }
        raise

    task = (
        raw.get("task")
        if isinstance(raw, dict) and isinstance(raw.get("task"), dict)
        else (raw if isinstance(raw, dict) else {})
    )
    progress = float(task.get("progress") or 0)
    complete = task_complete(task)
    if complete:
        progress = 100

    state = str(task.get("state") or task.get("status") or "").lower()
    status = "completed" if complete else (
        "waiting" if state in {"queued", "pending", "waiting", "paused", "stopped"} else "downloading"
    )

    task_folder_id = seedr_task_folder_id(task)
    completed_files: list[dict[str, Any]] = []
    if complete:
        try:
            completed_files = await task_contents(tid)
        except (HTTPException, SeedrError):
            completed_files = []

    effective_folder_id = _seedr_effective_task_folder_id(task, completed_files)
    task_id_value = str(tid).strip()

    canonical_name = str(
        _seedr_torrent_names_by_task.get(task_id_value)
        or _seedr_torrent_names.get(effective_folder_id)
        or seedr_task_name(task)
        or ""
    ).strip()

    if effective_folder_id and canonical_name:
        _seedr_torrent_names[effective_folder_id] = canonical_name
        if complete:
            _remember_seedr_cleanup_folder(task_id_value, effective_folder_id)
        # Seedr can expose the folder only after the task starts. Rename at
        # that point, rather than only immediately after /tasks POST.
        # Do not call Seedr folder-management endpoints from the free-account
        # progress path. The task state itself is sufficient for polling.

    seedr_task_display_name = canonical_name
    return {
        "taskId": tid,
        "status": status,
        "progress": progress,
        "name": seedr_task_display_name,
        "folderId": effective_folder_id,
    }

@app.get("/api/seedr/tasks/{tid}")
async def seedr_task(tid: str):
    try:
        raw = seedr_data(await seedr_request(f"/tasks/{quote(tid)}"))
    except HTTPException as exc:
        if exc.status_code == 404:
            return {"taskId": tid, "status": "not_found", "progress": 0, "files": [], "downloadUrl": None}
        raise
    task = raw.get("task") if isinstance(raw, dict) and isinstance(raw.get("task"), dict) else (raw if isinstance(raw, dict) else {})
    progress = float(task.get("progress") or 0)
    complete = task_complete(task)
    if complete:
        progress = 100
    files = await task_contents(tid)
    folder_id = _seedr_effective_task_folder_id(task, files)
    _remember_seedr_cleanup_folder(str(tid).strip(), folder_id)
    task_id_value = str(tid).strip()
    canonical_name = str(
        _seedr_torrent_names_by_task.get(task_id_value)
        or _seedr_torrent_names.get(folder_id)
        or seedr_task_name(task)
        or ""
    ).strip()
    if folder_id and canonical_name:
        previous_name = _seedr_torrent_names.get(folder_id)
        _seedr_torrent_names[folder_id] = canonical_name
        if previous_name != canonical_name:
            try:
                await rename_seedr_folder(folder_id, canonical_name)
            except Exception:
                pass
            global _seedr_metadata_cache
            _seedr_metadata_cache = None
    folderNameValue = canonical_name or (await folder_name(folder_id) if folder_id else "")
    for f in files:
        f["folderPath"] = "/Torrent Studio" + ("/" + folderNameValue if folderNameValue else "")
        # Do not resolve direct Seedr URLs while polling/finalizing a task.
        # A fresh URL is generated only by an explicit download/copy/stream action.
        f["url"] = None
    return {"taskId": tid, "name": str(task.get("title") or task.get("name") or ""), "folderName": folderNameValue, "folderId": folder_id, "status": "completed" if complete else "downloading", "progress": progress, "task": task, "files": files, "downloadUrl": None}

async def seedr_folder_payload(folder_id: str) -> dict[str, Any]:
    """Fetch one Seedr folder level, with a short-lived in-process cache."""
    folder_id = str(folder_id).strip()
    if not folder_id:
        return {}

    now = asyncio.get_running_loop().time()
    cached = _seedr_folder_cache.get(folder_id)
    if cached and now - cached[0] < SEEDR_FOLDER_CACHE_SECONDS:
        return cached[1]

    async with _seedr_folder_semaphore:
        # Re-check after waiting for the semaphore so concurrent callers do
        # not issue duplicate Seedr requests for the same folder.
        now = asyncio.get_running_loop().time()
        cached = _seedr_folder_cache.get(folder_id)
        if cached and now - cached[0] < SEEDR_FOLDER_CACHE_SECONDS:
            return cached[1]

        try:
            # Root access is inconsistent across Seedr API variants/accounts.
            # Prefer the normal bearer-authenticated filesystem endpoint, then
            # fall back to the dedicated root endpoint and finally the legacy
            # list_contents endpoint. A failure in one variant must not be
            # presented as a token failure when another variant works.
            if folder_id == "0":
                root_errors: list[str] = []

                try:
                    payload = seedr_data(await seedr_request("/fs/folder/0/contents"))
                    logger.info("Seedr library root resolved via bearer fs endpoint")
                except (HTTPException, SeedrError) as exc:
                    root_errors.append(f"fs:{getattr(exc, 'status_code', 0)}")
                    try:
                        payload = seedr_data(await seedr_root_request())
                        logger.info("Seedr library root resolved via dedicated root endpoint")
                    except (HTTPException, SeedrError) as root_exc:
                        root_errors.append(f"root:{getattr(root_exc, 'status_code', 0)}")
                        try:
                            payload = seedr_data(await legacy_seedr_list_contents("0"))
                            logger.info("Seedr library root resolved via legacy list_contents endpoint")
                        except (HTTPException, SeedrError) as legacy_exc:
                            root_errors.append(f"legacy:{getattr(legacy_exc, 'status_code', 0)}")
                            logger.warning(
                                "Seedr library root resolution failed across all API variants: %s",
                                ",".join(root_errors),
                            )
                            raise legacy_exc
            else:
                payload = seedr_data(await seedr_request(f"/fs/folder/{quote(folder_id)}/contents"))
        except HTTPException as exc:
            if exc.status_code == 404:
                return {}
            raise

    result = payload if isinstance(payload, dict) else {}
    _seedr_folder_cache[folder_id] = (asyncio.get_running_loop().time(), result)
    return result


def direct_folder_summary(folder_id: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    files = arr(payload, ("files", "items"))
    folders = arr(payload, ("folders", "directories"))
    total_size = 0
    valid_files = 0
    for raw in files:
        if not isinstance(raw, dict):
            continue
        try:
            size = int(float(raw.get("size") or 0))
        except Exception:
            size = 0
        total_size += max(0, size)
        if str(raw.get("id") or raw.get("file_id") or "").strip():
            valid_files += 1

    return {
        "id": str(folder_id),
        "folderId": str(folder_id),
        "name": Path(path.rstrip("/")).name or "Root Files",
        "path": path,
        "filesCount": valid_files,
        "totalSize": total_size,
        "folderCount": len([x for x in folders if isinstance(x, dict)]),
    }


async def build_seedr_metadata_tree(
    folder_id: str,
    path: str,
    folder_name_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Build the first library response from the root and its immediate child
    folders. Child contents are fetched concurrently so folder cards already
    have exact file counts/sizes when the browser receives this response.
    """
    folder_name_overrides = folder_name_overrides or {}
    payload = await seedr_folder_payload(folder_id)
    summary = direct_folder_summary(folder_id, path, payload)

    child_entries: list[tuple[str, str, dict[str, Any]]] = []
    for raw in arr(payload, ("folders", "directories")):
        if not isinstance(raw, dict):
            continue
        child_id = str(raw.get("id") or raw.get("folder_id") or "").strip()
        if not child_id:
            continue
        folder_override = str(folder_name_overrides.get(child_id) or "").strip()
        child_name = (
            folder_override
            or str(raw.get("name") or raw.get("title") or child_id).strip()
            or child_id
        )
        child_path = path.rstrip("/") + "/" + child_name
        child_entries.append((child_id, child_name, raw))

    async def load_child(entry: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
        child_id, child_name, raw = entry
        child_payload = await seedr_folder_payload(child_id)

        # Older Seedr folders can be exposed with their raw 40-character
        # identifier as the visible name. For a single-file torrent we can
        # recover the human title from the actual file name and normalize the
        # folder once, so existing downloads also get fixed on refresh.
        effective_child_name = child_name
        child_files = arr(child_payload, ("files", "items"))
        if (
            re.fullmatch(r"[0-9a-fA-F]{40}", effective_child_name)
            and len(child_files) == 1
            and isinstance(child_files[0], dict)
        ):
            raw_file_name = str(
                child_files[0].get("name")
                or child_files[0].get("title")
                or ""
            ).strip()
            inferred_name = Path(raw_file_name.replace("\\", "/")).name
            inferred_name = Path(inferred_name).stem.strip()
            if inferred_name:
                try:
                    renamed = await rename_seedr_folder(child_id, inferred_name)
                except Exception:
                    renamed = False
                _seedr_torrent_names[child_id] = inferred_name
                effective_child_name = inferred_name
                if renamed:
                    logger.info(
                        "Renamed legacy hash-named Seedr folder %s to %s",
                        child_id,
                        inferred_name,
                    )

        child_summary = direct_folder_summary(
            child_id,
            path.rstrip("/") + "/" + effective_child_name,
            child_payload,
        )

        # Some Seedr responses expose size/count directly on the folder item;
        # use those only when the contents endpoint did not provide a value.
        if child_summary["filesCount"] == 0:
            for key in ("files_count", "file_count", "filesCount", "fileCount", "count"):
                value = raw.get(key)
                if value is not None:
                    try:
                        child_summary["filesCount"] = max(0, int(value))
                        break
                    except (TypeError, ValueError):
                        pass

        if child_summary["totalSize"] == 0:
            for key in ("size", "total_size", "totalSize"):
                value = raw.get(key)
                if value is not None:
                    try:
                        child_summary["totalSize"] = max(0, int(float(value)))
                        break
                    except (TypeError, ValueError):
                        pass

        child_summary["torrentName"] = (
            _seedr_torrent_names.get(child_id)
            or folder_name_overrides.get(child_id)
            or (effective_child_name if re.fullmatch(r"[0-9a-fA-F]{40}", child_name) is None else "")
            or ""
        )
        child_summary["folderCount"] = len(
            [x for x in arr(child_payload, ("folders", "directories")) if isinstance(x, dict)]
        )
        return child_summary

    children: list[dict[str, Any]] = []
    if child_entries:
        results = await asyncio.gather(
            *(load_child(entry) for entry in child_entries),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, dict):
                children.append(result)

    # The top-level library badge represents all files in its visible torrent
    # folders, not just files placed directly in the root.
    summary["filesCount"] += sum(int(item.get("filesCount") or 0) for item in children)
    summary["totalSize"] += sum(int(item.get("totalSize") or 0) for item in children)
    summary["folderCount"] = len(children)

    return summary, children


async def get_seedr_metadata_tree(force_refresh: bool = False) -> dict[str, Any]:
    global _seedr_metadata_cache, _seedr_metadata_task

    if not current_seedr_token():
        return {"configured": False, "root": None, "folders": []}

    now = asyncio.get_running_loop().time()

    if force_refresh:
        # A completed Seedr task can become visible in the filesystem a few
        # seconds after the metadata cache was populated. Clear both caches so
        # the completion flow can see the new folder immediately.
        _seedr_metadata_cache = None
        _seedr_folder_cache.clear()

    if _seedr_metadata_cache and now - _seedr_metadata_cache[0] < SEEDR_METADATA_CACHE_SECONDS:
        return _seedr_metadata_cache[1]

    if _seedr_metadata_task is not None and not _seedr_metadata_task.done():
        return await _seedr_metadata_task

    async def build() -> dict[str, Any]:
        # Seedr's account root is exposed through a dedicated endpoint.
        # Keep SEEDR_LIBRARY_FOLDER_ID as an optional override for deployments
        # that want to start from a specific existing folder.
        root = "0"

        # Resolve human-readable torrent names from Seedr task metadata in one
        # call. The folder contents/counts and task list are independent, so
        # fetch them concurrently without adding a serial round trip.
        folder_name_overrides: dict[str, str] = dict(_seedr_torrent_names)
        task_folders: list[tuple[str, str]] = []
        try:
            tasks_payload = seedr_data(await seedr_request("/tasks"))
            for raw_task in arr(tasks_payload, ("tasks", "torrents", "items")):
                task = unwrap_seedr_task(seedr_data(raw_task))
                if not task:
                    continue

                task_folder_id = seedr_task_folder_id(task)
                task_name = seedr_task_name(task)

                if task_folder_id:
                    task_folders.append((task_folder_id, task_name))

                if task_folder_id and task_name:
                    # The title supplied by our search/add flow is canonical.
                    # Seedr's task title can contain provider/site prefixes
                    # (for example "www.UIndex.org - ..."), so never let task
                    # metadata overwrite an existing canonical title.
                    if task_folder_id not in _seedr_torrent_names:
                        _seedr_torrent_names[task_folder_id] = task_name
                    folder_name_overrides[task_folder_id] = (
                        _seedr_torrent_names.get(task_folder_id) or task_name
                    )
        except (HTTPException, SeedrError):
            # Folder metadata remains usable even when task-name lookup fails.
            pass

        try:
            root_summary, children = await build_seedr_metadata_tree(
                root,
                "/Torrent Studio",
                folder_name_overrides,
            )
        except (HTTPException, SeedrError) as exc:
            # Some Seedr tokens can authenticate and manage transfers while
            # denying the account-root filesystem listing. Do not turn that
            # provider limitation into a library-wide error. Reconstruct the
            # visible library from the task list and each task's own contents.
            if getattr(exc, "status_code", 0) not in {401, 403}:
                raise

            # First collect folder IDs from the task list. Seedr may expose
            # the created folder only after a task has started/completed, so
            # also resolve task contents below when the task object has no
            # folder_created_id.
            task_candidates: list[dict[str, Any]] = []
            try:
                tasks_payload = seedr_data(await seedr_request("/tasks"))
                for raw_task in arr(tasks_payload, ("tasks", "torrents", "items")):
                    task = unwrap_seedr_task(seedr_data(raw_task))
                    if task:
                        task_candidates.append(task)
            except (HTTPException, SeedrError):
                task_candidates = []

            unique_folders: dict[str, str] = {}

            for task in task_candidates:
                tid = task_id(task)
                task_name = (
                    seedr_task_name(task)
                    or (folder_name_overrides.get(seedr_task_folder_id(task)) if seedr_task_folder_id(task) else "")
                    or f"Torrent {tid}"
                ).strip()
                folder_id = seedr_task_folder_id(task)

                # If the task itself does not expose its created folder, the
                # task contents endpoint often does. This is the same endpoint
                # already used by the completion/progress flow.
                if tid and (not folder_id or folder_id == "0"):
                    try:
                        task_files = await task_contents(tid)
                        folder_id = _seedr_file_folder_id(task_files)
                    except (HTTPException, SeedrError):
                        folder_id = ""

                if folder_id and folder_id != root:
                    unique_folders.setdefault(
                        folder_id,
                        folder_name_overrides.get(folder_id)
                        or _seedr_torrent_names.get(folder_id)
                        or task_name
                        or folder_id,
                    )

            # Preserve any folder IDs already learned from task metadata.
            for folder_id, task_name in task_folders:
                if folder_id and folder_id != root:
                    unique_folders.setdefault(
                        folder_id,
                        folder_name_overrides.get(folder_id)
                        or _seedr_torrent_names.get(folder_id)
                        or task_name
                        or folder_id,
                    )

            async def load_task_folder(folder_id: str, name: str) -> dict[str, Any] | None:
                try:
                    payload = await seedr_folder_payload(folder_id)
                except (HTTPException, SeedrError):
                    return None
                if not payload:
                    return None
                summary = direct_folder_summary(
                    folder_id,
                    "/Torrent Studio/" + name,
                    payload,
                )
                summary["torrentName"] = name
                summary["folderCount"] = len(
                    [x for x in arr(payload, ("folders", "directories")) if isinstance(x, dict)]
                )
                return summary

            if unique_folders:
                results = await asyncio.gather(
                    *(load_task_folder(folder_id, name) for folder_id, name in unique_folders.items()),
                    return_exceptions=True,
                )
                children = [result for result in results if isinstance(result, dict)]
            else:
                children = []

            # A denied root listing is a capability limitation, not an
            # authentication failure. Returning an empty but valid library
            # keeps Search/Prepare/Stream usable even for an account with no
            # folders exposed through the token.
            root_summary = {
                "id": root,
                "folderId": root,
                "name": "Torrent Studio",
                "path": "/Torrent Studio",
                "filesCount": sum(int(item.get("filesCount") or 0) for item in children),
                "totalSize": sum(int(item.get("totalSize") or 0) for item in children),
                "folderCount": len(children),
            }

        return {
            "configured": True,
            "root": root_summary,
            "folders": children,
        }

    _seedr_metadata_task = asyncio.create_task(build())
    try:
        result = await _seedr_metadata_task
        _seedr_metadata_cache = (asyncio.get_running_loop().time(), result)
        return result
    finally:
        _seedr_metadata_task = None


@app.get("/api/seedr/library")
async def seedr_library_metadata(fresh: bool = Query(False)):
    return await get_seedr_metadata_tree(force_refresh=fresh)


@app.get("/api/seedr/folders/{folder_id}/contents")
async def seedr_folder_contents(folder_id: str):
    if not current_seedr_token():
        return {"configured": False, "folderId": folder_id, "files": [], "folders": []}

    payload = await seedr_folder_payload(folder_id)
    files: list[dict[str, Any]] = []
    for raw in arr(payload, ("files", "items")):
        file = normalize_file(raw, folder_id)
        file["url"] = None
        files.append(file)

    folders: list[dict[str, Any]] = []
    for raw in arr(payload, ("folders", "directories")):
        if not isinstance(raw, dict):
            continue
        child_id = str(raw.get("id") or raw.get("folder_id") or "").strip()
        if not child_id:
            continue
        child_name = str(raw.get("name") or raw.get("title") or child_id).strip() or child_id
        folders.append({"id": child_id, "folderId": child_id, "name": child_name})

    return {"configured": True, "folderId": folder_id, "files": files, "folders": folders}


@app.get("/api/seedr/files")
async def seedr_files():
    # Backwards-compatible full file endpoint. New UI code uses
    # /api/seedr/library + /api/seedr/folders/{id}/contents instead.
    if not current_seedr_token():
        return {"configured": False, "files": []}

    root = "0"

    result = await collect_folder(root, "/Torrent Studio")
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result:
        item_id = str(item.get("id") or "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            unique.append(item)

    return {"configured": True, "files": unique}

@app.get("/api/seedr/files/{file_id}/download/url")
async def seedr_file_download_url(file_id: str):
    # Resolve a fresh direct Seedr URL only when the user explicitly requests it.
    return await download_url(file_id)

@app.get("/api/seedr/files/{file_id}/download")
async def seedr_file_download(file_id: str):
    # Redirect the browser to Seedr's temporary direct URL so large downloads
    # do not flow through the Render Free instance.
    result = await download_url(file_id)
    return RedirectResponse(result["url"], status_code=307)


@app.get("/api/seedr/files/{file_id}/download/direct")
async def seedr_file_download_direct(
    file_id: str,
    filename: str = Query(""),
):
    """Stream a Seedr file with an explicit browser download filename."""
    result = await download_url(file_id)
    filename_value = _safe_download_filename(
        filename or result.get("name") or "",
        f"seedr-file-{file_id}",
    )
    body, content_type, headers = await _stream_seedr_download(result["url"])
    ascii_name = filename_value.encode("ascii", errors="ignore").decode("ascii").strip() or f"seedr-file-{file_id}"
    headers["Content-Disposition"] = (
        'attachment; filename="' + ascii_name.replace('"', "_") + '"'
        + "; filename*=UTF-8''" + quote(filename_value, safe="")
    )
    return StreamingResponse(
        body(),
        media_type=content_type,
        headers=headers,
    )


async def seedr_folder_download_url(folder_id: str) -> str:
    folder_id = str(folder_id or "").strip()
    if not folder_id or not folder_id.isdigit():
        raise HTTPException(400, "Invalid Seedr folder id")

    # Known-good Torrent Studio implementation:
    # initialize a temporary archive and let Seedr return the signed URL.
    archive_id = str(uuid.uuid4())
    payload = seedr_data(
        await seedr_request(
            f"/download/archive/init/{quote(archive_id)}",
            "PUT",
            {"archive_arr": [{"type": "folder", "id": int(folder_id)}]},
        )
    )

    if isinstance(payload, dict):
        url = str(
            payload.get("url")
            or payload.get("download_url")
            or payload.get("downloadUrl")
            or payload.get("signed_url")
            or payload.get("signedUrl")
            or ""
        ).strip()
    elif isinstance(payload, str):
        url = payload.strip()
    else:
        url = ""

    if not url:
        raise HTTPException(502, "Seedr did not return a folder download URL")
    return url


@app.get("/api/seedr/folders/{folder_id}/download/url")
async def seedr_folder_download_url_api(folder_id: str):
    # Resolve the temporary direct Seedr folder URL only on an explicit click.
    url = await seedr_folder_download_url(folder_id)
    return {"url": url}

@app.get("/api/seedr/folders/{folder_id}/download")
async def seedr_folder_download(folder_id: str):
    # Redirect the browser to Seedr's temporary direct folder URL rather than
    # streaming the archive through Render.
    url = await seedr_folder_download_url(folder_id)
    return RedirectResponse(url, status_code=307)


@app.get("/api/seedr/folders/{folder_id}/download/direct")
async def seedr_folder_download_direct(
    folder_id: str,
    filename: str = Query(""),
):
    """Stream a Seedr folder archive with an explicit .zip filename."""
    url = await seedr_folder_download_url(folder_id)
    filename_value = _safe_download_filename(
        filename,
        f"seedr-folder-{folder_id}.zip",
    )
    if not filename_value.lower().endswith(".zip"):
        filename_value += ".zip"

    body, content_type, headers = await _stream_seedr_download(url)
    ascii_name = filename_value.encode("ascii", errors="ignore").decode("ascii").strip() or f"seedr-folder-{folder_id}.zip"
    headers["Content-Disposition"] = (
        'attachment; filename="' + ascii_name.replace('"', "_") + '"'
        + "; filename*=UTF-8''" + quote(filename_value, safe="")
    )
    return StreamingResponse(
        body(),
        media_type=content_type or "application/zip",
        headers=headers,
    )

# HLS stream sources are kept server-side. The browser receives a same-origin
# manifest URL so the Seedr access token never needs to be exposed to the client.
SEEDR_HLS_CACHE_SECONDS = 600
_seedr_hls_sources: dict[str, tuple[float, str, set[str]]] = {}


def _seedr_media_url(file_id: str, media_type: str) -> str:
    if media_type == "video":
        endpoint = f"/media/hls/{quote(file_id)}"
    elif media_type == "audio":
        endpoint = f"/media/mp3/{quote(file_id)}"
    else:
        raise HTTPException(400, "Unsupported Seedr media type")
    return SEEDR_MEDIA_BASE.rstrip("/") + endpoint + "?access_token=" + quote(current_seedr_token(), safe="")

def seedr_v2_bearer_token() -> str:
    """Accept a raw Seedr PAT or MediaFusion-style base64 JSON token."""
    raw = current_seedr_token().strip()
    if not raw:
        return ""
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        payload = json.loads(decoded)
        token = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
        if token:
            return token
    except Exception:
        pass
    return raw


async def seedr_v2_request(path: str) -> Any:
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")
    bearer = seedr_v2_bearer_token()
    if not bearer:
        raise HTTPException(503, "Seedr access token is empty")
    url = SEEDR_V2_BASE.rstrip("/") + "/" + str(path).lstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        )
    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw
    if response.status_code >= 400:
        raise HTTPException(
            response.status_code,
            seedr_error_message(response.status_code, data, raw),
        )
    return data

async def seedr_v2_video_url(file_id: str, audio_index: int | None = None) -> str:
    """Use Seedr V2 current presentation URL, with direct-download fallback."""
    if not file_id:
        return ""
    try:
        presentation_path = f"/presentations/file/{quote(file_id)}/video"
        if audio_index is not None and audio_index >= 0:
            presentation_path += "?" + urlencode({"audio": str(audio_index)})
        payload = seedr_data(await seedr_v2_request(presentation_path))
        if isinstance(payload, dict):
            link = payload.get("link")
            link_url = link.get("url") if isinstance(link, dict) else ""
            url = str(payload.get("url") or payload.get("stream_url") or link_url or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    except HTTPException:
        pass

    try:
        payload = seedr_data(await seedr_v2_request(f"/download/file/{quote(file_id)}/url"))
        if isinstance(payload, dict):
            url = str(payload.get("url") or payload.get("download_url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    except HTTPException:
        pass

    return ""


def _absolute_hls_uri(base_url: str, uri: str) -> str:
    absolute = urljoin(base_url, uri)
    # Some HLS manifests use one access query string on the playlist URL and
    # omit it from relative segment/variant URLs. Carry it forward.
    if not urlsplit(uri).query:
        base_query = urlsplit(base_url).query
        if base_query and not urlsplit(absolute).query:
            absolute += "?" + base_query
    return absolute


def _encode_hls_target(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_hls_target(value: str) -> str:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except Exception as exc:
        raise HTTPException(400, "Invalid HLS resource token") from exc


def _rewrite_hls_manifest(file_id: str, manifest_text: str, base_url: str) -> str:
    lines = manifest_text.splitlines()
    rewritten: list[str] = []

    def proxy_url(absolute: str) -> str:
        return "/api/seedr/hls/" + quote(file_id, safe="") + "/resource?u=" + _encode_hls_target(absolute)

    for line in lines:
        current = line
        def replace_uri(match: re.Match[str]) -> str:
            uri = match.group(1)
            return 'URI="' + proxy_url(_absolute_hls_uri(base_url, uri)) + '"'
        current = re.sub(r'URI="([^"]+)"', replace_uri, current)

        stripped = current.strip()
        if stripped and not stripped.startswith("#"):
            current = proxy_url(_absolute_hls_uri(base_url, stripped))
        rewritten.append(current)

    return "\n".join(rewritten) + ("\n" if manifest_text.endswith("\n") else "")


async def _presentation_is_hls(url: str) -> bool:
    """Probe a Seedr presentation URL without downloading the full media file."""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(url, headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,video/*,*/*", "Range": "bytes=0-2047"})
        content_type = str(response.headers.get("content-type") or "").lower()
        prefix = response.content[:2048].decode("utf-8", errors="ignore")
        return "mpegurl" in content_type or "#EXTM3U" in prefix
    except Exception as exc:
        logger.info("Seedr presentation probe failed: %s", exc)
        return False


async def _fetch_seedr_hls_manifest(file_id: str) -> tuple[str, str]:
    """Resolve a browser-playable HLS manifest from Seedr V2, then v1 fallback."""
    candidates: list[str] = []
    presentation_url = await seedr_v2_video_url(file_id)
    if presentation_url:
        candidates.append(presentation_url)
    # Seedr's legacy HLS endpoint explicitly requests a converted browser stream.
    candidates.append(_seedr_media_url(file_id, "video"))

    last_detail = "Seedr did not return an HLS manifest"
    for upstream_url in dict.fromkeys(candidates):
        try:
            async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
                response = await client.get(
                    upstream_url,
                    headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*"},
                )
            if response.status_code >= 400:
                last_detail = response.text[:500] or f"Seedr returned HTTP {response.status_code}"
                continue

            content_type = str(response.headers.get("content-type") or "").lower()
            text = response.text
            if "#EXTM3U" not in text[:200]:
                last_detail = f"Seedr returned {content_type or 'non-HLS content'}"
                continue

            final_url = str(response.url)
            host = urlsplit(final_url).hostname or ""
            if not host:
                last_detail = "Seedr returned an invalid HLS URL"
                continue

            allowed_hosts = {host.lower()}
            for raw_uri in re.findall(r'(?:URI="([^"]+)"|^([^#\s][^\r\n]*))', text, flags=re.M):
                uri = raw_uri[0] or raw_uri[1]
                if uri:
                    try:
                        ref_host = urlsplit(_absolute_hls_uri(final_url, uri)).hostname
                        if ref_host:
                            allowed_hosts.add(ref_host.lower())
                    except Exception:
                        pass

            _seedr_hls_sources[file_id] = (
                asyncio.get_running_loop().time() + SEEDR_HLS_CACHE_SECONDS,
                final_url,
                allowed_hosts,
            )
            logger.info("Seedr HLS manifest resolved: file=%s source=%s", file_id, urlsplit(final_url).hostname)
            return text, final_url
        except Exception as exc:
            last_detail = str(exc)
            continue

    raise HTTPException(502, last_detail)

async def _get_hls_source(file_id: str) -> tuple[str, set[str]]:
    now = asyncio.get_running_loop().time()
    cached = _seedr_hls_sources.get(file_id)
    if cached and cached[0] > now:
        return cached[1], cached[2]

    _manifest, final_url = await _fetch_seedr_hls_manifest(file_id)
    cached = _seedr_hls_sources[file_id]
    return final_url, cached[2]


async def resolve_seedr_stream_id(file_id: str, name: str = "") -> str:
    """Resolve the Seedr playback file identifier without requiring HLS."""
    candidate = str(file_id or "").strip()
    if candidate:
        try:
            presentation = await seedr_v2_video_url(candidate)
            if presentation:
                return candidate
        except HTTPException:
            pass
        try:
            await _fetch_seedr_hls_manifest(candidate)
            return candidate
        except HTTPException:
            pass

    if not name:
        raise HTTPException(404, "Seedr playback file could not be resolved")

    try:
        result = seedr_data(await seedr_request(f"/search/fs?query={quote(name)}"))
    except HTTPException as exc:
        raise HTTPException(exc.status_code, "Seedr file lookup failed") from exc

    candidates = arr(result, ("files", "items"))
    wanted_name = Path(name).name.lower()
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        raw_name = str(raw.get("name") or raw.get("title") or "").strip()
        if raw_name.lower() != wanted_name:
            continue

        # Match MediaFusion: Seedr V2 playback uses the actual file id.
        resolved = str(
            raw.get("id")
            or raw.get("file_id")
            or raw.get("folder_file_id")
            or raw.get("folderFileId")
            or ""
        ).strip()
        if not resolved:
            continue

        try:
            await _fetch_seedr_hls_manifest(resolved)
            logger.info("Seedr playback id resolved by filename: %s -> %s", name, resolved)
            return resolved
        except HTTPException:
            continue

    raise HTTPException(404, f"No playable Seedr stream found for {name}")


@app.get("/api/seedr/files/stream")
async def seedr_file_stream(
    file_id: str = Query(""),
    type: str = Query("video"),
    name: str = Query(""),
):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    resolved_id = await resolve_seedr_stream_id(file_id, name)

    if type == "video":
        # Restore the tested direct-browser-stream strategy from
        # fix-seedr-direct-browser-stream:
        #   1) ask Seedr V2 for its presentation URL;
        #   2) if it is a browser-playable direct presentation, proxy it with
        #      Range support;
        #   3) otherwise use the existing HLS proxy.
        presentation_url = await seedr_v2_video_url(resolved_id)
        if presentation_url and not await _presentation_is_hls(presentation_url):
            return {
                "url": "/api/seedr/media/video/" + quote(resolved_id, safe=""),
                "externalUrl": presentation_url,
                "name": name or resolved_id,
                "resolvedFileId": resolved_id,
                "protocol": "direct",
            }

        try:
            await _fetch_seedr_hls_manifest(resolved_id)
            return {
                "url": "/api/seedr/hls/" + quote(resolved_id, safe=""),
                "externalUrl": presentation_url or _seedr_media_url(resolved_id, "video"),
                "name": name or resolved_id,
                "resolvedFileId": resolved_id,
                "protocol": "hls",
            }
        except HTTPException:
            if presentation_url:
                return {
                    "url": "/api/seedr/media/video/" + quote(resolved_id, safe=""),
                    "externalUrl": presentation_url,
                    "name": name or resolved_id,
                    "resolvedFileId": resolved_id,
                    "protocol": "direct",
                }
            raise

    return {
        "url": "/api/seedr/media/audio/" + quote(resolved_id, safe=""),
        "externalUrl": _seedr_media_url(resolved_id, "audio"),
        "name": name or resolved_id,
        "resolvedFileId": resolved_id,
        "protocol": "mp3",
    }

@app.get("/api/seedr/hls/{file_id}")
async def seedr_hls_manifest(file_id: str):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    manifest, final_url = await _fetch_seedr_hls_manifest(file_id)
    cached = _seedr_hls_sources.get(file_id)
    allowed_hosts = cached[2] if cached else {urlsplit(final_url).hostname.lower()}
    rewritten = _rewrite_hls_manifest(file_id, manifest, final_url)

    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/seedr/hls/{file_id}/resource")
async def seedr_hls_resource(request: Request, file_id: str, u: str = Query(...)):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    target = _decode_hls_target(u)
    parsed = urlsplit(target)
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(400, "Invalid HLS target URL")

    try:
        base_url, allowed_hosts = await _get_hls_source(file_id)
    except HTTPException:
        raise

    if host not in allowed_hosts:
        raise HTTPException(403, "HLS resource host is not allowed")

    headers: dict[str, str] = {"Accept": "*/*"}
    if request is not None:
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header

    async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
        response = await client.get(target, headers=headers)

    if response.status_code >= 400:
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/octet-stream"),
            headers={"Access-Control-Allow-Origin": "*"},
        )

    content_type = str(response.headers.get("content-type") or "").lower()
    body = response.content
    _record_media_bytes(file_id, len(body))

    if "mpegurl" in content_type or "#EXTM3U" in body[:200].decode("utf-8", errors="ignore"):
        text = body.decode("utf-8", errors="replace")
        final_url = str(response.url)
        final_host = urlsplit(final_url).hostname
        if final_host:
            allowed_hosts.add(final_host.lower())
        _seedr_hls_sources[file_id] = (
            asyncio.get_running_loop().time() + SEEDR_HLS_CACHE_SECONDS,
            final_url,
            allowed_hosts,
        )
        body = _rewrite_hls_manifest(file_id, text, final_url).encode("utf-8")
        content_type = "application/vnd.apple.mpegurl"

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
    }
    for header in ("content-range", "content-length", "etag", "last-modified"):
        if response.headers.get(header):
            response_headers[header.title()] = response.headers[header]

    return Response(
        content=body,
        status_code=response.status_code,
        media_type=content_type or "application/octet-stream",
        headers=response_headers,
    )


async def _seedr_media_source_url(file_id: str) -> str:
    result = await download_url(file_id)
    url = str(result.get("url") or "").strip()
    if not url:
        raise HTTPException(502, "Seedr returned no media URL")
    return url


_SEEDR_FFPROBE_CACHE_SECONDS = 600
_seedr_ffprobe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _ffprobe_seedr_file(file_id: str) -> dict[str, Any]:
    """Inspect a Seedr file and reuse the result for media/subtitle requests.

    The same MKV was previously probed once for track discovery and then probed
    again when the user selected an embedded subtitle. Reusing the probe keeps
    Render Free from doing the expensive remote MKV inspection twice.
    """
    now = asyncio.get_running_loop().time()
    cached = _seedr_ffprobe_cache.get(file_id)
    if cached and cached[0] > now:
        return cached[1]

    source_url = await _seedr_media_source_url(file_id)
    command = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        "-analyzeduration", "10M",
        "-probesize", "20M",
        source_url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        raise HTTPException(504, "Media track inspection timed out") from exc
    except FileNotFoundError as exc:
        raise HTTPException(503, "FFmpeg is not installed on the media server") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise HTTPException(502, detail or "FFprobe could not inspect the Seedr file")

    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise HTTPException(502, "FFprobe returned invalid metadata") from exc

    _seedr_ffprobe_cache[file_id] = (now + _SEEDR_FFPROBE_CACHE_SECONDS, data)
    return data


_SEEDR_MEDIA_INFO_CACHE_SECONDS = 600
_seedr_media_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_SEEDR_EMBEDDED_SUBTITLE_CACHE_SECONDS = 600
_seedr_embedded_subtitle_cache: dict[str, tuple[float, bytes]] = {}


def _stream_tags(stream: dict[str, Any]) -> dict[str, Any]:
    raw = stream.get("tags")
    if not isinstance(raw, dict):
        return {}
    # FFprobe normally emits lowercase Matroska tag keys, but some containers
    # expose alternate casing/names. Normalize them before building labels.
    return {str(key).strip().lower(): value for key, value in raw.items()}


def _track_language(stream: dict[str, Any]) -> str:
    tags = _stream_tags(stream)
    for key in ("language", "lang", "language-eng", "language_ietf"):
        value = str(tags.get(key) or "").strip().lower()
        if value:
            return value.split("-")[0]
    return ""


def _track_title(stream: dict[str, Any], fallback: str) -> str:
    tags = _stream_tags(stream)
    for key in ("title", "handler_name", "name", "track_name"):
        value = str(tags.get(key) or "").strip()
        if value:
            return value
    return fallback


@app.get("/api/seedr/media-info/{file_id}")
async def seedr_media_info(file_id: str):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    cached = _seedr_media_info_cache.get(file_id)
    now = asyncio.get_running_loop().time()
    if cached and cached[0] > now:
        return cached[1]

    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    if not isinstance(streams, list):
        streams = []

    language_names = {
        "en": "English", "eng": "English",
        "hi": "Hindi", "hin": "Hindi",
        "fr": "French", "fra": "French",
        "de": "German", "deu": "German",
        "es": "Spanish", "spa": "Spanish",
        "it": "Italian", "ita": "Italian",
        "pt": "Portuguese", "por": "Portuguese",
        "ru": "Russian", "rus": "Russian",
        "ja": "Japanese", "jpn": "Japanese",
        "ko": "Korean", "kor": "Korean",
        "zh": "Chinese", "zho": "Chinese",
        "ar": "Arabic", "ara": "Arabic",
        "bn": "Bengali", "ben": "Bengali",
    }

    audio_tracks = []
    subtitle_tracks = []
    audio_index = 0
    subtitle_index = 0

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "").lower()
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        lang = _track_language(stream)
        title = _track_title(stream, "")
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        if codec_type == "audio":
            language_label = language_names.get(lang, lang.upper() if lang else "")
            # Keep the raw mix title separate from the language. The frontend
            # combines them into a clearer label such as "English (BD 5.1)".
            label = title or language_label or f"Audio {audio_index + 1}"
            audio_tracks.append({
                "index": audio_index,
                "streamIndex": int(stream.get("index") or 0),
                "language": lang,
                "title": label,
                "codec": str(stream.get("codec_name") or "").upper(),
                "channels": int(stream.get("channels") or 0),
                "default": bool(disposition.get("default")),
            })
            audio_index += 1
        elif codec_type == "subtitle":
            codec = str(stream.get("codec_name") or "").lower()
            # Bitmap subtitle codecs cannot be represented as browser WebVTT.
            if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}:
                continue
            language_label = language_names.get(lang, lang.upper() if lang else "")
            forced = bool(disposition.get("forced"))
            default = bool(disposition.get("default"))
            codec_label = {
                "subrip": "SRT",
                "ass": "ASS",
                "ssa": "SSA",
                "webvtt": "WebVTT",
                "mov_text": "Text",
                "text": "Text",
            }.get(codec, codec.upper() or "Text")
            # Preserve the real Matroska title/language whenever available.
            # Some MKVs contain no subtitle language/title tags at all. In that
            # case the old fallback produced identical entries such as
            # "SRT", making it impossible to tell tracks apart. Give unnamed
            # tracks a stable ordinal while keeping the real language when one
            # is available.
            if title:
                label = title
            elif language_label:
                label = f"{language_label} {subtitle_index + 1}"
            else:
                label = f"{codec_label} {subtitle_index + 1}"

            if forced and "forced" not in label.lower():
                label += " · Forced"
            elif default and "default" not in label.lower():
                label += " · Default"
            stream_index = int(stream.get("index") or 0)
            subtitle_tracks.append({
                "index": subtitle_index,
                "streamIndex": stream_index,
                "language": lang,
                "title": label,
                "codec": str(stream.get("codec_name") or "").upper(),
                "url": f"/api/seedr/media-info/{quote(file_id, safe='')}/subtitle?stream={stream_index}&name={quote(label, safe='')}",
            })
            subtitle_index += 1

    result = {
        "name": str((data.get("format") or {}).get("filename") or file_id) if isinstance(data, dict) else file_id,
        "audioTracks": audio_tracks,
        "subtitleTracks": subtitle_tracks,
    }
    _seedr_media_info_cache[file_id] = (now + _SEEDR_MEDIA_INFO_CACHE_SECONDS, result)
    return result


@app.get("/api/seedr/media-info/{file_id}/subtitle")
async def seedr_embedded_subtitle(
    file_id: str,
    track: int | None = Query(None, ge=0),
    stream: int | None = Query(None, ge=0),
    name: str = Query("subtitle"),
):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    cache_key = f"{file_id}:{track}"
    now = asyncio.get_running_loop().time()
    cached = _seedr_embedded_subtitle_cache.get(cache_key)
    if cached and cached[0] > now:
        return Response(
            content=cached[1],
            media_type="text/vtt; charset=utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "private, max-age=600",
                "Content-Disposition": f'inline; filename="{Path(name).name.replace(chr(34), "_")}.vtt"',
            },
        )

    # Reuses the media-info FFprobe result instead of probing the remote MKV
    # a second time when the user selects an embedded subtitle.
    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    subtitle_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "subtitle"
        and str(stream.get("codec_name") or "").lower() not in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
    ]
    if stream is not None:
        selected = next(
            (
                item for item in subtitle_streams
                if int(item.get("index") or -1) == stream
            ),
            None,
        )
        if selected is None:
            raise HTTPException(404, "Subtitle stream not found")
        stream_index = int(selected.get("index") or 0)
        cache_key = f"{file_id}:stream:{stream_index}"
    else:
        if track is None or track >= len(subtitle_streams):
            raise HTTPException(404, "Subtitle track not found")
        stream_index = int(subtitle_streams[track].get("index") or 0)
        cache_key = f"{file_id}:{track}"

    source_url = await _seedr_media_source_url(file_id)
    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", source_url,
        "-map", f"0:{stream_index}",
        "-c:s", "webvtt",
        "-f", "webvtt",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        raise HTTPException(504, "Subtitle extraction timed out") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise HTTPException(502, detail or "Subtitle extraction failed")

    _seedr_embedded_subtitle_cache[cache_key] = (
        now + _SEEDR_EMBEDDED_SUBTITLE_CACHE_SECONDS,
        stdout,
    )

    return Response(
        content=stdout,
        media_type="text/vtt; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "private, max-age=600",
            "Content-Disposition": f'inline; filename="{Path(name).name.replace(chr(34), "_")}.vtt"',
        },
    )


async def _stream_selected_audio(file_id: str, audio_index: int, start: float = 0.0) -> StreamingResponse:
    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "audio"
    ]
    if audio_index >= len(audio_streams):
        raise HTTPException(404, "Audio track not found")

    source_url = await _seedr_media_source_url(file_id)
    stream = audio_streams[audio_index]
    video_streams = [
        item for item in streams
        if isinstance(item, dict) and str(item.get("codec_type") or "").lower() == "video"
    ]
    video_codec = str((video_streams[0] if video_streams else {}).get("codec_name") or "").lower()

    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        *([ "-ss", str(max(0.0, start)) ] if start > 0 else []),
        "-i", source_url,
        "-map", "0:v:0",
        "-map", f"0:a:{audio_index}",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    if video_codec in {"h264", "avc1"}:
        command[command.index("-c:a"):command.index("-c:a")] = ["-c:v", "copy"]
    else:
        command[command.index("-c:a"):command.index("-c:a")] = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
        ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def body():
        try:
            while True:
                chunk = await process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass

    return StreamingResponse(
        body(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/api/seedr/media/video/{file_id}/stats")
async def seedr_video_media_stats(file_id: str):
    return {
        "bytesPerSecond": round(_media_download_speed(file_id), 2),
        "windowSeconds": _MEDIA_STATS_WINDOW_SECONDS,
    }

@app.get("/api/seedr/media/video/{file_id}")
async def seedr_video_media(
    file_id: str,
    request: Request,
    audio: int | None = Query(None, ge=0),
    start: float = Query(0.0, ge=0.0),
):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    if audio is not None:
        return await _stream_selected_audio(file_id, audio, start=start)

    upstream_url = await seedr_v2_video_url(file_id)
    if not upstream_url:
        raise HTTPException(404, "Seedr returned no video presentation URL")

    headers = {"Accept": "video/*,application/octet-stream,*/*"}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        response = await client.send(
            client.build_request("GET", upstream_url, headers=headers),
            stream=True,
        )
    except Exception:
        await client.aclose()
        raise

    if response.status_code >= 400:
        body = await response.aread()
        status = response.status_code
        content_type = response.headers.get("content-type", "application/octet-stream")
        await response.aclose()
        await client.aclose()
        return Response(
            content=body,
            status_code=status,
            media_type=content_type,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "no-store",
    }
    for header in ("content-range", "content-length", "etag", "last-modified"):
        if response.headers.get(header):
            response_headers[header.title()] = response.headers[header]

    async def body_stream():
        try:
            async for chunk in response.aiter_bytes():
                _record_media_bytes(file_id, len(chunk))
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )


@app.get("/api/seedr/media/audio/{file_id}")
async def seedr_audio_media_route(
    file_id: str,
    request: Request,
    track: int = Query(0, ge=0),
    start: float = Query(0.0, ge=0.0),
):
    """Stream one Seedr audio track as browser-compatible fragmented MP4.

    This route is used for alternate-audio playback so the video element never
    has to be replaced. Keeping video on its original timeline avoids the
    shortened-duration and A/V drift caused by remuxing the whole video when a
    user changes audio tracks.
    """
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "audio"
    ]
    if track >= len(audio_streams):
        raise HTTPException(404, "Audio track not found")

    source_url = await _seedr_media_source_url(file_id)
    source = audio_streams[track]
    codec = str(source.get("codec_name") or "").lower()

    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        *(["-ss", str(max(0.0, start))] if start > 0 else []),
        "-i", source_url,
        "-map", f"0:a:{track}",
    ]

    # Emit a plain MP3 stream for the hidden browser audio element.
    # MP3 is broadly supported by Chrome/Edge/Android and does not require the
    # fragmented MP4/WebM demuxers that can stall on a non-seekable pipe.
    command += [
        "-vn",
        "-sn",
        "-dn",
        "-ac", "2",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-f", "mp3",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, "FFmpeg is not installed on the media server") from exc

    async def body():
        try:
            while True:
                chunk = await process.stdout.read(512 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass

    return StreamingResponse(
        body(),
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )

@app.get("/api/seedr/files/{file_id}/subtitle")
async def seedr_file_subtitle(
    file_id: str,
    filename: str = Query(""),
):
    """Return a Seedr sidecar subtitle as browser-compatible WebVTT."""
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    requested_name = Path(filename or "").name
    extension = requested_name.rsplit(".", 1)[-1].lower() if "." in requested_name else ""
    if extension not in {"srt", "vtt"}:
        raise HTTPException(400, "Only SRT and VTT subtitles are supported")

    result = await download_url(file_id)
    upstream_url = str(result.get("url") or "").strip()
    if not upstream_url:
        raise HTTPException(502, "Seedr returned no subtitle URL")

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(upstream_url)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, "Seedr subtitle download failed")

    text = response.content.decode("utf-8-sig", errors="replace")
    if extension == "srt":
        # Convert the common SRT timestamp format to WebVTT.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(
            r"(\d{2}:\d{2}:\d{2}),(\d{3})",
            r"\1.\2",
            text,
        )
        text = "WEBVTT\n\n" + text.lstrip()
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text.lstrip()

    return Response(
        content=text,
        media_type="text/vtt; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
            "Content-Disposition": "inline; filename=\"" + requested_name.replace('"', '_') + "\"",
        },
    )

async def _cancel_seedr_task_and_partial_folder(
    tid: str,
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stop an unfinished Seedr task and remove its partial folder, never a completed download."""
    global _seedr_metadata_cache

    task_id_value = str(tid or "").strip()
    if not task_id_value:
        raise HTTPException(400, "A Seedr task id is required")

    task_found = isinstance(task, dict) and bool(task)
    task_value = task if isinstance(task, dict) else {}
    if not task_found:
        try:
            raw = seedr_data(await seedr_request(f"/tasks/{quote(task_id_value, safe='')}"))
            task_value = unwrap_seedr_task(raw)
            task_found = bool(task_value)
        except (HTTPException, SeedrError) as exc:
            if getattr(exc, "status_code", 0) != 404:
                raise
            task_value = {}

    complete = task_complete(task_value) if task_found else False
    active = task_found and not complete
    task_name = seedr_task_name(task_value) or f"Torrent {task_id_value}"
    folder_id = seedr_task_folder_id(task_value) if task_found else ""
    task_files: list[dict[str, Any]] = []

    # Some Seedr API variants expose the created folder only in task contents.
    if active and not folder_id:
        try:
            task_files = await task_contents(task_id_value)
            folder_id = (
                _seedr_effective_task_folder_id(task_value, task_files)
                or _seedr_file_folder_id(task_files)
            )
        except (HTTPException, SeedrError):
            task_files = []

    folder_size = 0
    for file in task_files:
        if not isinstance(file, dict):
            continue
        try:
            folder_size += max(0, int(float(file.get("size") or 0)))
        except (TypeError, ValueError):
            continue
    task_deleted = False
    if task_found:
        try:
            await seedr_request(f"/tasks/{quote(task_id_value, safe='')}", "DELETE")
            task_deleted = True
        except (HTTPException, SeedrError) as exc:
            status_code = getattr(exc, "status_code", 0)
            if status_code == 405:
                try:
                    await seedr_request(f"/tasks/{quote(task_id_value, safe='')}/delete", "POST")
                    task_deleted = True
                except (HTTPException, SeedrError) as fallback_exc:
                    if getattr(fallback_exc, "status_code", 0) != 404:
                        raise
            elif status_code != 404:
                raise

    folder_deleted = False
    if active and folder_id and folder_id != "0":
        # Seedr can remove the task before its partial folder is ready to be
        # deleted. Retry the folder DELETE with backoff instead of returning an
        # error that forces the user to click Prepare a second time.
        max_delete_attempts = 5
        last_delete_error: Exception | None = None
        for attempt in range(max_delete_attempts):
            try:
                await seedr_request(f"/fs/folder/{quote(folder_id, safe='')}", "DELETE")
                folder_deleted = True
                last_delete_error = None
                break
            except (HTTPException, SeedrError) as exc:
                status_code = getattr(exc, "status_code", 0)
                if status_code == 404:
                    # A previous attempt or Seedr's own task deletion already
                    # removed the transient folder.
                    folder_deleted = True
                    last_delete_error = None
                    break

                last_delete_error = exc
                if attempt + 1 < max_delete_attempts:
                    delay = min(0.75 * (2 ** attempt), 4.0)
                    logger.warning(
                        "Seedr partial-folder delete retry task=%s folder=%s attempt=%s/%s delay_seconds=%.2f status=%s detail=%s",
                        task_id_value,
                        folder_id,
                        attempt + 1,
                        max_delete_attempts,
                        delay,
                        status_code,
                        getattr(exc, "detail", str(exc)),
                    )
                    await asyncio.sleep(delay)

        if not folder_deleted:
            # Do not abort the new Prepare solely because the partial-folder
            # deletion endpoint briefly failed. The old task has already been
            # cancelled; continue to quota cleanup and the requested add, which
            # will trigger the existing storage-recovery retry if necessary.
            logger.error(
                "Seedr partial-folder deletion did not confirm success after retries; continuing with new add task=%s folder=%s status=%s detail=%s",
                task_id_value,
                folder_id,
                getattr(last_delete_error, "status_code", 0),
                getattr(last_delete_error, "detail", str(last_delete_error)),
            )

    _seedr_cleanup_jobs.pop(task_id_value, None)
    _seedr_torrent_names_by_task.pop(task_id_value, None)
    if folder_id:
        _seedr_torrent_names.pop(folder_id, None)
        _seedr_folder_cache.pop(folder_id, None)
    _save_seedr_cleanup_jobs()
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()

    return {
        "taskId": task_id_value,
        "name": task_name,
        "found": task_found,
        "active": active,
        "completed": complete,
        "taskDeleted": task_deleted,
        "folderId": folder_id,
        "folderDeleted": folder_deleted,
        "size": folder_size,
    }


async def _cancel_active_seedr_tasks_for_replacement(
    replace_task_id: str = "",
) -> list[dict[str, Any]]:
    """Cancel all unfinished tasks in the connected Seedr account before a new torrent is added."""
    explicit_id = str(replace_task_id or "").strip()
    tracked_ids = {str(task_id_value).strip() for task_id_value in _seedr_cleanup_jobs}
    tracked_ids.discard("")

    try:
        payload = seedr_data(await seedr_request("/tasks"))
    except (HTTPException, SeedrError) as exc:
        logger.warning("Could not list Seedr tasks before replacement: %s", getattr(exc, "detail", str(exc)))
        raise SeedrError(
            "SEEDR_TASK_LIST_FAILED",
            503,
            "Could not check the current Seedr download before replacing it. Please retry.",
        ) from exc

    cancelled: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_task in arr(payload, ("tasks", "torrents", "items")):
        task = unwrap_seedr_task(seedr_data(raw_task))
        if not task:
            continue
        task_id_value = task_id(task)
        if task_id_value:
            seen_ids.add(task_id_value)
        if not task_id_value or task_complete(task):
            continue
        if task_id_value in {str(item.get("taskId") or "") for item in cancelled}:
            continue

        result = await _cancel_seedr_task_and_partial_folder(task_id_value, task)
        if result.get("active"):
            cancelled.append(result)

    # A tracked or explicitly selected task may be temporarily absent from the
    # list endpoint. Check those IDs directly so stale Seedr tasks do not survive
    # simply because the backend restarted or /tasks omitted a row.
    fallback_ids = tracked_ids | ({explicit_id} if explicit_id else set())
    for task_id_value in sorted(fallback_ids - seen_ids):
        try:
            result = await _cancel_seedr_task_and_partial_folder(task_id_value)
        except (HTTPException, SeedrError) as exc:
            logger.warning(
                "Seedr replacement could not cancel stale task=%s: %s",
                task_id_value,
                getattr(exc, "detail", str(exc)),
            )
            raise
        if result.get("active"):
            cancelled.append(result)

    if cancelled:
        logger.info(
            "Seedr replacement cancelled %s unfinished task(s): %s",
            len(cancelled),
            ",".join(str(item.get("taskId") or "") for item in cancelled),
        )
    else:
        logger.info("Seedr replacement found no unfinished Seedr tasks")
    return cancelled


@app.delete("/api/seedr/tasks/{tid}")
async def seedr_task_delete(tid: str):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")
    return await _cancel_seedr_task_and_partial_folder(tid)

@app.delete("/api/seedr/files/{file_id}")
async def seedr_file_delete(file_id: str):
    global _seedr_metadata_cache
    result = await seedr_request(f"/fs/file/{quote(file_id)}", "DELETE")
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()
    return result

@app.delete("/api/seedr/folders/{folder_id}")
async def seedr_folder_delete(folder_id: str):
    global _seedr_metadata_cache
    result = await seedr_request(f"/fs/folder/{quote(folder_id)}", "DELETE")
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()
    return result

# Compatibility endpoints for the preserved UI. They intentionally do not run
# qBittorrent or maintain local torrent storage; Seedr is the only transfer backend.
@app.get("/api/v2/torrents/info")
async def empty_torrents(filter: str | None = None):
    return []

def _libtorrent_metadata_sync(magnet: str) -> tuple[str, str, list[dict[str, Any]], int]:
    """Resolve only torrent metadata using libtorrent; never request content pieces."""
    tmp = tempfile.mkdtemp(prefix="torrent-metadata-lt-")
    ses = None
    handle = None
    try:
        ses = lt.session()

        # Explicitly enable the discovery paths needed for magnet metadata on
        # a cloud host. Keep UPnP/NAT-PMP/LSd off: they are not useful on
        # Render and can add connection delay. DHT + tracker access remain on.
        ses.apply_settings({
            "enable_dht": True,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_tcp": True,
            "enable_outgoing_utp": True,
            "enable_incoming_tcp": True,
            "enable_incoming_utp": True,
            "listen_interfaces": "0.0.0.0:0",
            "dht_bootstrap_nodes": (
                "router.bittorrent.com:6881,"
                "router.utorrent.com:6881,"
                "dht.transmissionbt.com:6881"
            ),
            "use_dht_as_fallback": False,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connection_speed": 50,
            "handshake_timeout": 10,
        })

        atp = lt.parse_magnet_uri(magnet)
        atp.save_path = tmp

        # upload_mode prevents piece requests. Disable auto-management so the
        # session cannot later take the torrent out of upload mode automatically.
        atp.flags = atp.flags | lt.torrent_flags.upload_mode
        atp.flags = atp.flags & ~lt.torrent_flags.auto_managed

        handle = ses.add_torrent(atp)

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if handle.has_metadata():
                break
            for alert in ses.pop_alerts():
                message = str(alert)
                if "error" in message.lower() or "tracker" in message.lower() or "dht" in message.lower():
                    logger.info("libtorrent alert for %s: %s", info_hash(magnet), message[:500])
            time.sleep(0.15)

        if not handle.has_metadata():
            status = handle.status()
            raise TimeoutError(
                f"libtorrent metadata timeout (state={status.state}, "
                f"peers={status.num_peers}, seeds={status.num_seeds})"
            )

        ti = handle.torrent_file()
        if ti is None:
            raise RuntimeError("libtorrent reported metadata but torrent_file() is empty")

        fs = ti.layout()
        torrent_name = str(ti.name() or "")
        files: list[dict[str, Any]] = []
        for index in range(fs.num_files()):
            path = str(fs.file_path(index))
            size = int(fs.file_size(index))
            if not path:
                continue
            files.append({
                "index": index,
                "name": path,
                "size": size,
                "path": path,
                "type": "file",
                "priority": 1,
            })

        total_size = sum(int(item.get("size") or 0) for item in files)
        return torrent_name, magnet, files, total_size
    finally:
        try:
            if ses is not None and handle is not None and handle.is_valid():
                ses.remove_torrent(handle)
        except Exception:
            pass
        try:
            if ses is not None:
                del ses
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


async def _fetch_remote_torrent_metadata(magnet: str, info_hash_value: str) -> dict[str, Any] | None:
    """Use the public torrent-metadata resolver as a metadata-only fallback."""
    if not TORRENT_METADATA_API_URL:
        return None
    try:
        # This service resolves BEP-9 metadata from a magnet without starting
        # a file download. Give Fly.io enough time to wake a sleeping instance.
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.post(
                TORRENT_METADATA_API_URL.rstrip("/") + "/",
                json={"query": magnet},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Remote metadata service failed for %s: %s", info_hash_value, exc)
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    returned_hash = str(data.get("infoHash") or "").strip().lower()
    if returned_hash and returned_hash != info_hash_value.lower():
        logger.info("Remote metadata hash mismatch: wanted %s, got %s", info_hash_value, returned_hash)
        return None

    raw_files = data.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return None

    files: list[dict[str, Any]] = []
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        if not path:
            continue
        files.append({
            "index": index,
            "name": str(item.get("name") or path),
            "size": int(float(item.get("size") or 0)),
            "path": path,
            "type": "file",
            "priority": 1,
        })
    if not files:
        return None

    result = {
        "name": str(data.get("name") or files[0]["name"]),
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item["size"]) for item in files),
        "source": "remote_torrent_metadata",
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    _metadata_cache[info_hash_value.lower()] = result
    _save_metadata_cache()
    return result


async def _lookup_knaben_by_hash(info_hash_value: str) -> dict[str, Any] | None:
    """Find and parse a Knaben cached descriptor for an exact info-hash."""
    target = info_hash_value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        return None

    payload_base = {
        "search_type": "100%",
        "query": target,
        "from": 0,
        "size": 20,
        "hide_unsafe": False,
        "hide_xxx": False,
    }

    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        for field in ("hash", None):
            payload = dict(payload_base)
            if field:
                payload["search_field"] = field
            try:
                response = await client.post(
                    KNABEN_API_URL,
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                continue

            hits = data.get("hits") if isinstance(data, dict) else None
            if not isinstance(hits, list):
                continue

            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                hit_hash = str(hit.get("hash") or "").strip().lower()
                if hit_hash != target:
                    continue

                descriptor = str(hit.get("link") or "").strip()
                if descriptor:
                    parsed = await _fetch_source_torrent_descriptor(descriptor, target)
                    if parsed:
                        return parsed

                details = str(hit.get("details") or "").strip()
                if details:
                    parsed = await _fetch_source_torrent_descriptor(details, target)
                    if parsed:
                        return parsed
    return None


@app.post("/api/v2/torrents/inspect-magnet")
async def seedr_inspect_magnet(body: dict[str, Any]):
    """Return cached metadata immediately or start a background resolver."""
    raw_magnet = str(body.get("magnet") or body.get("source") or "").strip()
    if not raw_magnet:
        raise HTTPException(400, "A magnet link is required")

    magnet = normalize_magnet(raw_magnet)
    h = info_hash(magnet)
    if not h:
        raise HTTPException(400, "A valid BTIH magnet link is required")
    h = h.lower()

    cached = _metadata_cache.get(h)
    if cached:
        return cached

    # Shared public cache is the fastest metadata path for both pasted magnets
    # and search results. It never starts Seedr or requests payload pieces.
    cached_descriptor = await _fetch_itorrents_metadata(h)
    if cached_descriptor:
        return cached_descriptor

    # Search results can carry a detail URL. When that page exposes a real
    # .torrent descriptor, use it before touching DHT/trackers.
    descriptor_url = str(body.get("descriptorUrl") or "").strip()
    source_url = str(body.get("sourceUrl") or body.get("infoUrl") or "").strip()

    # First use an actual descriptor URL returned by the indexer.
    for candidate_url in (descriptor_url, source_url):
        if candidate_url:
            descriptor_result = await _fetch_source_torrent_descriptor(candidate_url, h)
            if descriptor_result:
                return descriptor_result

    # Search results may have an exact hash cached by Knaben. For a pasted
    # magnet there is no source URL, so do this lookup in the background rather
    # than making the user wait on a second network request before the job starts.
    if descriptor_url or source_url:
        try:
            hash_result = await _lookup_knaben_by_hash(h)
        except Exception as exc:
            logger.info("Knaben hash lookup failed for %s: %s", h, exc)
            hash_result = None
        if hash_result:
            return hash_result

    job_id = h
    existing = _metadata_jobs.get(job_id)
    if existing and existing.get("status") == "ready" and existing.get("result"):
        return existing["result"]

    if not existing or existing.get("status") == "error":
        now = time.time()
        _metadata_jobs[job_id] = {
            "status": "queued",
            "jobId": job_id,
            "hash": h,
            "name": str(parse_qs(urlsplit(magnet).query).get("dn", [""])[0] or "").strip(),
            "startedAt": now,
            "updatedAt": now,
            "deadlineAt": now + TORRENT_METADATA_BACKGROUND_TTL_SECONDS,
            "rounds": 0,
            "error": None,
        }
        asyncio.create_task(_run_metadata_job(job_id, magnet, h))

    # Give a fast first attempt a short window. Slow metadata resolution
    # continues in the background and is polled by the frontend.
    for _ in range(10):
        await asyncio.sleep(0.2)
        job = _metadata_jobs.get(job_id, {})
        if job.get("status") == "ready" and job.get("result"):
            return job["result"]
        if job.get("status") == "error":
            raise HTTPException(502, str(job.get("error") or "Metadata lookup failed"))

    return JSONResponse(
        status_code=202,
        content={
            "status": "resolving",
            "jobId": job_id,
            "hash": h,
            "source": "libtorrent_metadata",
            "message": "Torrent metadata is still resolving. Seedr has not been started.",
        },
    )


@app.get("/api/v2/torrents/metadata-jobs")
async def seedr_metadata_jobs():
    now = time.time()
    jobs: list[dict[str, Any]] = []
    for job in _metadata_jobs.values():
        started = float(job.get("startedAt") or now)
        deadline = float(job.get("deadlineAt") or started)
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        files = result.get("files") if isinstance(result.get("files"), list) else []
        jobs.append({
            "jobId": str(job.get("jobId") or job.get("hash") or ""),
            "hash": str(job.get("hash") or "").lower(),
            "name": str(job.get("name") or result.get("name") or "").strip(),
            "status": str(job.get("status") or "resolving"),
            "rounds": int(job.get("rounds") or 0),
            "startedAt": started,
            "updatedAt": float(job.get("updatedAt") or started),
            "deadlineAt": deadline,
            "elapsedSeconds": max(0, int(now - started)),
            "remainingSeconds": max(0, int(deadline - now)),
            "fileCount": len(files),
            "totalSize": int(result.get("totalSize") or 0),
            "source": str(result.get("source") or "background_metadata"),
            "error": str(job.get("error") or "") or None,
        })
    jobs.sort(key=lambda item: float(item.get("startedAt") or 0), reverse=True)
    return {
        "jobs": jobs,
        "backgroundTtlSeconds": TORRENT_METADATA_BACKGROUND_TTL_SECONDS,
        "retentionSeconds": TORRENT_METADATA_JOB_RETENTION_SECONDS,
    }


@app.get("/api/v2/torrents/inspect-magnet/status")
async def seedr_inspect_magnet_status(jobId: str = Query(...)):
    job = _metadata_jobs.get(jobId.lower())
    if not job:
        cached = _metadata_cache.get(jobId.lower())
        if cached:
            return cached
        raise HTTPException(404, "Metadata job not found")

    if job.get("status") == "ready" and job.get("result"):
        return job["result"]

    if job.get("status") == "error":
        raise HTTPException(502, str(job.get("error") or "Metadata lookup failed"))

    now = time.time()
    started = float(job.get("startedAt") or now)
    deadline = float(job.get("deadlineAt") or started)
    return {
        "status": "resolving",
        "jobId": jobId,
        "hash": jobId,
        "name": str(job.get("name") or ""),
        "rounds": int(job.get("rounds") or 0),
        "startedAt": started,
        "updatedAt": float(job.get("updatedAt") or started),
        "deadlineAt": deadline,
        "elapsedSeconds": max(0, int(now - started)),
        "remainingSeconds": max(0, int(deadline - now)),
        "source": "background_metadata",
        "message": job.get("error") or "Torrent metadata is still resolving in the background. Seedr has not been started.",
    }



@app.get("/api/v2/torrents/files")
async def empty_torrent_files(hash: str):
    return []

@app.post("/api/v2/torrents/pause")
async def noop_pause(body: dict[str, Any]):
    return {}

@app.post("/api/v2/torrents/resume")
async def noop_resume(body: dict[str, Any]):
    return {}

@app.post("/api/v2/torrents/delete")
async def noop_delete(body: dict[str, Any]):
    return {}

@app.post("/api/v2/torrents/filePrio")
async def noop_prio(body: dict[str, Any]):
    return {}

@app.get("/api/files")
async def files_compat(
    folder: str = "/",
    search: str = "",
    type: str = "all",
    folder_id: str = "",
):
    # Only load files for the folder currently open in the UI. Folder metadata
    # is returned separately by /api/folders.
    target_id = folder_id.strip()
    if not target_id:
        target_id = "0" if folder in {"/", "/Torrent Studio"} else ""

    if not target_id.isdigit():
        metadata = await get_seedr_metadata_tree()
        for item in metadata.get("folders", []) if isinstance(metadata, dict) else []:
            if str(item.get("path") or "") == folder:
                target_id = str(item.get("id") or "")
                break

    if not target_id.isdigit():
        return []

    payload = await seedr_folder_payload(target_id)
    result = []
    for raw in arr(payload, ("files", "items")):
        f = normalize_file(raw, target_id)
        name = f["name"]
        if search and search.lower() not in name.lower():
            continue

        lower = name.lower()
        if type != "all":
            if type == "video" and not re.search(r"\.(mkv|mp4|m4v|webm|avi|mov|m3u8|ts)$", lower):
                continue
            if type == "audio" and not re.search(r"\.(mp3|wav|flac|aac|ogg|m4a)$", lower):
                continue
            if type == "document" and not re.search(r"\.(pdf|txt|doc|docx|xls|xlsx|ppt|pptx|csv)$", lower):
                continue
            if type == "archive" and not re.search(r"\.(zip|rar|7z|tar|gz|bz2)$", lower):
                continue

        folder_path = folder if folder not in {"", "/"} else "/Torrent Studio"
        result.append({
            "id": f["id"],
            "name": name,
            "path": folder_path.rstrip("/") + "/" + name,
            "folder": folder_path,
            "size": f["size"],
            "type": "video" if re.search(r"\.(mp4|mkv|webm|avi|mov|m4v|m3u8|ts)$", lower) else "other",
            "mimeType": "video/mp4" if re.search(r"\.mp4$", lower) else "application/octet-stream",
            "createdAt": 0,
            "isStreamable": bool(re.search(r"\.(mp4|mkv|webm|avi|mov|m3u8|ts|mp3|m4a|flac|aac|ogg)$", lower)),
            "ownerId": "seedr",
            "ownerName": "Seedr",
            "downloadUrl": f"/api/seedr/files/{f['id']}/download",
            "streamUrl": "",
        })
    return result


@app.get("/api/folders")
async def folders_compat():
    metadata = await get_seedr_metadata_tree()
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or "Folder"),
            "path": str(item.get("path") or "/"),
            "ownerId": "seedr",
            "ownerName": "Seedr",
            "isShared": False,
            "permissions": {},
            "createdAt": 0,
            "filesCount": int(item.get("filesCount") or 0),
            "totalSize": int(item.get("totalSize") or 0),
        }
        for item in metadata.get("folders", [])
    ] if isinstance(metadata, dict) else []

@app.get("/api/storage/stats")
async def storage_stats():
    q = await seedr_quota()
    metadata = await get_seedr_metadata_tree()
    root = metadata.get("root") if isinstance(metadata, dict) else {}
    used = int(q.get("usedSpace") or 0)
    total = int(q.get("maxSpace") or 0)
    pct = (used / total * 100) if total else 0
    return {
        "totalBytes": total,
        "usedBytes": used,
        "freeBytes": max(0, total-used),
        "usedPercentage": pct,
        "filesCount": int((root or {}).get("filesCount") or 0),
        "torrentsCount": 0,
        "isUnlimited": False,
        "serverCapacityLabel": "Seedr cloud storage",
        "alertLevel": "critical" if pct > 90 else "warning" if pct > 80 else "normal",
    }

@app.get("/api/users")
async def users():
    user = {"id": "seedr-user", "name": "Seedr User", "email": "", "role": "admin", "avatar": ""}
    return {"users": [user], "activeUserId": user["id"], "activeUser": user}

@app.get("/api/logs")
async def logs():
    return []

@app.post("/api/logs/clear")
async def clear_logs():
    return {}

@app.get("/api/notifications")
async def notifications():
    return []

@app.post("/api/notifications/read")
async def notifications_read():
    return {}

@app.post("/api/notifications/test")
async def notifications_test():
    return {}

@app.get("/api/cleanup/settings")
async def cleanup_settings():
    return {"autoCleanCompletedDays": 7, "autoPurgeOrphans": False, "autoCleanTempFiles": False, "storageThresholdPercent": 80}

@app.post("/api/cleanup/settings")
async def update_cleanup_settings(body: dict[str, Any]):
    return body

@app.post("/api/cleanup/run")
async def run_cleanup():
    return {"bytesFreed": 0, "filesRemoved": 0, "tempRemoved": 0, "orphansRemoved": 0}

@app.post("/api/feedback")
async def submit_feedback(body: FeedbackRequest, request: Request):
    feedback_type = str(body.type or "").strip().lower()
    if feedback_type not in {"review", "suggestion", "bug"}:
        raise HTTPException(400, "Invalid feedback type")

    message = str(body.message or "").strip()
    if len(message) < 5:
        raise HTTPException(400, "Feedback is too short")
    if len(message) > 3000:
        raise HTTPException(400, "Feedback is too long")

    name = str(body.name or "").strip()[:80]
    rating = int(body.rating) if body.rating is not None else None
    if feedback_type == "review" and (rating is None or rating < 1 or rating > 5):
        raise HTTPException(400, "A review rating from 1 to 5 is required")
    if feedback_type != "review":
        rating = None

    if not GITHUB_FEEDBACK_TOKEN:
        raise HTTPException(503, "Feedback is not configured yet")

    repo = GITHUB_FEEDBACK_REPO.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise HTTPException(500, "Feedback repository is not configured correctly")

    label = {"review": "Review", "suggestion": "Suggestion", "bug": "Bug report"}[feedback_type]
    title_prefix = {"review": "Review", "suggestion": "Suggestion", "bug": "Bug report"}[feedback_type]
    title_text = message.replace("\\n", " ").strip()
    title_text = re.sub(r"\s+", " ", title_text)[:70] or "New feedback"

    if feedback_type == "review" and rating is not None:
        stars = "⭐" * rating
        title = f"{stars} {rating}/5 Review"
    else:
        title = title_prefix

    if name:
        title += f" — {name}"

    submitted_at = datetime.now(timezone.utc).isoformat()
    forwarded_for = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()

    body_lines = [
        f"# {('⭐' * rating + ' ' + str(rating) + '/5 Review') if feedback_type == 'review' and rating is not None else label}",
        "",
    ]
    if name:
        body_lines.append(f"**👤 Name:** {name}")
    else:
        body_lines.append("**👤 Name:** Anonymous")
    if rating is not None:
        body_lines.append(f"**⭐ Rating:** {rating}/5")
    body_lines.extend([
        f"**🕒 Submitted:** {submitted_at}",
        "",
        "### 💬 Feedback",
        "",
        f"> {message.replace(chr(10), chr(10) + '> ')}",
    ])
    if forwarded_for:
        # Do not persist or expose the visitor IP in the feedback issue.
        pass

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_FEEDBACK_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "Torrent-Studio-Feedback",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers=headers,
                json={"title": title, "body": "\\n".join(body_lines)},
            )
    except httpx.HTTPError as exc:
        logger.warning("Feedback submission failed: %s", exc)
        raise HTTPException(502, "Feedback service is temporarily unavailable")

    if response.status_code != 201:
        try:
            detail = response.json().get("message")
        except Exception:
            detail = None
        logger.warning("GitHub feedback submission failed: HTTP %s %s", response.status_code, detail or "")
        raise HTTPException(502, "Feedback could not be submitted right now")

    try:
        issue = response.json()
    except Exception:
        issue = {}
    return {"submitted": True, "issueUrl": issue.get("html_url")}


@app.get("/api/qbt/settings")
async def qbt_settings():
    return {"isExternal": False, "host": "", "username": "", "connected": False, "version": "Seedr backend"}

@app.post("/api/qbt/settings")
async def update_qbt_settings(body: dict[str, Any]):
    return {"isExternal": False, "host": "", "username": "", "connected": False, "version": "Seedr backend"}

@app.post("/api/files/delete")
async def delete_file_compat(body: dict[str, Any]):
    return await seedr_file_delete(str(body.get("id") or ""))

@app.post("/api/files/rename")
async def rename_file_compat(body: dict[str, Any]):
    raise HTTPException(501, "Rename is not exposed by this Seedr API deployment")

@app.post("/api/files/move")
async def move_file_compat(body: dict[str, Any]):
    raise HTTPException(501, "Move is not exposed by this Seedr API deployment")

@app.post("/api/files/folder")
async def create_folder_compat(body: dict[str, Any]):
    raise HTTPException(501, "Folder creation is not exposed by this Seedr API deployment")

@app.post("/api/folders/share")
async def share_folder_compat(body: dict[str, Any]):
    raise HTTPException(501, "Sharing is not provided by Seedr")

@app.post("/api/users/switch")
async def switch_user(body: dict[str, Any]):
    return {"activeUser": {"id": "seedr-user", "name": "Seedr User", "email": "", "role": "admin", "avatar": ""}}

@app.post("/api/users/create")
async def create_user(body: dict[str, Any]):
    raise HTTPException(501, "Multi-user accounts are not provided by this deployment")

@app.get("/api/search/torrents/add")
async def search_add_info():
    return JSONResponse({"added": False, "reason": "Use the Seedr magnet action"}, status_code=405)

@app.post("/api/search/torrents/add")
async def search_torrent_add(body: dict[str, Any]):
    source = str(body.get("source") or "")
    magnet = source if source.lower().startswith("magnet:") else ""
    if not magnet and body.get("infoHash"):
        magnet = "magnet:?xt=urn:btih:" + str(body["infoHash"])
    if not magnet:
        return {"added": False, "reason": "no_magnet_or_info_hash"}
    result = await seedr_add(
        MagnetRequest(
            magnet=magnet,
            torrent_name=str(body.get("torrent_name") or "").strip() or None,
        )
    )
    return {"added": True, **result}
