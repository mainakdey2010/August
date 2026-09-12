"""
Celery application with dual worker pools.

Two pools prevent agent reasoning workloads from starving ingestion tasks:
  - ingest queue:  I/O-bound (GEE exports, BQ loads, GCS reads)
  - agents queue:  CPU/LLM-bound (ADK sessions, risk scoring)

Launch:
  celery -A sera.tasks.celery_app worker -Q ingest --concurrency=8 --hostname=ingest@%h
  celery -A sera.tasks.celery_app worker -Q agents --concurrency=4 --hostname=agents@%h
  celery -A sera.tasks.celery_app beat   --scheduler=celery.beat:PersistentScheduler
"""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery(
    "sera",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "sera.tasks.ingest",
        "sera.tasks.agent_tasks",
        "sera.tasks.baseline_tasks",
        "sera.tasks.gee_poller",
        "sera.tasks.webhook_tasks",
    ],
)

app.conf.update(
    task_serializer            = "json",
    result_serializer          = "json",
    accept_content             = ["json"],
    timezone                   = "UTC",
    enable_utc                 = True,
    task_track_started         = True,
    task_acks_late             = True,         # only ack after task completes (safer on crash)
    worker_prefetch_multiplier = 1,            # one task at a time per worker (for long GEE tasks)
    task_routes                = {
        "sera.tasks.ingest.*":    {"queue": "ingest"},
        "sera.tasks.baseline_*":  {"queue": "ingest"},
        "sera.tasks.gee_poller.*":{"queue": "ingest"},
        "sera.tasks.agent_tasks.*":{"queue": "agents"},
    },
    beat_schedule = {
        # Poll active GEE export tasks every 60 seconds
        "poll-gee-tasks": {
            "task":     "sera.tasks.gee_poller.poll_active_tasks",
            "schedule": 60.0,
            "options":  {"queue": "ingest"},
        },
        # Trigger scheduled region scans
        "trigger-scheduled-scans": {
            "task":     "sera.tasks.ingest.trigger_due_scans",
            "schedule": crontab(minute="0", hour="*/1"),
            "options":  {"queue": "ingest"},
        },
        # Baseline refresh every Sunday at 02:00 UTC
        "refresh-baselines": {
            "task":     "sera.tasks.baseline_tasks.refresh_all_baselines",
            "schedule": crontab(minute="0", hour="2", day_of_week="sunday"),
            "options":  {"queue": "ingest"},
        },
    },
)
