# 🚀 UltraFastYTAPI

> **Blazing-fast YouTube video/metadata extractor & downloader API**
> FastAPI · yt-dlp · aria2c · Celery · Redis · Anti-ban 2026

---

## ✨ Features

| Feature | Detail |
|---|---|
| **All formats** | Every itag, quality note, codec, FPS, filesize |
| **HLS/m3u8** | Master + variant playlists for live streams |
| **DASH** | Full manifest URL extraction |
| **Speed** | aria2c with 16 connections + 1MB segments |
| **Async** | FastAPI + asyncio throughout |
| **Background jobs** | Celery + Redis — no blocking the API |
| **Caching** | Redis TTL cache (30 min) — no repeated yt-dlp calls |
| **Rate limiting** | slowapi per-IP to protect against abuse |
| **Anti-ban 2026** | android+web+ios player clients, PO token support, cookie injection, proxy rotation |
| **Error handling** | Structured JSON for 403 / 429 / geo-blocks / age-gate / private videos |

---

## 📁 Project Structure

```
UltraFastYTAPI/
├── main.py           # FastAPI app & all endpoints
├── tasks.py          # Celery download tasks
├── models.py         # Pydantic request/response models
├── utils.py          # yt-dlp helpers, cache, aria2c opts
├── celeryconfig.py   # Celery broker/backend config
├── requirements.txt  # Python dependencies
├── Dockerfile        # Container image
├── docker-compose.yml # Full stack (API + worker + Redis + Flower)
├── .env.example      # Environment variable template
└── README.md
```

---

## 🛠️ Prerequisites

### System packages

```bash
# Ubuntu / Debian
sudo apt-get update
sudo apt-get install -y aria2 ffmpeg redis-server

# macOS (Homebrew)
brew install aria2 ffmpeg redis

# Arch Linux
sudo pacman -S aria2 ffmpeg redis
```

### Node.js (recommended — yt-dlp JS interpreter)

```bash
# via nvm (recommended)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
nvm install 20
nvm use 20

# or via package manager
sudo apt-get install -y nodejs   # Ubuntu
brew install node                # macOS
```

### Deno (alternative JS runtime)

```bash
curl -fsSL https://deno.land/install.sh | sh
# Add to PATH: export PATH="$HOME/.deno/bin:$PATH"
```

---

## ⚡ Quick Start (Local)

### 1. Clone & install

```bash
git clone https://github.com/yourname/UltraFastYTAPI
cd UltraFastYTAPI

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set REDIS_URL if not using localhost
```

### 3. Create downloads folder

```bash
sudo mkdir -p /downloads && sudo chmod 777 /downloads
# Or set DOWNLOADS_DIR=./downloads in .env and create it:
mkdir -p ./downloads
```

### 4. Start Redis

```bash
redis-server &
# or: sudo systemctl start redis
```

### 5. Start Celery worker

```bash
celery -A tasks.celery_app worker \
  --loglevel=info \
  --concurrency=4 \
  -Q downloads,celery \
  --max-tasks-per-child=10
```

### 6. Start FastAPI server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --reload
```

Open docs: **http://localhost:8000/docs**

---

## 🐳 Docker Compose (Recommended)

```bash
cp .env.example .env
# Edit .env as needed

docker compose up --build -d

# View logs
docker compose logs -f api
docker compose logs -f worker

# Flower dashboard (Celery task monitor)
open http://localhost:5555
```

---

## 🔌 API Endpoints

### `POST /get_all_formats`

Extract complete metadata and every available format.

```bash
curl -X POST http://localhost:8000/get_all_formats \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

**Response:**
```json
{
  "id": "dQw4w9WgXcQ",
  "title": "Rick Astley - Never Gonna Give You Up",
  "uploader": "Rick Astley",
  "duration": 213,
  "is_live": false,
  "formats": [
    {
      "format_id": "137",
      "ext": "mp4",
      "protocol": "https",
      "quality_note": "1080p",
      "resolution": "1920x1080",
      "fps": 25.0,
      "vcodec": "avc1.640028",
      "acodec": "none",
      "filesize_approx": 185432190,
      "tbr": 4073.195,
      "url": "https://rr4---sn-....googlevideo.com/..."
    },
    {
      "format_id": "251",
      "ext": "webm",
      "protocol": "https",
      "quality_note": "medium",
      "acodec": "opus",
      "vcodec": "none",
      "abr": 128.0
    }
  ],
  "m3u8_urls": [],
  "dash_manifest_url": null
}
```

---

### `POST /extract_m3u8`

Focused HLS/m3u8 extraction — perfect for live streams.

```bash
curl -X POST http://localhost:8000/extract_m3u8 \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=LIVE_VIDEO_ID"}'
```

**Response:**
```json
{
  "id": "LIVE_VIDEO_ID",
  "title": "Live Stream Title",
  "is_live": true,
  "master_m3u8": "https://manifest.googlevideo.com/api/manifest/hls_playlist/...",
  "variant_m3u8s": [
    {"format_id": "233", "resolution": "1920x1080", "tbr": 5000, "url": "..."},
    {"format_id": "234", "resolution": "1280x720",  "tbr": 2500, "url": "..."}
  ]
}
```

---

### `POST /start_download`

Queue a download (async — returns immediately).

