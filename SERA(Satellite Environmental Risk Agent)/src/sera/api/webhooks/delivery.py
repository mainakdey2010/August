"""
Webhook delivery with exponential backoff and dead-letter queue.

Delivers signed POST requests to registered webhook URLs.
Retries up to 5 times with exponential backoff.
After all retries exhausted, writes to dead-letter queue in Redis for ops inspection.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import redis
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    RetryError,
)

log = logging.getLogger(__name__)

MAX_RETRIES    = 5
DLQ_KEY        = "sera:webhook:dlq"
DLQ_TTL        = 7 * 24 * 3600   # 7 days


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()


def _build_delivery(
    webhook_id: str,
    event_type: str,
    event: dict[str, Any],
) -> tuple[bytes, str]:
    body = {
        "webhook_id":  webhook_id,
        "event_type":  event_type,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "event":       event,
    }
    payload_bytes = json.dumps(body, default=str).encode()
    return payload_bytes, body["timestamp"]


class WebhookDeliveryError(Exception):
    pass


def deliver(
    webhook_id: str,
    url: str,
    secret: str,
    event_type: str,
    event: dict[str, Any],
    redis_client: redis.Redis,
) -> bool:
    """
    Deliver event to webhook URL. Returns True on success, False if DLQ'd.
    Retries with exponential backoff: 1s, 2s, 4s, 8s, 16s.
    """
    payload_bytes, timestamp = _build_delivery(webhook_id, event_type, event)
    signature = _sign_payload(payload_bytes, secret)

    @retry(
        retry=retry_if_exception_type((httpx.RequestError, WebhookDeliveryError)),
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=False,
    )
    def _attempt() -> None:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url,
                content=payload_bytes,
                headers={
                    "Content-Type":          "application/json",
                    "X-SERA-Signature-256":  signature,
                    "X-SERA-Webhook-ID":     webhook_id,
                    "X-SERA-Delivery":       timestamp,
                },
            )
        if resp.status_code >= 500:
            raise WebhookDeliveryError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            # 4xx = client error, do not retry
            log.error(
                "Webhook %s returned %s — not retrying (client error)",
                webhook_id, resp.status_code,
            )
            return

    try:
        _attempt()
        log.info("Webhook %s delivered event_type=%s", webhook_id, event_type)
        return True
    except RetryError:
        _send_to_dlq(redis_client, webhook_id, url, event_type, event, payload_bytes)
        return False


def _send_to_dlq(
    redis_client: redis.Redis,
    webhook_id: str,
    url: str,
    event_type: str,
    event: dict[str, Any],
    payload_bytes: bytes,
) -> None:
    dlq_entry = json.dumps({
        "webhook_id":    webhook_id,
        "url":           url,
        "event_type":    event_type,
        "event_id":      event.get("event_id"),
        "failed_at":     datetime.now(timezone.utc).isoformat(),
        "payload_bytes": payload_bytes.decode(),
    }, default=str)

    redis_client.lpush(DLQ_KEY, dlq_entry)
    redis_client.expire(DLQ_KEY, DLQ_TTL)
    log.error(
        "Webhook %s permanently failed after %d retries — written to DLQ",
        webhook_id, MAX_RETRIES,
    )


def drain_dlq(redis_client: redis.Redis) -> list[dict[str, Any]]:
    """Read all entries from the DLQ without removing them. For ops inspection."""
    entries = redis_client.lrange(DLQ_KEY, 0, -1)
    return [json.loads(e) for e in entries]


def retry_dlq_entry(redis_client: redis.Redis, webhook_id: str, url: str, secret: str) -> bool:
    """
    Manual retry of a specific DLQ entry (ops tooling).
    Removes entry from DLQ on success.
    """
    entries = redis_client.lrange(DLQ_KEY, 0, -1)
    for i, raw in enumerate(entries):
        entry = json.loads(raw)
        if entry["webhook_id"] == webhook_id:
            ok = deliver(
                webhook_id=webhook_id,
                url=url,
                secret=secret,
                event_type=entry["event_type"],
                event=json.loads(entry["payload_bytes"]).get("event", {}),
                redis_client=redis_client,
            )
            if ok:
                redis_client.lrem(DLQ_KEY, 1, raw)
            return ok
    return False
