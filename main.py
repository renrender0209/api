import os
import logging
import asyncio
import yt_dlp as _ydlp

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from celery.result import AsyncResult

from models import FormatRequest, DownloadRequest, VideoMetadata, M3U8Response, TaskResponse, TaskStatusResponse, TaskStatus
from utils import get_video_metadata, classify_error, DOWNLOADS_DIR
from tasks import celery_app, download_video

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

app = FastAPI(title="UltraFastYTAPI", version="1.0.0", docs_url="/docs", redoc_url="/redoc")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "UltraFastYTAPI", "version": "1.0.0"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}


@app.post("/get_all_formats", response_model=VideoMetadata, tags=["Extraction"])
@limiter.limit("12/minute")
async def get_all_formats(request: Request, body: FormatRequest):
    try:
        data = await get_video_metadata(body.url)
        return JSONResponse(content=data)
    except Exception as e:
        err = classify_error(e)
        raise HTTPException(status_code=err.get("code", 500), detail=err)


@app.post("/extract_m3u8", response_model=M3U8Response, tags=["Extraction"])
@limiter.limit("12/minute")
async def extract_m3u8(request: Request, body: FormatRequest):
    try:
        data = await get_video_metadata(body.url)
        hls_formats = [f for f in data.get("formats", []) if f.get("is_hls") or "m3u8" in f.get("protocol", "")]
        master = None
        manifest_urls = {f.get("manifest_url") for f in hls_formats if f.get("manifest_url")}
        if manifest_urls:
            master = next(iter(manifest_urls))
        variants = [
            {"format_id": f.get("format_id"), "resolution": f.get("resolution"), "tbr": f.get("tbr"),
             "url": f.get("url"), "vcodec": f.get("vcodec"), "acodec": f.get("acodec"), "ext": f.get("ext")}
            for f in sorted(hls_formats, key=lambda x: x.get("tbr") or 0, reverse=True)
        ]
        return JSONResponse(content={"id": data["id"], "title": data["title"], "is_live": data.get("is_live", False),
                                     "master_m3u8": master, "variant_m3u8s": variants, "hls_formats": hls_formats})
    except Exception as e:
        err = classify_error(e)
        raise HTTPException(status_code=err.get("code", 500), detail=err)


@app.post("/start_download", response_model=TaskResponse, tags=["Download"])
@limiter.limit("6/minute")
async def start_download(request: Request, body: DownloadRequest):
    try:
        task = download_video.apply_async(args=[body.url, body.itag, body.filename], queue="downloads")
        return JSONResponse(content={"task_id": task.id, "status": "queued",
                                     "message": f"queued. poll /task_status/{task.id}"})
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.get("/task_status/{task_id}", response_model=TaskStatusResponse, tags=["Download"])
async def task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    state = result.state
    if state == "PENDING":
        return JSONResponse(content={"task_id": task_id, "status": "queued", "progress": 0})
    if state in ("STARTED", "PROGRESS"):
        meta = result.info or {}
        return JSONResponse(content={"task_id": task_id, "status": state.lower(),
                                     "progress": meta.get("progress", 0), "progress_str": meta.get("progress_str", "")})
    if state == "SUCCESS":
        res = result.result or {}
        return JSONResponse(content={"task_id": task_id, "status": "success", "progress": 100,
                                     "file_path": res.get("file_path"), "filename": res.get("filename"), "result": res})
    if state == "FAILURE":
        meta = result.info
        err_str = str(meta) if not isinstance(meta, dict) else meta.get("error", str(meta))
        return JSONResponse(content={"task_id": task_id, "status": "failure", "error": err_str}, status_code=500)
    return JSONResponse(content={"task_id": task_id, "status": state.lower()})


@app.get("/download/{task_id}", tags=["Download"])
async def serve_file(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    if result.state != "SUCCESS":
        raise HTTPException(status_code=404, detail=f"task not complete (state: {result.state})")
    res = result.result or {}
    file_path = res.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="file not found")
    if not os.path.realpath(file_path).startswith(os.path.realpath(DOWNLOADS_DIR)):
        raise HTTPException(status_code=403, detail="access denied")
    return FileResponse(path=file_path, filename=os.path.basename(file_path), media_type="application/octet-stream")