```bash
# Download best quality
curl -X POST http://localhost:8000/start_download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "itag": "bestvideo+bestaudio/best"}'

# Download specific itag (1080p video + 128k audio merged)
curl -X POST http://localhost:8000/start_download \
  -H "Content-Type: application/json" \
  -d '{"url": "...", "itag": "137+140", "filename": "my_video"}'

# Audio only
curl -X POST http://localhost:8000/start_download \
  -H "Content-Type: application/json" \
  -d '{"url": "...", "itag": "bestaudio/best"}'
```

**Response:**
```json
{
  "task_id": "8b3c1f42-...",
  "status": "queued",
  "message": "Download queued. Poll /task_status/8b3c1f42-... for progress."
}
```

---

### `GET /task_status/{task_id}`

Poll for progress.

```bash
curl http://localhost:8000/task_status/8b3c1f42-...
```

**While downloading:**
```json
{
  "task_id": "8b3c1f42-...",
  "status": "progress",
  "progress": 67.3,
  "progress_str": "67.3% — 45.2MiB/s — ETA 00:03"
}
```

**When done:**
```json
{
  "task_id": "8b3c1f42-...",
  "status": "success",
  "progress": 100,
  "file_path": "/downloads/Rick_Astley_-_Never_Gonna_Give_You_Up.mp4",
  "filename": "Rick_Astley_-_Never_Gonna_Give_You_Up.mp4"
}
```

---

### `GET /download/{task_id}`

Stream the file to your browser/client once complete.

```bash
curl -L -o video.mp4 http://localhost:8000/download/8b3c1f42-...
```

---

## 🔐 Anti-Ban Configuration (2026)

### 1. Player clients (built-in)

The API automatically tries `android`, `web`, and `ios` player clients in order. This covers the vast majority of cases.

### 2. Cookies (age-gated / auth videos)

```bash
# Install browser extension: "Get cookies.txt LOCALLY" (Chrome/Firefox)
# Export as Netscape format → save to /app/cookies.txt

# In .env:
YT_COOKIES_FILE=/app/cookies.txt
```

### 3. PO Token (Proof of Origin — YouTube 2025+ bot detection)

```bash
# Follow: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#po-token-guide
# In .env:
YT_PO_TOKEN=your_token_here
YT_VISITOR_DATA=your_visitor_data_here
```

### 4. Proxy rotation

```bash
# Single proxy in .env:
YT_PROXY=http://user:pass@proxy-host:8080

# For rotation: use a rotating proxy service URL
YT_PROXY=http://rotating.proxyservice.com:8080
```

### 5. Common error fixes

| Error | Cause | Fix |
|---|---|---|
| `403 Forbidden` | IP banned or missing auth | Add cookies + proxy |
| `429 Too Many Requests` | Rate limited | Wait 30s, use proxy rotation |
| `geo-blocked` | Region restriction | Use proxy in allowed region |
| `Sign in to confirm age` | Age gate | Add logged-in cookies |
| `Private video` | No access | Must be logged in with access |
| `This live event will begin` | Stream not started | Poll and retry later |
| `nsig extraction failed` | Outdated yt-dlp | `pip install -U yt-dlp` |

---

## 📊 Common itag Reference

| itag | Container | Resolution | Codec |
|------|-----------|------------|-------|
| 137 | mp4 | 1080p | avc1 |
| 248 | webm | 1080p | vp9 |
| 136 | mp4 | 720p | avc1 |
| 247 | webm | 720p | vp9 |
| 135 | mp4 | 480p | avc1 |
| 140 | m4a | audio | aac 128k |
| 251 | webm | audio | opus 160k |
| 160 | mp4 | 144p | avc1 |

---

## 🚀 Scaling Tips

### Multiple Celery workers

```bash
# Worker 1 — 4 concurrent download slots
celery -A tasks.celery_app worker -n worker1@%h --concurrency=4 -Q downloads

# Worker 2 — on another machine or process
celery -A tasks.celery_app worker -n worker2@%h --concurrency=4 -Q downloads
```

### Multiple API instances (behind nginx/load balancer)

```bash
uvicorn main:app --host 0.0.0.0 --port 8001 --workers 8
uvicorn main:app --host 0.0.0.0 --port 8002 --workers 8
```

### Proxy rotation pool

Use a service like BrightData, Oxylabs, or Smartproxy with rotating residential IPs. Set `YT_PROXY` to the gateway URL.

### Redis cluster

For high-volume production, upgrade from standalone Redis to Redis Cluster or Redis Sentinel. Update `REDIS_URL` accordingly.

### Flower monitoring

```bash
celery -A tasks.celery_app flower --port=5555
open http://localhost:5555
```

---

## 🧑‍💻 Running on Replit / Cursor / Codespaces

1. Ensure Redis is available (use a managed Redis like Upstash — set `REDIS_URL`)
2. Install system packages via shell: `apt-get install aria2 ffmpeg`
3. `pip install -r requirements.txt`
4. Start API: `uvicorn main:app --port 8000`
5. Start worker in second shell: `celery -A tasks.celery_app worker --loglevel=info`

For Replit: add `REDIS_URL` as a secret pointing to Upstash or Railway Redis.

---

## 📝 License

MIT — use freely, responsibly, and in accordance with YouTube's Terms of Service.
