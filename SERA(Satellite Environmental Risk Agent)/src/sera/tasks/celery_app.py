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
import re
import ssl
from urllib.parse import quote

from celery import Celery
from celery.schedules import crontab


def _safe_broker_url(url: str) -> str:
    """
    URL-encode the password in a redis(s):// URL.
    Upstash tokens are base64 and contain +/= which confuse Kombu's URL parser,
    causing it to fall back to the AMQP default transport.
    """
    url = url.strip()
    m = re.match(r'^(rediss?://)([^:@]+):(.+?)@(.+)$', url)
    if m:
        scheme_slash, user, password, host = m.groups()
        return f"{scheme_slash}{user}:{quote(password, safe='')}@{host}"
    return url


REDIS_URL = _safe_broker_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

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

# Upstash uses TLS (rediss://). Kombu needs explicit SSL config — unlike redis-py,
# it doesn't infer SSL from the scheme alone on all versions.
if REDIS_URL.startswith("rediss://"):
    _ssl = {"ssl_cert_reqs": ssl.CERT_NONE}
    app.conf.broker_use_ssl        = _ssl
    app.conf.redis_backend_use_ssl = _ssl

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

