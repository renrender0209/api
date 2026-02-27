import os

broker_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
result_backend = os.getenv("REDIS_URL", "redis://localhost:6379/0")
task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
timezone = "UTC"
enable_utc = True
task_track_started = True
task_acks_late = True
worker_prefetch_multiplier = 1
task_soft_time_limit = 3600
task_time_limit = 3900
result_expires = 86400
task_max_retries = 3
worker_max_tasks_per_child = 10
