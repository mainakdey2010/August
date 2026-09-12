"""
Webhook delivery tasks — fetches registered webhooks from Postgres, delivers with retry.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import redis as redis_lib
from celery import shared_task

log = logging.getLogger(__name__)


def _redis() -> redis_lib.Redis:
    return redis_lib.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


@shared_task(
    name="sera.tasks.webhook_tasks.deliver_event",
    queue="ingest",
    max_retries=0,    # tenacity handles retries inside delivery.deliver()
)
def deliver_event(region_id: str, risk_tier: str, event: dict[str, Any]) -> dict[str, Any]:
    """
    Look up all webhooks registered for this (region, risk_tier),
    deliver to each. Each delivery is independent — one failure doesn't block others.
    """
    from sera.storage.postgis import get_webhooks_for_event
    from sera.api.webhooks.delivery import deliver
    import google.cloud.secretmanager as sm

    rc       = _redis()
    webhooks = get_webhooks_for_event(region_id, risk_tier)
    results  = {}

    for wh in webhooks:
        webhook_id = wh["webhook_id"]
        url        = wh["url"]

        # Fetch raw secret from Secret Manager (not the stored hash)
        secret = _get_secret(webhook_id)
        if not secret:
            log.error("Cannot deliver webhook %s — secret not found in Secret Manager", webhook_id)
            results[webhook_id] = "secret_missing"
            continue

        ok = deliver(
            webhook_id = webhook_id,
            url        = url,
            secret     = secret,
            event_type = f"risk_event.{risk_tier}",
            event      = event,
            redis_client = rc,
        )
        results[webhook_id] = "delivered" if ok else "dlq"

    return results


def _get_secret(webhook_id: str) -> str | None:
    """
    Fetch webhook signing secret from Google Secret Manager.
    Secret name convention: projects/{project}/secrets/sera-webhook-{webhook_id}/versions/latest
    """
    import os
    from google.cloud import secretmanager

    project = os.environ.get("GCP_PROJECT")
    if not project:
        return None

    client      = secretmanager.SecretManagerServiceClient()
    secret_name = f"projects/{project}/secrets/sera-webhook-{webhook_id}/versions/latest"
    try:
        response = client.access_secret_version(name=secret_name)
        return response.payload.data.decode("utf-8")
    except Exception:
        log.warning("Secret not found for webhook %s", webhook_id)
        return None
