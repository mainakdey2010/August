"""
PostGIS client — spatial queries and webhook persistence.

Webhooks moved from Redis (ephemeral) to Postgres (durable).
Asset boundary queries use asset_boundaries_as_of(date) to handle versioned geometries.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import date
from typing import Any

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://sera:sera@localhost:5432/sera")


def _conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Spatial: H3 → Asset resolution ───────────────────────────────────────────

def resolve_h3_to_assets(
    region_id: str,
    h3_cells: list[str],
    as_of_date: date,
) -> dict[str, dict[str, Any]]:
    """
    Find all assets whose boundaries intersect any of the given H3 cells,
    using geometry valid as-of as_of_date.

    Returns: {asset_id: {"tier": str, "criticality": float, "cells": [h3_cell, ...]}}
    """
    if not h3_cells:
        return {}

    # Convert H3 cells to WKT polygons
    import h3
    cell_wkt = [
        (cell, _h3_to_wkt(cell))
        for cell in h3_cells
    ]

    result: dict[str, dict[str, Any]] = {}

    with _conn() as conn:
        with conn.cursor() as cur:
            for cell, wkt in cell_wkt:
                cur.execute(
                    """
                    SELECT asset_id, asset_tier, criticality_score
                    FROM asset_boundaries_as_of(%s)
                    WHERE region_id = %s
                      AND ST_Intersects(geom, ST_GeomFromText(%s, 4326))
                    """,
                    (str(as_of_date), region_id, wkt),
                )
                for row in cur.fetchall():
                    asset_id = row["asset_id"]
                    if asset_id not in result:
                        result[asset_id] = {
                            "tier":        row["asset_tier"],
                            "criticality": float(row["criticality_score"]),
                            "cells":       [],
                        }
                    result[asset_id]["cells"].append(cell)

    return result


def _h3_to_wkt(cell: str) -> str:
    import h3
    boundary = h3.cell_to_boundary(cell)   # [(lat, lon), ...]
    coords = ", ".join(f"{lon} {lat}" for lat, lon in boundary)
    first_lon, first_lat = boundary[0][1], boundary[0][0]
    return f"POLYGON(({coords}, {first_lon} {first_lat}))"


# ── Region management ─────────────────────────────────────────────────────────

def register_region(region_id: str, config: dict, geom_wkt: str, buffer_wkt: str | None) -> None:
    import yaml
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO monitoring_regions
                  (region_id, display_name, asset_tier, interval_days,
                   geom, buffer_geom, config_yaml, active)
                VALUES (%s, %s, %s, %s,
                  ST_GeomFromText(%s, 4326),
                  ST_GeomFromText(%s, 4326),
                  %s, TRUE)
                ON CONFLICT (region_id) DO UPDATE SET
                  display_name = EXCLUDED.display_name,
                  asset_tier   = EXCLUDED.asset_tier,
                  interval_days= EXCLUDED.interval_days,
                  config_yaml  = EXCLUDED.config_yaml,
                  updated_at   = NOW()
                """,
                (
                    region_id,
                    config.get("display_name", region_id),
                    config.get("asset_tier", "medium"),
                    config.get("monitoring", {}).get("interval_days", 7),
                    geom_wkt,
                    buffer_wkt or geom_wkt,
                    yaml.dump(config),
                ),
            )
        conn.commit()


# ── Webhook persistence (Postgres, not Redis) ─────────────────────────────────

def create_webhook(
    url: str,
    secret: str,
    events: list[str],
    region_ids: list[str],
    active: bool = True,
) -> str:
    webhook_id = "wh_" + uuid.uuid4().hex[:8]

    # Store raw secret in Secret Manager BEFORE writing to Postgres.
    # If Secret Manager fails, we don't create a webhook with an undeliverable secret.
    _store_secret_in_sm(webhook_id, secret)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhooks (webhook_id, url, secret_hash, events, region_ids, active)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    webhook_id,
                    url,
                    _hash_secret(secret),
                    events,
                    region_ids,
                    active,
                ),
            )
        conn.commit()
    return webhook_id


def _store_secret_in_sm(webhook_id: str, secret: str) -> None:
    """
    Create (or add a version to) a Secret Manager secret for this webhook's signing key.
    Secret name: sera-webhook-{webhook_id}
    Delivery code reads this secret at send time — raw secret never stored in Postgres.
    """
    import os
    from google.cloud import secretmanager

    project = os.environ.get("GCP_PROJECT")
    if not project:
        log.warning("GCP_PROJECT not set — skipping Secret Manager for webhook %s", webhook_id)
        return

    client      = secretmanager.SecretManagerServiceClient()
    secret_name = f"projects/{project}/secrets/sera-webhook-{webhook_id}"
    parent      = f"projects/{project}"

    # Create the secret resource (idempotent)
    try:
        client.create_secret(
            request={
                "parent":    parent,
                "secret_id": f"sera-webhook-{webhook_id}",
                "secret":    {"replication": {"automatic": {}}},
            }
        )
    except Exception:
        pass   # Already exists — fine, just add a new version

    # Add the secret value as a new version
    client.add_secret_version(
        request={
            "parent":  secret_name,
            "payload": {"data": secret.encode("utf-8")},
        }
    )
    log.info("Secret stored in Secret Manager for webhook %s", webhook_id)


def get_webhooks_for_event(region_id: str, risk_tier: str) -> list[dict[str, Any]]:
    """Return all active webhooks that should receive this event."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT webhook_id, url, secret_hash, events
                FROM webhooks
                WHERE active = TRUE
                  AND %s = ANY(events)
                  AND (
                    array_length(region_ids, 1) IS NULL     -- empty = all regions
                    OR %s = ANY(region_ids)
                  )
                """,
                (risk_tier, region_id),
            )
            return [dict(r) for r in cur.fetchall()]


def delete_webhook(webhook_id: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM webhooks WHERE webhook_id = %s", (webhook_id,))
        conn.commit()


def _hash_secret(secret: str) -> str:
    import hashlib
    return hashlib.sha256(secret.encode()).hexdigest()

