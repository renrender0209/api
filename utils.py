import asyncio
import json
import os
import re
import logging
from typing import Optional, Any
import yt_dlp
import redis.asyncio as aioredis

logger = logging.getLogger(“uvicorn.error”)

REDIS_URL = os.getenv(“REDIS_URL”, “redis://localhost:6379/0”)
CACHE_TTL = int(os.getenv(“CACHE_TTL”, “1800”))
PROXY = os.getenv(“YT_PROXY”, None)
COOKIES_FILE = os.getenv(“YT_COOKIES_FILE”, None)
PO_TOKEN = os.getenv(“YT_PO_TOKEN”, None)
PO_TOKEN_VISITOR_DATA = os.getenv(“YT_VISITOR_DATA”, None)
DOWNLOADS_DIR = os.getenv(“DOWNLOADS_DIR”, “/downloads”)

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

_redis_client: Optional[aioredis.Redis] = None

async def get_redis() -> aioredis.Redis:
global _redis_client
if _redis_client is None:
_redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
return _redis_client

async def cache_get(key: str) -> Optional[Any]:
try:
r = await get_redis()
val = await r.get(key)
if val:
return json.loads(val)
except Exception as e:
logger.warning(f”Cache GET error: {e}”)
return None

async def cache_set(key: str, value: Any, ttl: int = CACHE_TTL):
try:
r = await get_redis()
await r.setex(key, ttl, json.dumps(value, default=str))
except Exception as e:
logger.warning(f”Cache SET error: {e}”)

def build_ydl_opts(extra: dict = None) -> dict:
extractor_args = {“youtube”: {“player_client”: [“android”, “web”, “ios”], “player_skip”: []}}
if PO_TOKEN:
extractor_args[“youtube”][“po_token”] = [f”web+{PO_TOKEN}”]
if PO_TOKEN_VISITOR_DATA:
extractor_args[“youtube”][“visitor_data”] = [PO_TOKEN_VISITOR_DATA]

```
opts = {
    "quiet": True,
    "no_warnings": False,
    "extract_flat": False,
    "skip_download": True,
    "extractor_args": extractor_args,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    },
    "socket_timeout": 30,
    "retries": 5,
    "fragment_retries": 10,
    "file_access_retries": 5,
    "concurrent_fragment_downloads": 4,
    "js_engine": os.getenv("YT_JS_ENGINE", "auto"),
    "format": "bestvideo+bestaudio/bestvideo/best",
    "format_sort": ["res:1080", "ext:mp4:m4a", "codec:avc:m4a"],
}

if PROXY:
    opts["proxy"] = PROXY
cookie_path = COOKIES_FILE
if not cookie_path or not os.path.exists(cookie_path):
    cookie_path = "/tmp/cookies.txt"
if cookie_path and os.path.exists(cookie_path):
    opts["cookiefile"] = cookie_path
if extra:
    opts.update(extra)
return opts
```

def parse_format(fmt: dict) -> dict:
protocol = fmt.get(“protocol”, “”)
return {
“format_id”: str(fmt.get(“format_id”, “”)),
“ext”: fmt.get(“ext”, “”),
“protocol”: protocol,
“quality_note”: fmt.get(“format_note”, “”),
“resolution”: fmt.get(“resolution”) or (f”{fmt[‘width’]}x{fmt[‘height’]}” if fmt.get(“width”) and fmt.get(“height”) else None),
“fps”: fmt.get(“fps”),
“vcodec”: fmt.get(“vcodec”),
“acodec”: fmt.get(“acodec”),
“filesize_approx”: fmt.get(“filesize”) or fmt.get(“filesize_approx”),
“tbr”: fmt.get(“tbr”),
“vbr”: fmt.get(“vbr”),
“abr”: fmt.get(“abr”),
“url”: fmt.get(“url”),
“manifest_url”: fmt.get(“manifest_url”),
“is_hls”: “m3u8” in protocol,
“is_dash”: “dash” in protocol,
“is_live”: fmt.get(“is_from_start”, False),
“height”: fmt.get(“height”),
“width”: fmt.get(“width”),
“format_note”: fmt.get(“format_note”),
}

async def extract_info_async(url: str, opts: dict = None) -> dict:
def _extract(extra_opts=None):
ydl_opts = build_ydl_opts({**(opts or {}), **(extra_opts or {})})
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
return ydl.extract_info(url, download=False)

```
loop = asyncio.get_event_loop()

# 1st try: android + web + ios
try:
    return await loop.run_in_executor(None, _extract)
except yt_dlp.utils.DownloadError:
    pass

# 2nd try: web only (一部動画はandroidクライアントで失敗する)
try:
    return await loop.run_in_executor(None, lambda: _extract(
        {"extractor_args": {"youtube": {"player_client": ["web"]}}}
    ))
except yt_dlp.utils.DownloadError:
    pass

# 3rd try: ios only
return await loop.run_in_executor(None, lambda: _extract(
    {"extractor_args": {"youtube": {"player_client": ["ios"]}}}
))
```

async def get_video_metadata(url: str) -> dict:
vid_id = extract_video_id(url)
cache_key = f”ytmeta:{vid_id}” if vid_id else f”ytmeta:url:{hash(url)}”
cached = await cache_get(cache_key)
if cached:
return cached

```
info = await extract_info_async(url)
if not info:
    raise ValueError("yt-dlp returned no info")

formats = [parse_format(f) for f in info.get("formats", [])]
m3u8_urls = list({f["url"] for f in formats if f["is_hls"] and f.get("url")})

result = {
    "id": info.get("id", ""),
    "title": info.get("title", ""),
    "uploader": info.get("uploader"),
    "uploader_id": info.get("uploader_id"),
    "channel_id": info.get("channel_id"),
    "channel_url": info.get("channel_url"),
    "channel_follower_count": info.get("channel_follower_count"),
    "channel_is_verified": info.get("channel_is_verified", False),
    "uploader_avatar_url": info.get("uploader_avatar_url"),
    "duration": info.get("duration"),
    "duration_string": info.get("duration_string"),
    "view_count": info.get("view_count"),
    "like_count": info.get("like_count"),
    "comment_count": info.get("comment_count"),
    "age_limit": info.get("age_limit", 0),
    "availability": info.get("availability"),
    "description": info.get("description") or "",
    "thumbnail": info.get("thumbnail"),
    "thumbnails": info.get("thumbnails") or [],
    "upload_date": info.get("upload_date"),
    "release_timestamp": info.get("release_timestamp"),
    "tags": info.get("tags") or [],
    "categories": info.get("categories") or [],
    "chapters": info.get("chapters") or [],
    "is_live": bool(info.get("is_live")),
    "was_live": bool(info.get("was_live")),
    "formats": formats,
    "m3u8_urls": m3u8_urls,
    "dash_manifest_url": info.get("dash_manifest_url"),
}

ttl = 300 if result["is_live"] else CACHE_TTL
await cache_set(cache_key, result, ttl=ttl)
return result
```

def extract_video_id(url: str) -> Optional[str]:
patterns = [r”(?:v=|/v/|youtu.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})”]
for p in patterns:
m = re.search(p, url)
if m:
return m.group(1)
return None

