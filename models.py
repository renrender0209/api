"""
models.py — Pydantic リクエスト/レスポンスモデル
修正点:
  - DownloadRequest.itag → format_selector にリネーム
    （yt-dlp のフォーマットセレクター文字列であることを明示）
  - 全モデルに model_config で extra="forbid" を追加（意図しないフィールドを拒否）
  - VideoMetadata / M3U8Response を実際のレスポンス構造に合わせて拡張
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ─────────────────────────────────────────────────────
# リクエストモデル
# ─────────────────────────────────────────────────────
class FormatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str

    @field_validator("url")
    @classmethod
    def validate_youtube_url(cls, v: str) -> str:
        if not any(x in v for x in ["youtube.com", "youtu.be", "youtube-nocookie.com"]):
            raise ValueError("URL must be a valid YouTube URL")
        return v


class DownloadRequest(BaseModel):
    """
    ダウンロードリクエスト。

    format_selector は yt-dlp のフォーマットセレクター文字列。
    例: "bestvideo+bestaudio/best", "137+140", "bestaudio/best"
    ※ 旧フィールド名 `itag` から `format_selector` にリネーム
    """
    model_config = ConfigDict(extra="forbid")

    url: str
    format_selector: Optional[str] = "bestvideo+bestaudio/best"
    filename: Optional[str] = None

    @field_validator("url")
    @classmethod
    def validate_youtube_url(cls, v: str) -> str:
        if not any(x in v for x in ["youtube.com", "youtu.be", "youtube-nocookie.com"]):
            raise ValueError("URL must be a valid YouTube URL")
        return v


# ─────────────────────────────────────────────────────
# フォーマット情報
# ─────────────────────────────────────────────────────
class FormatInfo(BaseModel):
    format_id:      str
    ext:            str
    protocol:       str
    quality_note:   Optional[str]   = None
    resolution:     Optional[str]   = None
    fps:            Optional[float] = None
    vcodec:         Optional[str]   = None
    acodec:         Optional[str]   = None
    filesize_approx: Optional[int]  = None
    tbr:            Optional[float] = None
    vbr:            Optional[float] = None
    abr:            Optional[float] = None
    url:            Optional[str]   = None
    manifest_url:   Optional[str]   = None
    is_hls:         bool            = False
    is_dash:        bool            = False
    is_live:        bool            = False
    height:         Optional[int]   = None
    width:          Optional[int]   = None
    format_note:    Optional[str]   = None


# ─────────────────────────────────────────────────────
# レスポンスモデル
# ─────────────────────────────────────────────────────
class VideoMetadata(BaseModel):
    """/get_all_formats のレスポンス（簡易版）。"""
    id:               str
    title:            str
    uploader:         Optional[str]        = None
    duration:         Optional[float]      = None
    is_live:          bool                 = False
    formats:          List[FormatInfo]     = []
    m3u8_urls:        List[str]            = []
    dash_manifest_url: Optional[str]       = None


class M3U8Response(BaseModel):
    """/extract_m3u8 のレスポンス。"""
    id:             str
    title:          str
    is_live:        bool               = False
    master_m3u8:    Optional[str]      = None
    variant_m3u8s:  List[Dict]         = []
    hls_formats:    List[FormatInfo]   = []


# ─────────────────────────────────────────────────────
# タスク系モデル
# ─────────────────────────────────────────────────────
class TaskStatus(str, Enum):
    QUEUED   = "queued"
    STARTED  = "started"
    PROGRESS = "progress"
    SUCCESS  = "success"
    FAILURE  = "failure"


class TaskResponse(BaseModel):
    task_id: str
    status:  TaskStatus
    message: Optional[str] = None


class TaskStatusResponse(BaseModel):
    task_id:      str
    status:       str
    progress:     Optional[float] = None
    progress_str: Optional[str]   = None
    file_path:    Optional[str]   = None
    filename:     Optional[str]   = None
    error:        Optional[str]   = None
    result:       Optional[Any]   = None
