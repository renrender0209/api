"""
utils.py — yt-dlp helpers, Redis cache, aria2c opts
修正点:
  - asyncio.get_event_loop() → get_running_loop()
  - リトライ時 extractor_args を深くマージ（PO_TOKEN/VISITOR_DATA 保持）
  - aria2c 引数のエンダッシュ(–) → ASCII ダブルハイフン(--)
  - hash(url) → hashlib.md5（マルチプロセス間でキャッシュキー一致）
  - js_engine "auto" を環境変数未設定時は opts から除外
  - cookie_path フォールバックロジックを明確化
  - Redis 接続プール化・再接続ハンドリング強化
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Optional

import yt_dlp
import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool

logger = logging.getLogger("uvicorn.error")

# ── 環境変数 ─────────────────────────────────────────
REDIS_URL          = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL          = int(os.getenv("CACHE_TTL", "1800"))
PROXY              = os.getenv("YT_PROXY") or None
COOKIES_FILE       = os.getenv("YT_COOKIES_FILE") or None
PO_TOKEN           = os.getenv("YT_PO_TOKEN") or None
PO_TOKEN_VISITOR_DATA = os.getenv("YT_VISITOR_DATA") or None
DOWNLOADS_DIR      = os.getenv("DOWNLOADS_DIR", "/downloads")
_JS_ENGINE         = os.getenv("YT_JS_ENGINE") or None   # 未設定なら yt-dlp デフォルト

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ── Redis 接続プール ──────────────────────────────────
_pool: Optional[ConnectionPool] = None
_redis_client: Optional[aioredis.Redis] = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            REDIS_URL,
            decode_responses=True,
            max_connections=20,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _pool


def get_redis() -> aioredis.Redis:
    """Redis クライアントをプールから取得（同期的に返す）。"""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(connection_pool=_get_pool())
    return _redis_client


# ── キャッシュ ────────────────────────────────────────
async def cache_get(key: str) -> Optional[Any]:
    try:
        r = get_redis()
        val = await r.get(key)
        if val:
            return json.loads(val)
    except Exception as e:
        logger.warning("Cache GET error: %s", e)
    return None


async def cache_set(key: str, value: Any, ttl: int = CACHE_TTL) -> None:
    try:
        r = get_redis()
        await r.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        logger.warning("Cache SET error: %s", e)


# ── yt-dlp オプション ─────────────────────────────────
def _base_extractor_args() -> dict:
    """ベースの extractor_args を生成（PO_TOKEN / VISITOR_DATA 込み）。"""
    ea: dict[str, Any] = {
        "player_client": ["android", "web", "ios"],
        "player_skip": [],
    }
    if PO_TOKEN:
        ea["po_token"] = [f"web+{PO_TOKEN}"]
    if PO_TOKEN_VISITOR_DATA:
        ea["visitor_data"] = [PO_TOKEN_VISITOR_DATA]
    return {"youtube": ea}


def _deep_merge_extractor_args(base: dict, override: dict) -> dict:
    """
    extractor_args を深くマージする。
    override の player_client だけ差し替えつつ、
    po_token / visitor_data などは base から継承する。
    """
    merged: dict[str, Any] = {}
    all_keys = set(base) | set(override)
    for k in all_keys:
        if k in base and k in override:
            if isinstance(base[k], dict) and isinstance(override[k], dict):
                merged[k] = {**base[k], **override[k]}
            else:
                merged[k] = override[k]
        elif k in override:
            merged[k] = override[k]
        else:
            merged[k] = base[k]
    return merged


def build_ydl_opts(extra: Optional[dict] = None) -> dict:
    """
    yt-dlp オプション辞書を生成する。
    extra に extractor_args が含まれる場合は深くマージして
    PO_TOKEN / VISITOR_DATA が失われないようにする。
    """
    base_ea = _base_extractor_args()

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "extract_flat": False,
        "skip_download": True,
        "extractor_args": base_ea,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "concurrent_fragment_downloads": 4,
        "format": "bestvideo+bestaudio/bestvideo/best",
        "format_sort": ["res:1080", "ext:mp4:m4a", "codec:avc:m4a"],
    }

    # js_engine は設定がある場合のみ追加（"auto" は無効値のため除外）
    if _JS_ENGINE:
        opts["js_engine"] = _JS_ENGINE

    # プロキシ
    if PROXY:
        opts["proxy"] = PROXY

    # クッキーファイル：明示指定 → /tmp/cookies.txt の順で存在確認
    cookie_path: Optional[str] = None
    for candidate in filter(None, [COOKIES_FILE, "/tmp/cookies.txt"]):
        if os.path.exists(candidate):
            cookie_path = candidate
            break
    if cookie_path:
        opts["cookiefile"] = cookie_path

    # extra を適用（extractor_args は深くマージ）
    if extra:
        for k, v in extra.items():
            if k == "extractor_args" and isinstance(v, dict):
                opts["extractor_args"] = _deep_merge_extractor_args(
                    opts["extractor_args"], v
                )
            else:
                opts[k] = v

    return opts


# ── フォーマットパーサー ───────────────────────────────
def parse_format(fmt: dict) -> dict:
    protocol = fmt.get("protocol", "")
    width    = fmt.get("width")
    height   = fmt.get("height")
    return {
        "format_id":      str(fmt.get("format_id", "")),
        "ext":            fmt.get("ext", ""),
        "protocol":       protocol,
        "quality_note":   fmt.get("format_note", ""),
        "resolution":     fmt.get("resolution") or (
            f"{width}x{height}" if width and height else None
        ),
        "fps":            fmt.get("fps"),
        "vcodec":         fmt.get("vcodec"),
        "acodec":         fmt.get("acodec"),
        "filesize_approx": fmt.get("filesize") or fmt.get("filesize_approx"),
        "tbr":            fmt.get("tbr"),
        "vbr":            fmt.get("vbr"),
        "abr":            fmt.get("abr"),
        "url":            fmt.get("url"),
        "manifest_url":   fmt.get("manifest_url"),
        "is_hls":         "m3u8" in protocol,
        "is_dash":        "dash" in protocol,
        "is_live":        fmt.get("is_from_start", False),
        "height":         height,
        "width":          width,
        "format_note":    fmt.get("format_note"),
    }


# ── 非同期 extract_info ───────────────────────────────
async def extract_info_async(url: str, opts: Optional[dict] = None) -> dict:
    """
    yt-dlp で動画情報を非同期取得。
    失敗時は player_client を変えながら最大3回試みる。
    各リトライでも PO_TOKEN / VISITOR_DATA が保持される。
    """

    def _extract(extra_opts: Optional[dict] = None) -> dict:
        ydl_opts = build_ydl_opts({**(opts or {}), **(extra_opts or {})})
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    # Python 3.10+ では get_running_loop() を使用
    loop = asyncio.get_running_loop()

    # 1st: android + web + ios（デフォルト）
    try:
        return await loop.run_in_executor(None, _extract)
    except yt_dlp.utils.DownloadError:
        pass

    # 2nd: web のみ
    try:
        return await loop.run_in_executor(
            None,
            lambda: _extract({"extractor_args": {"youtube": {"player_client": ["web"]}}}),
        )
    except yt_dlp.utils.DownloadError:
        pass

    # 3rd: ios のみ（最終手段）
    return await loop.run_in_executor(
        None,
        lambda: _extract({"extractor_args": {"youtube": {"player_client": ["ios"]}}}),
    )


# ── メタデータ取得（キャッシュ付き）─────────────────────
async def get_video_metadata(url: str) -> dict:
    vid_id = extract_video_id(url)
    # hash(url) はプロセス間で一致しない → hashlib.md5 を使用
    if vid_id:
        cache_key = f"ytmeta:{vid_id}"
    else:
        cache_key = f"ytmeta:url:{hashlib.md5(url.encode()).hexdigest()}"

    cached = await cache_get(cache_key)
    if cached:
        return cached

    info = await extract_info_async(url)
    if not info:
        raise ValueError("yt-dlp returned no info")

    formats    = [parse_format(f) for f in info.get("formats", [])]
    m3u8_urls  = list({f["url"] for f in formats if f["is_hls"] and f.get("url")})

    result = {
        "id":                    info.get("id", ""),
        "title":                 info.get("title", ""),
        "uploader":              info.get("uploader"),
        "uploader_id":           info.get("uploader_id"),
        "channel_id":            info.get("channel_id"),
        "channel_url":           info.get("channel_url"),
        "channel_follower_count": info.get("channel_follower_count"),
        "channel_is_verified":   info.get("channel_is_verified", False),
        "uploader_avatar_url":   info.get("uploader_avatar_url"),
        "duration":              info.get("duration"),
        "duration_string":       info.get("duration_string"),
        "view_count":            info.get("view_count"),
        "like_count":            info.get("like_count"),
        "comment_count":         info.get("comment_count"),
        "age_limit":             info.get("age_limit", 0),
        "availability":          info.get("availability"),
        "description":           info.get("description") or "",
        "thumbnail":             info.get("thumbnail"),
        "thumbnails":            info.get("thumbnails") or [],
        "upload_date":           info.get("upload_date"),
        "release_timestamp":     info.get("release_timestamp"),
        "tags":                  info.get("tags") or [],
        "categories":            info.get("categories") or [],
        "chapters":              info.get("chapters") or [],
        "is_live":               bool(info.get("is_live")),
        "was_live":              bool(info.get("was_live")),
        "formats":               formats,
        "m3u8_urls":             m3u8_urls,
        "dash_manifest_url":     info.get("dash_manifest_url"),
    }

    ttl = 300 if result["is_live"] else CACHE_TTL
    await cache_set(cache_key, result, ttl=ttl)
    return result


# ── ユーティリティ ────────────────────────────────────
def extract_video_id(url: str) -> Optional[str]:
    patterns = [r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})"]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def build_aria2c_opts(download_dir: str, filename: str) -> dict:
    """
    aria2c を使ったダウンロードオプションを返す。
    修正: エンダッシュ(–) → ASCII ダブルハイフン(--)
    """
    return {
        "skip_download": False,
        "external_downloader": "aria2c",
        "external_downloader_args": {
            "aria2c": [
                "-x", "16",
                "-s", "16",
                "-k", "1M",
                "--max-tries=8",           # 修正: – → --
                "--retry-wait=3",          # 修正: – → --
                "--connect-timeout=10",    # 修正: – → --
                "--timeout=30",            # 修正: – → --
                "--auto-file-renaming=false",  # 修正: – → --
                "--console-log-level=warn",    # 修正: – → --
            ]
        },
        "outtmpl": os.path.join(download_dir, filename),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }


def classify_error(e: Exception) -> dict:
    msg = str(e)
    if "403" in msg or "Forbidden" in msg:
        return {"error": "Access denied (403)", "detail": "Try adding cookies or a proxy.", "code": 403}
    if "429" in msg or "Too Many Requests" in msg:
        return {"error": "Rate limited (429)", "detail": "Slow down or rotate proxies.", "code": 429}
    if "geo" in msg.lower() or "not available in your country" in msg.lower():
        return {"error": "Geo-blocked", "detail": "Use a proxy in an allowed region.", "code": 451}
    if "Private video" in msg:
        return {"error": "Private video", "detail": "This video is private.", "code": 403}
    if "Sign in" in msg or "age" in msg.lower():
        return {"error": "Age-restricted", "detail": "Provide cookies via YT_COOKIES_FILE.", "code": 403}
    if "Requested format is not available" in msg:
        return {"error": "Format not available", "detail": "No matching format found.", "code": 404}
    if "This live event will begin" in msg:
        return {"error": "Stream not started", "detail": "Live stream has not started yet.", "code": 425}
    if "nsig" in msg.lower():
        return {"error": "nsig extraction failed", "detail": "Update yt-dlp: pip install -U yt-dlp", "code": 500}
    return {"error": "Extraction failed", "detail": msg[:300], "code": 500}