def build_aria2c_opts(download_dir: str, filename: str) -> dict:
return {
“skip_download”: False,
“external_downloader”: “aria2c”,
“external_downloader_args”: {
“aria2c”: [”-x”, “16”, “-s”, “16”, “-k”, “1M”, “–max-tries=8”, “–retry-wait=3”,
“–connect-timeout=10”, “–timeout=30”, “–auto-file-renaming=false”, “–console-log-level=warn”]
},
“outtmpl”: os.path.join(download_dir, filename),
“merge_output_format”: “mp4”,
“postprocessors”: [{“key”: “FFmpegVideoConvertor”, “preferedformat”: “mp4”}],
}

def classify_error(e: Exception) -> dict:
msg = str(e)
if “403” in msg or “Forbidden” in msg:
return {“error”: “Access denied (403)”, “detail”: “Try adding cookies or a proxy.”, “code”: 403}
if “429” in msg or “Too Many Requests” in msg:
return {“error”: “Rate limited (429)”, “detail”: “Slow down or rotate proxies.”, “code”: 429}
if “geo” in msg.lower() or “not available in your country” in msg.lower():
return {“error”: “Geo-blocked”, “detail”: “Use a proxy in an allowed region.”, “code”: 451}
if “Private video” in msg:
return {“error”: “Private video”, “detail”: “This video is private.”, “code”: 403}
if “Sign in” in msg or “age” in msg.lower():
return {“error”: “Age-restricted”, “detail”: “Provide cookies via YT_COOKIES_FILE.”, “code”: 403}
if “Requested format is not available” in msg:
return {“error”: “Format not available”, “detail”: “No matching format found. The video may be restricted or region-locked.”, “code”: 404}
if “This live event will begin” in msg:
return {“error”: “Stream not started”, “detail”: “Live stream has not started yet.”, “code”: 425}
return {“error”: “Extraction failed”, “detail”: msg[:300], “code”: 500}