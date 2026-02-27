"""
tasks.py — Celery ダウンロードタスク
修正点:
  - _find_file の戻り値型 str | None → Optional[str]（Python 3.9 互換）
  - _get_title() を廃止 → ダウンロード時の extract_info 結果からタイトル取得
  - format_selector パラメータ名の統一（DownloadRequest.format_selector に合わせる）
  - yt-dlp の戻り値（info）からタイトルを取得するよう変更
  - FAILURE 時の state 二重更新問題を修正
"""

from __future__ import annotations

import glob
import logging
import os
import re
from typing import Optional

import yt_dlp
from celery import Celery
from celery.utils.log import get_task_logger

from utils import build_aria2c_opts, build_ydl_opts, classify_error, DOWNLOADS_DIR

logger = get_task_logger(__name__)

celery_app = Celery("ultrafastytapi")
celery_app.config_from_object("celeryconfig")


def _sanitize(name: str) -> str:
    """ファイル名に使えない文字を除去し最大180文字に切り詰める。"""
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:180]


def _find_file(base: str, exts: list) -> Optional[str]:  # 修正: str | None → Optional[str]
    """base パスに指定した拡張子のファイルが存在するか確認し、最初に見つかったものを返す。"""
    for ext in exts:
        p = f"{base}.{ext}"
        if os.path.exists(p):
            return p
    # ワイルドカードでマッチした中で最大サイズのものを返す
    matches = glob.glob(f"{base}.*")
    return max(matches, key=os.path.getsize) if matches else None


def _progress_hook(task, d: dict) -> None:
    """yt-dlp progress_hook コールバック → Celery タスク状態を更新。"""
    if d["status"] == "downloading":
        downloaded = d.get("downloaded_bytes", 0)
        total      = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        pct        = round(downloaded / total * 100, 1) if total > 0 else 0.0
        speed_str  = d.get("_speed_str", "?")
        eta_str    = d.get("_eta_str", "?")
        task.update_state(
            state="PROGRESS",
            meta={
                "progress":     pct,
                "progress_str": f"{pct}%  {speed_str}/s  ETA {eta_str}",
                "status":       "downloading",
            },
        )
    elif d["status"] == "finished":
        task.update_state(
            state="PROGRESS",
            meta={"progress": 99.0, "progress_str": "merging...", "status": "merging"},
        )


@celery_app.task(bind=True, name="tasks.download_video", max_retries=3)
def download_video(
    self,
    url: str,
    format_selector: str,
    filename_hint: Optional[str] = None,
) -> dict:
    """
    YouTube 動画をダウンロードする Celery タスク。

    修正:
      - _get_title() による事前の yt-dlp 呼び出しを廃止
        → extract_info(download=True) の戻り値からタイトルを取得
      - FAILURE 時に self.update_state を呼んだ後に raise すると
        Celery が状態を FAILURE で上書きするため update_state を削除
    """
    self.update_state(state="STARTED", meta={"progress": 0, "status": "started"})

    try:
        # ファイル名の仮決め（hint があれば使い、なければ video_id で暫定）
        from utils import extract_video_id
        vid_id       = extract_video_id(url) or "video"
        tmp_name     = _sanitize(filename_hint or vid_id)

        # ダウンロードオプション組み立て
        aria2c_extra = build_aria2c_opts(DOWNLOADS_DIR, f"{tmp_name}.%(ext)s")
        extra = {
            **aria2c_extra,
            "format":               format_selector or "bestvideo+bestaudio/best",
            "merge_output_format":  "mp4",
        }
        ydl_opts = build_ydl_opts(extra)
        ydl_opts["progress_hooks"] = [lambda d: _progress_hook(self, d)]

        self.update_state(state="PROGRESS", meta={"progress": 1, "status": "downloading"})

        # ダウンロード実行（extract_info の戻り値からタイトルを取得）
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # タイトルをダウンロード結果から取得（_get_title() 呼び出し不要）
        title    = (info or {}).get("title", vid_id)
        safe     = _sanitize(filename_hint or title)

        # outtmpl が %(ext)s なのでワイルドカードで探す
        final = _find_file(
            os.path.join(DOWNLOADS_DIR, tmp_name),
            ["mp4", "mkv", "webm", "mp3", "m4a", "opus"],
        )

        return {
            "status":    "success",
            "file_path": final,
            "filename":  os.path.basename(final) if final else None,
            "title":     title,
        }

    except yt_dlp.utils.DownloadError as exc:
        err = classify_error(exc)
        # 429 / 500 はリトライ対象
        if err.get("code") in (429, 500):
            raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
        # リトライ対象外はそのまま raise（Celery が FAILURE に設定する）
        raise

    except Exception as exc:
        # 予期しない例外もそのまま raise（Celery が FAILURE に設定）
        raise
