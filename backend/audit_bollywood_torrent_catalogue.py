#!/usr/bin/env python3
"""Audit existing cached Bollywood torrent matches without making provider requests.

Dry-run by default. Use --apply to back up and atomically replace the matched cache.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path("/app/data")
CACHE_PATH = Path(__import__("os").getenv(
    "BOLLYWOOD_TORRENT_CATALOGUE_PATH",
    str(DATA_DIR / "bollywood_torrent_matched_catalogue.json"),
))
ADULT_TERMS = {
    "xxx", "porn", "porno", "pornographic", "erotic", "hentai",
    "blowjob", "handjob", "milf", "deepthroat", "sexvideo",
    "mommysboy", "mommys", "onlyfans", "camgirl", "camsex",
}
LANGUAGE_MARKERS = {
    "tamil", "malayalam", "telugu", "kannada", "marathi", "bengali",
    "punjabi", "gujarati", "urdu", "indonesian", "thai", "korean",
    "japanese", "russian", "spanish", "french", "german",
}


def normalize(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def tokens_in_order(needle: list[str], haystack: list[str]) -> bool:
    if not needle:
        return False
    pos = 0
    for token in haystack:
        if token == needle[pos]:
            pos += 1
            if pos == len(needle):
                return True
    return False


def year_from_title(value: Any) -> int | None:
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match.group()) if match else None


MAX_CACHED_TORRENT_SIZE_BYTES = 5 * 1024 * 1024 * 1024


def size_bytes(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip().casefold().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(b|bytes?|kb|kib|mb|mib|gb|gib|tb|tib)?", text)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2) or "b"
    factors = {"b": 1, "byte": 1, "bytes": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2, "gb": 1000**3, "gib": 1024**3, "tb": 1000**4, "tib": 1024**4}
    return int(amount * factors[unit])


def audit(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    match = row.get("torrentMatch")
    if not isinstance(match, dict):
        return ["missing-torrentMatch"]
    size = size_bytes(match.get("size"))
    if size <= 0:
        reasons.append("unknown-size")
    elif size > MAX_CACHED_TORRENT_SIZE_BYTES:
        reasons.append("over-5gb")
    movie_title = str(row.get("title") or "").strip()
    result_title = str(match.get("title") or "").strip()
    movie_tokens = normalize(movie_title).split()
    result_tokens = normalize(result_title).split()
    if not movie_tokens or not result_tokens:
        return ["missing-title"]

    if set(result_tokens) & ADULT_TERMS:
        reasons.append("adult-content-term")

    # Single-word titles are ambiguous: the exact title must lead the release name.
    if len(movie_tokens) == 1:
        if result_tokens[0] != movie_tokens[0]:
            reasons.append("ambiguous-single-word-title")
    elif not tokens_in_order(movie_tokens, result_tokens):
        reasons.append("title-words-not-in-order")

    expected_year = row.get("year")
    if expected_year is None:
        expected_year = year_from_title(row.get("releaseDate"))
    found_year = year_from_title(result_title)
    try:
        if expected_year and found_year and abs(int(expected_year) - found_year) > 1:
            reasons.append("year-mismatch")
    except (ValueError, TypeError):
        pass

    original_language = str(row.get("originalLanguage") or row.get("original_language") or "").casefold()
    if original_language in {"hi", "hindi"}:
        other_languages = set(result_tokens) & LANGUAGE_MARKERS
        if other_languages and not ({"hindi", "hin"} & set(result_tokens)):
            reasons.append("possible-language-mismatch:" + ",".join(sorted(other_languages)))
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="back up and write the cleaned cache")
    args = parser.parse_args()

    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read cache {CACHE_PATH}: {exc}", file=sys.stderr)
        return 2
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        print("ERROR: cache does not contain a records list", file=sys.stderr)
        return 2

    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            rejected.append({"title": None, "torrentTitle": None, "reasons": ["invalid-record"]})
            continue
        reasons = audit(row)
        if reasons:
            rejected.append({
                "title": row.get("title"),
                "year": row.get("year"),
                "torrentTitle": (row.get("torrentMatch") or {}).get("title") if isinstance(row.get("torrentMatch"), dict) else None,
                "reasons": reasons,
            })
        else:
            kept.append(row)

    print(json.dumps({
        "cache": str(CACHE_PATH),
        "mode": "apply" if args.apply else "dry-run",
        "before": len(records),
        "kept": len(kept),
        "rejected": len(rejected),
        "rejectedRecords": rejected,
    }, ensure_ascii=False, indent=2))

    if args.apply and rejected:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = CACHE_PATH.with_name(CACHE_PATH.name + ".pre-audit-" + stamp + ".bak")
        shutil.copy2(CACHE_PATH, backup)
        cleaned = dict(payload)
        cleaned["records"] = kept
        cleaned["generatedAt"] = datetime.now(timezone.utc).isoformat()
        cleaned["source"] = str(payload.get("source", "")) + " (audited title/year/language heuristics)"
        stats = dict(payload.get("stats") or {})
        stats["matchedTitles"] = len(kept)
        stats["auditRemoved"] = len(rejected)
        cleaned["stats"] = stats
        temp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        temp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(CACHE_PATH)
        print(f"APPLIED: kept {len(kept)}; removed {len(rejected)}; backup={backup}")
    elif args.apply:
        print("APPLIED: no records needed removal")
    else:
        print("Dry run only. Review the rejected list, then rerun with --apply to write a backup and clean the cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
