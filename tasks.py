import os
import re
import glob
import logging
import yt_dlp
from celery import Celery
from celery.utils.log import get_task_logger
from utils import build_ydl_opts, build_aria2c_opts, classify_error, DOWNLOADS_DIR

logger = get_task_logger(__name__)

celery_app = Celery("ultrafastytapi")
celery_app.config_from_object("celeryconfig")


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:180]


def _find_file(base: str, exts: list) -> str | None:
    for ext in exts:
        p = f"{base}.{ext}"
        if os.path.exists(p):
            return p
    matches = glob.glob(f"{base}.*")
    return max(matches, key=os.path.getsize) if matches else None


def _progress_hook(task, d: dict):
    if d["status"] == "downloading":
        downloaded = d.get("downloaded_bytes", 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        pct = round(downloaded / total * 100, 1) if total > 0 else 0.0
        task.update_state(state="PROGRESS", meta={"progress": pct,
            "progress_str": f"{pct}% — {d.get('_speed_str','?')}/s — ETA {d.get('_eta_str','?')}", "status": "downloading"})
    elif d["status"] == "finished":
        task.update_state(state="PROGRESS", meta={"progress": 99.0, "progress_str": "merging...", "status": "merging"})


def _get_title(url: str) -> str:
    opts = build_ydl_opts({"skip_download": True, "quiet": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info.get("title", "video")


@celery_app.task(bind=True, name="tasks.download_video", max_retries=3)
def download_video(self, url: str, format_selector: str, filename_hint: str = None):
    self.update_state(state="STARTED", meta={"progress": 0, "status": "started"})
    try:
        title = _get_title(url)
        safe = _sanitize(filename_hint or title)
        extra = {**build_aria2c_opts(DOWNLOADS_DIR, f"{safe}.%(ext)s"), "format": format_selector or "bestvideo+bestaudio/best", "merge_output_format": "mp4"}
        ydl_opts = build_ydl_opts(extra)
        ydl_opts["progress_hooks"] = [lambda d: _progress_hook(self, d)]
        self.update_state(state="PROGRESS", meta={"progress": 1, "status": "downloading"})
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        final = _find_file(os.path.join(DOWNLOADS_DIR, safe), ["mp4", "mkv", "webm", "mp3", "m4a", "opus"])
        return {"status": "success", "file_path": final, "filename": os.path.basename(final) if final else None, "title": title}
    except yt_dlp.utils.DownloadError as exc:
        err = classify_error(exc)
        if err.get("code") in (429, 500):
            raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
        self.update_state(state="FAILURE", meta=err)
        raise
    except Exception as exc:
        self.update_state(state="FAILURE", meta=classify_error(exc))
        raise
