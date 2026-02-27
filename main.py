"""
main.py — FastAPI アプリ & 全エンドポイント
修正点:
  - asyncio.get_event_loop() → get_running_loop()
  - response_model と JSONResponse の不一致を解消
  - clean_fmt() の2重呼び出し を排除
  - /api/{video_id} に ETag + Cache-Control ヘッダーを追加
  - /health に Redis 疎通確認を追加
  - lifespan で起動/終了時の Redis 接続管理
  - 全エンドポイントに適切な status_code / summary / description を付与
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import yt_dlp as _ydlp
from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from models import (
    DownloadRequest,
    FormatRequest,
    M3U8Response,
    TaskResponse,
    TaskStatusResponse,
    VideoMetadata,
)
from tasks import celery_app, download_video
from utils import (
    DOWNLOADS_DIR,
    classify_error,
    extract_video_id,
    get_redis,
    get_video_metadata,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")

# ── レートリミッター ──────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


# ── lifespan（起動/終了処理）─────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時: Redis 接続確認
    try:
        r = get_redis()
        await r.ping()
        logger.info("Redis connection OK")
    except Exception as e:
        logger.warning("Redis not available at startup: %s", e)
    yield
    # 終了時: Redis 接続プールをクローズ
    try:
        r = get_redis()
        await r.aclose()
        logger.info("Redis connection closed")
    except Exception:
        pass


# ── FastAPI アプリ ────────────────────────────────────
app = FastAPI(
    title="UltraFastYTAPI",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────
# ヘルスチェック
# ─────────────────────────────────────────────────────
@app.get("/", tags=["Health"], summary="ルート")
async def root():
    return {"status": "ok", "service": "UltraFastYTAPI", "version": "2.0.0"}


@app.get("/health", tags=["Health"], summary="ヘルスチェック（Redis 疎通確認付き）")
async def health():
    redis_ok = False
    try:
        r = get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass
    return {"status": "ok", "redis": redis_ok}


# ─────────────────────────────────────────────────────
# 抽出系エンドポイント
# ─────────────────────────────────────────────────────
@app.post(
    "/get_all_formats",
    tags=["Extraction"],
    summary="全フォーマット取得",
    description="指定した YouTube URL の全フォーマット情報・メタデータを返す。",
)
@limiter.limit("12/minute")
async def get_all_formats(request: Request, body: FormatRequest):
    try:
        data = await get_video_metadata(body.url)
        return JSONResponse(content=data)
    except Exception as e:
        err = classify_error(e)
        raise HTTPException(status_code=err.get("code", 500), detail=err)


@app.post(
    "/extract_m3u8",
    tags=["Extraction"],
    summary="HLS/m3u8 抽出",
    description="ライブ配信向け。HLS マスタープレイリストとバリアントを返す。",
)
@limiter.limit("12/minute")
async def extract_m3u8(request: Request, body: FormatRequest):
    try:
        data = await get_video_metadata(body.url)
        hls_formats = [
            f for f in data.get("formats", [])
            if f.get("is_hls") or "m3u8" in f.get("protocol", "")
        ]

        # マスター m3u8 を manifest_url から取得
        manifest_urls = {f["manifest_url"] for f in hls_formats if f.get("manifest_url")}
        master = next(iter(manifest_urls), None)

        variants = [
            {
                "format_id":  f.get("format_id"),
                "resolution": f.get("resolution"),
                "tbr":        f.get("tbr"),
                "url":        f.get("url"),
                "vcodec":     f.get("vcodec"),
                "acodec":     f.get("acodec"),
                "ext":        f.get("ext"),
            }
            for f in sorted(hls_formats, key=lambda x: x.get("tbr") or 0, reverse=True)
        ]
        return JSONResponse(content={
            "id":            data["id"],
            "title":         data["title"],
            "is_live":       data.get("is_live", False),
            "master_m3u8":   master,
            "variant_m3u8s": variants,
            "hls_formats":   hls_formats,
        })
    except Exception as e:
        err = classify_error(e)
        raise HTTPException(status_code=err.get("code", 500), detail=err)


# ─────────────────────────────────────────────────────
# ダウンロード系エンドポイント
# ─────────────────────────────────────────────────────
@app.post(
    "/start_download",
    response_model=TaskResponse,
    tags=["Download"],
    summary="ダウンロードキュー登録",
    description="Celery タスクをキューに積む。即座に task_id を返す。",
    status_code=202,
)
@limiter.limit("6/minute")
async def start_download(request: Request, body: DownloadRequest):
    try:
        task = download_video.apply_async(
            args=[body.url, body.format_selector, body.filename],
            queue="downloads",
        )
        return JSONResponse(
            status_code=202,
            content={
                "task_id": task.id,
                "status":  "queued",
                "message": f"queued. poll /task_status/{task.id}",
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.get(
    "/task_status/{task_id}",
    response_model=TaskStatusResponse,
    tags=["Download"],
    summary="タスク進捗確認",
)
async def task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    state  = result.state

    if state == "PENDING":
        return JSONResponse(content={"task_id": task_id, "status": "queued", "progress": 0})

    if state in ("STARTED", "PROGRESS"):
        meta = result.info or {}
        return JSONResponse(content={
            "task_id":      task_id,
            "status":       state.lower(),
            "progress":     meta.get("progress", 0),
            "progress_str": meta.get("progress_str", ""),
        })

    if state == "SUCCESS":
        res = result.result or {}
        return JSONResponse(content={
            "task_id":   task_id,
            "status":    "success",
            "progress":  100,
            "file_path": res.get("file_path"),
            "filename":  res.get("filename"),
            "result":    res,
        })

    if state == "FAILURE":
        meta    = result.info
        err_str = str(meta) if not isinstance(meta, dict) else meta.get("error", str(meta))
        return JSONResponse(
            status_code=500,
            content={"task_id": task_id, "status": "failure", "error": err_str},
        )

    return JSONResponse(content={"task_id": task_id, "status": state.lower()})


@app.get(
    "/download/{task_id}",
    tags=["Download"],
    summary="完了ファイルをストリーム配信",
)
async def serve_file(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    if result.state != "SUCCESS":
        raise HTTPException(
            status_code=404,
            detail=f"task not complete (state: {result.state})",
        )
    res       = result.result or {}
    file_path = res.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="file not found")
    # パストラバーサル対策
    if not os.path.realpath(file_path).startswith(os.path.realpath(DOWNLOADS_DIR)):
        raise HTTPException(status_code=403, detail="access denied")
    return FileResponse(
        path=file_path,
        filename=os.path.basename(file_path),
        media_type="application/octet-stream",
    )


# ─────────────────────────────────────────────────────
# 動画情報エンドポイント（メイン）
# ─────────────────────────────────────────────────────
@app.get(
    "/api/{video_id}",
    tags=["Video Info"],
    summary="動画詳細情報（フォーマット・関連動画付き）",
)
@limiter.limit("20/minute")
async def video_info(request: Request, video_id: str, response: Response):
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        data = await get_video_metadata(url)
    except Exception as e:
        err = classify_error(e)
        raise HTTPException(status_code=err.get("code", 500), detail=err)

    # 関連動画取得（タイムアウト 10s でフォールバック）
    related = await _fetch_related(video_id)

    formats = data.get("formats", [])

    def clean_fmt(f: dict) -> dict:
        return {
            "itag":        f.get("format_id"),
            "ext":         f.get("ext"),
            "quality":     f.get("format_note") or f.get("quality_note"),
            "resolution":  f.get("resolution"),
            "fps":         f.get("fps"),
            "vcodec":      (f.get("vcodec") or "none").split(".")[0],
            "acodec":      (f.get("acodec") or "none").split(".")[0],
            "bitrate_kbps": round(f["tbr"]) if f.get("tbr") else None,
            "size_bytes":  f.get("filesize_approx"),
            "protocol":    f.get("protocol"),
            "url":         f.get("url") or f.get("manifest_url"),
        }

    # clean_fmt の2重呼び出しを排除（1フォーマット1回のみ計算）
    cleaned: list[dict] = [clean_fmt(f) for f in formats if f.get("format_id")]
    is_hls_fn  = lambda f: f.get("is_hls")
    is_dash_fn = lambda f: f.get("is_dash")
    has_video  = lambda f: f.get("vcodec") and f.get("vcodec") != "none"
    has_audio  = lambda f: f.get("acodec") and f.get("acodec") != "none"

    video_formats = sorted(
        [c for f, c in zip(formats, cleaned) if has_video(f) and not is_hls_fn(f) and not is_dash_fn(f)],
        key=lambda x: x.get("resolution") or "", reverse=True,
    )
    audio_formats = sorted(
        [c for f, c in zip(formats, cleaned) if not has_video(f) and has_audio(f) and not is_hls_fn(f)],
        key=lambda x: x.get("bitrate_kbps") or 0, reverse=True,
    )
    hls_formats = sorted(
        [c for f, c in zip(formats, cleaned) if is_hls_fn(f)],
        key=lambda x: x.get("bitrate_kbps") or 0, reverse=True,
    )
    dash_formats = sorted(
        [c for f, c in zip(formats, cleaned) if is_dash_fn(f)],
        key=lambda x: x.get("bitrate_kbps") or 0, reverse=True,
    )
    # itag をキーにした辞書（1回のループで構築）
    all_by_itag: dict[str, dict] = {c["itag"]: c for c in cleaned if c.get("itag")}

    # 日付フォーマット
    raw_date    = data.get("upload_date", "")
    upload_date = (
        f"{raw_date[:4]}/{raw_date[4:6]}/{raw_date[6:]}"
        if raw_date and len(raw_date) == 8
        else raw_date
    )

    # チャプター
    chapters = [
        {
            "title":      c.get("title"),
            "start_sec":  c.get("start_time"),
            "end_sec":    c.get("end_time"),
            "start_time": _sec_to_hms(c.get("start_time")),
            "end_time":   _sec_to_hms(c.get("end_time")),
        }
        for c in (data.get("chapters") or [])
    ]

    # ETag: レスポンスキャッシュ用（video_id + upload_date のハッシュ）
    etag = hashlib.md5(f"{video_id}:{upload_date}".encode()).hexdigest()
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Cache-Control"] = (
        "no-store" if data.get("is_live") else "public, max-age=1800"
    )

    return JSONResponse(
        content={
            "video_id":    video_id,
            "title":       data.get("title"),
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "upload_date": upload_date,
            "duration_sec": data.get("duration"),
            "duration":    data.get("duration_string"),
            "view_count":  data.get("view_count"),
            "like_count":  data.get("like_count"),
            "comment_count": data.get("comment_count"),
            "age_limit":   data.get("age_limit"),
            "is_live":     data.get("is_live", False),
            "was_live":    data.get("was_live", False),
            "availability": data.get("availability"),
            "thumbnail":   data.get("thumbnail"),
            "thumbnails":  [
                {"id": t.get("id"), "url": t.get("url"), "width": t.get("width"), "height": t.get("height")}
                for t in (data.get("thumbnails") or [])
            ],
            "uploader": {
                "name":             data.get("uploader"),
                "id":               data.get("uploader_id"),
                "channel_id":       data.get("channel_id"),
                "channel_url":      data.get("channel_url"),
                "subscriber_count": data.get("channel_follower_count"),
                "avatar_url":       data.get("uploader_avatar_url") or _extract_avatar(data),
                "is_verified":      data.get("channel_is_verified", False),
            },
            "description": data.get("description") or "",
            "tags":        data.get("tags") or [],
            "categories":  data.get("categories") or [],
            "chapters":    chapters,
            "stream_urls": {
                "m3u8":          data.get("m3u8_urls", []),
                "dash_manifest": data.get("dash_manifest_url"),
            },
            "formats": {
                "summary": {
                    "total": len(formats),
                    "video": len(video_formats),
                    "audio": len(audio_formats),
                    "hls":   len(hls_formats),
                    "dash":  len(dash_formats),
                },
                "video":       video_formats,
                "audio":       audio_formats,
                "hls":         hls_formats,
                "dash":        dash_formats,
                "all_by_itag": all_by_itag,
            },
            "related_videos": related,
        }
    )


# ─────────────────────────────────────────────────────
# 内部ヘルパー
# ─────────────────────────────────────────────────────
def _sec_to_hms(sec: Any) -> str | None:
    if sec is None:
        return None
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _extract_avatar(data: dict) -> str | None:
    for t in (data.get("thumbnails") or []):
        url = t.get("url", "")
        if "ggpht" in url or ("ytimg.com/vi/" not in url and "photo" in url):
            return url
    return None


async def _fetch_related(video_id: str) -> list:
    """
    関連動画リストを取得する。
    修正: asyncio.get_event_loop() → get_running_loop()
    """
    def _get() -> list:
        opts = {
            "quiet": True,
            "extract_flat": True,
            "skip_download": True,
            "playlistend": 12,
        }
        results = []
        try:
            with _ydlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}",
                    download=False,
                )
                for e in (info.get("entries") or [])[:12]:
                    eid = e.get("id") or e.get("url", "").split("v=")[-1]
                    if not eid or eid == video_id:
                        continue
                    results.append({
                        "video_id":    eid,
                        "title":       e.get("title"),
                        "uploader":    e.get("uploader") or e.get("channel"),
                        "duration_sec": e.get("duration"),
                        "view_count":  e.get("view_count"),
                        "thumbnail":   f"https://i.ytimg.com/vi/{eid}/hqdefault.jpg",
                        "youtube_url": f"https://www.youtube.com/watch?v={eid}",
                    })
        except Exception as exc:
            logger.debug("_fetch_related failed: %s", exc)
        return results

    loop = asyncio.get_running_loop()  # 修正: get_event_loop() → get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _get), timeout=10)
    except asyncio.TimeoutError:
        logger.debug("_fetch_related timed out for %s", video_id)
        return []
    except Exception as exc:
        logger.debug("_fetch_related error: %s", exc)
        return []


# ─────────────────────────────────────────────────────
# グローバル例外ハンドラー
# ─────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    err = classify_error(exc)
    return JSONResponse(status_code=err.get("code", 500), content=err)
