"""
celeryconfig.py — Celery ブローカー/バックエンド設定
修正点:
  - task_max_retries を削除（@task デコレータ側の max_retries=3 で統一）
  - broker_connection_retry_on_startup = True を追加（Redis 起動待ち対策）
  - worker_cancel_long_running_tasks_on_connection_loss を追加
"""

import os

# ── ブローカー / バックエンド ─────────────────────────
broker_url              = os.getenv("REDIS_URL", "redis://localhost:6379/0")
result_backend          = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Redis 起動前に Celery が起動しても再試行する
broker_connection_retry_on_startup = True

# ── シリアライザー ────────────────────────────────────
task_serializer         = "json"
result_serializer       = "json"
accept_content          = ["json"]

# ── タイムゾーン ──────────────────────────────────────
timezone                = "UTC"
enable_utc              = True

# ── タスク挙動 ────────────────────────────────────────
task_track_started      = True
task_acks_late          = True          # タスク完了まで ACK を送らない（確実性向上）
worker_prefetch_multiplier = 1          # ワーカーが一度に取る最大タスク数
task_soft_time_limit    = 3600          # ソフトタイムリミット（1h）
task_time_limit         = 3900          # ハードタイムリミット（65min）
result_expires          = 86400         # 結果の保持期間（24h）

# ── ワーカー ──────────────────────────────────────────
worker_max_tasks_per_child = 10         # メモリリーク対策でワーカーを定期再起動
worker_cancel_long_running_tasks_on_connection_loss = True  # 接続断時のタスクキャンセル

# ── キュー定義 ────────────────────────────────────────
task_routes = {
    "tasks.download_video": {"queue": "downloads"},
}