@app.get("/api/{video_id}", tags=["Video Info"])
@limiter.limit("20/minute")
async def video_info(request: Request, video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        data = await get_video_metadata(url)
    except Exception as e:
        err = classify_error(e)
        raise HTTPException(status_code=err.get("code", 500), detail=err)

    related = await _fetch_related(video_id)
    formats = data.get("formats", [])

    def clean_fmt(f: dict) -> dict:
        return {
            "itag": f.get("format_id"),
            "ext": f.get("ext"),
            "quality": f.get("format_note") or f.get("quality_note"),
            "resolution": f.get("resolution"),
            "fps": f.get("fps"),
            "vcodec": (f.get("vcodec") or "none").split(".")[0],
            "acodec": (f.get("acodec") or "none").split(".")[0],
            "bitrate_kbps": round(f["tbr"]) if f.get("tbr") else None,
            "size_bytes": f.get("filesize_approx"),
            "protocol": f.get("protocol"),
            "url": f.get("url") or f.get("manifest_url"),
        }

    video_formats = sorted([clean_fmt(f) for f in formats if f.get("vcodec") and f.get("vcodec") != "none" and not f.get("is_hls") and not f.get("is_dash")], key=lambda x: x.get("resolution") or "", reverse=True)
    audio_formats = sorted([clean_fmt(f) for f in formats if (not f.get("vcodec") or f.get("vcodec") == "none") and f.get("acodec") and f.get("acodec") != "none" and not f.get("is_hls")], key=lambda x: x.get("bitrate_kbps") or 0, reverse=True)
    hls_formats   = sorted([clean_fmt(f) for f in formats if f.get("is_hls")],  key=lambda x: x.get("bitrate_kbps") or 0, reverse=True)
    dash_formats  = sorted([clean_fmt(f) for f in formats if f.get("is_dash")], key=lambda x: x.get("bitrate_kbps") or 0, reverse=True)
    all_by_itag   = {clean_fmt(f)["itag"]: clean_fmt(f) for f in formats if f.get("format_id")}

    raw_date = data.get("upload_date", "")
    upload_date = f"{raw_date[:4]}/{raw_date[4:6]}/{raw_date[6:]}" if raw_date and len(raw_date) == 8 else raw_date

    chapters = [{"title": c.get("title"), "start_sec": c.get("start_time"), "end_sec": c.get("end_time"),
                 "start_time": _sec_to_hms(c.get("start_time")), "end_time": _sec_to_hms(c.get("end_time"))}
                for c in (data.get("chapters") or [])]

    return JSONResponse(content={
        "video_id": video_id,
        "title": data.get("title"),
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "upload_date": upload_date,
        "duration_sec": data.get("duration"),
        "duration": data.get("duration_string"),
        "view_count": data.get("view_count"),
        "like_count": data.get("like_count"),
        "comment_count": data.get("comment_count"),
        "age_limit": data.get("age_limit"),
        "is_live": data.get("is_live", False),
        "was_live": data.get("was_live", False),
        "availability": data.get("availability"),
        "thumbnail": data.get("thumbnail"),
        "thumbnails": [{"id": t.get("id"), "url": t.get("url"), "width": t.get("width"), "height": t.get("height")} for t in (data.get("thumbnails") or [])],
        "uploader": {
            "name": data.get("uploader"),
            "id": data.get("uploader_id"),
            "channel_id": data.get("channel_id"),
            "channel_url": data.get("channel_url"),
            "subscriber_count": data.get("channel_follower_count"),
            "avatar_url": data.get("uploader_avatar_url") or _extract_avatar(data),
            "is_verified": data.get("channel_is_verified", False),
        },
        "description": data.get("description") or "",
        "tags": data.get("tags") or [],
        "categories": data.get("categories") or [],
        "chapters": chapters,
        "stream_urls": {"m3u8": data.get("m3u8_urls", []), "dash_manifest": data.get("dash_manifest_url")},
        "formats": {
            "summary": {"total": len(formats), "video": len(video_formats), "audio": len(audio_formats), "hls": len(hls_formats), "dash": len(dash_formats)},
            "video": video_formats,
            "audio": audio_formats,
            "hls": hls_formats,
            "dash": dash_formats,
            "all_by_itag": all_by_itag,
        },
        "related_videos": related,
    })


def _sec_to_hms(sec):
    if sec is None:
        return None
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _extract_avatar(data: dict):
    for t in (data.get("thumbnails") or []):
        url = t.get("url", "")
        if "ggpht" in url or ("ytimg.com/vi/" not in url and "photo" in url):
            return url
    return None


async def _fetch_related(video_id: str) -> list:
    def _get():
        opts = {"quiet": True, "extract_flat": True, "skip_download": True, "playlistend": 12}
        results = []
        try:
            with _ydlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}", download=False)
                for e in (info.get("entries") or [])[:12]:
                    eid = e.get("id") or e.get("url", "").split("v=")[-1]
                    if not eid or eid == video_id:
                        continue
                    results.append({"video_id": eid, "title": e.get("title"), "uploader": e.get("uploader") or e.get("channel"),
                                    "duration_sec": e.get("duration"), "view_count": e.get("view_count"),
                                    "thumbnail": f"https://i.ytimg.com/vi/{eid}/hqdefault.jpg",
                                    "youtube_url": f"https://www.youtube.com/watch?v={eid}"})
        except Exception:
            pass
        return results

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _get), timeout=10)
    except Exception:
        return []


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    err = classify_error(exc)
    return JSONResponse(status_code=err.get("code", 500), content=err)
