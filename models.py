from pydantic import BaseModel, field_validator
from typing import Optional, List, Any
from enum import Enum


class FormatRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_youtube_url(cls, v):
        if not any(x in v for x in ["youtube.com", "youtu.be", "youtube-nocookie.com"]):
            raise ValueError("URL must be a valid YouTube URL")
        return v


class DownloadRequest(BaseModel):
    url: str
    itag: Optional[str] = "bestvideo+bestaudio/best"
    filename: Optional[str] = None

    @field_validator("url")
    @classmethod
    def validate_youtube_url(cls, v):
        if not any(x in v for x in ["youtube.com", "youtu.be", "youtube-nocookie.com"]):
            raise ValueError("URL must be a valid YouTube URL")
        return v


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    protocol: str
    quality_note: Optional[str] = None
    resolution: Optional[str] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    filesize_approx: Optional[int] = None
    tbr: Optional[float] = None
    vbr: Optional[float] = None
    abr: Optional[float] = None
    url: Optional[str] = None
    manifest_url: Optional[str] = None
    is_hls: bool = False
    is_dash: bool = False
    is_live: bool = False
    height: Optional[int] = None
    width: Optional[int] = None
    format_note: Optional[str] = None


class VideoMetadata(BaseModel):
    id: str
    title: str
    uploader: Optional[str] = None
    duration: Optional[float] = None
    is_live: bool = False
    formats: List[FormatInfo] = []
    m3u8_urls: List[str] = []
    dash_manifest_url: Optional[str] = None


class M3U8Response(BaseModel):
    id: str
    title: str
    is_live: bool = False
    master_m3u8: Optional[str] = None
    variant_m3u8s: List[dict] = []
    hls_formats: List[FormatInfo] = []


class TaskStatus(str, Enum):
    QUEUED = "queued"
    STARTED = "started"
    PROGRESS = "progress"
    SUCCESS = "success"
    FAILURE = "failure"


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    message: Optional[str] = None


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: Optional[float] = None
    progress_str: Optional[str] = None
    file_path: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Any] = None
