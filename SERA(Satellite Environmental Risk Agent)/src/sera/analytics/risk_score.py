"""
Composite risk scoring.

Critical fix: original spec used an additive data-gap penalty:
  final = min(raw_score + gap_count * 0.05, 1.0)

Problem: 2 missing indices with zero anomalies → score = 0.10 → LOW risk (false positive).
Gaps represent data uncertainty, not risk signal.

Fix: separate Risk and Data Quality as independent dimensions.
  risk_score   — computed from available indices only (no gap inflation)
  quality_score — fraction of requested indices that were computable

Consumers decide how to act on (HIGH risk, DEGRADED quality) vs. (HIGH risk, GOOD quality).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskTier(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"
    NONE     = "NONE"


class QualityTier(str, Enum):
    GOOD     = "GOOD"       # >= 80% indices computed
    DEGRADED = "DEGRADED"   # 50-79% computed
    POOR     = "POOR"       # < 50% computed


RISK_THRESHOLDS: dict[RiskTier, float] = {
    RiskTier.CRITICAL: 0.75,
    RiskTier.HIGH:     0.50,
    RiskTier.MEDIUM:   0.25,
    RiskTier.LOW:      0.01,
}


@dataclass
class IndexScore:
    index_id:       str
    z_score:        float
    current_value:  float
    baseline_p50:   float
    anomaly:        bool
    direction:      str          # "decline" | "spike" | "neutral"
    weight:         float
    signal:         float        # contribution to raw_score (0.0–weight)
    confidence:     str          # "high" | "low" (based on pixel coverage)


@dataclass
class DataQuality:
    computed_count:  int
    total_count:     int
    gap_indices:     list[str]
    quality_score:   float       # computed_count / total_count
    quality_tier:    QualityTier


@dataclass
class AssetRiskResult:
    asset_id:            str
    asset_tier:          str
    criticality_score:   float
    risk_score:          float          # 0.0-1.0, from available indices only
    risk_tier:           RiskTier
    index_scores:        list[IndexScore]
    data_quality:        DataQuality
    affected_h3_cells:   list[str]
    reasoning:           str


def _score_to_tier(score: float) -> RiskTier:
    for tier, threshold in RISK_THRESHOLDS.items():
        if score >= threshold:
            return tier
    return RiskTier.NONE


def _quality_tier(quality_score: float) -> QualityTier:
    if quality_score >= 0.80:
        return QualityTier.GOOD
    if quality_score >= 0.50:
        return QualityTier.DEGRADED
    return QualityTier.POOR


def compute_index_signal(
    z_score: float,
    threshold_stddev: float,
    weight: float,
) -> float:
    """
    Map z-score magnitude to a 0–weight signal contribution.
    Clamps at 3× threshold (full weight) to prevent extreme outliers from dominating.
    """
    if abs(z_score) < threshold_stddev:
        return 0.0
    excess = (abs(z_score) - threshold_stddev) / threshold_stddev
    return weight * min(excess / 2.0, 1.0)   # saturates at 3× threshold


def score_asset(
    asset_id: str,
    asset_tier: str,
    criticality_score: float,
    anomaly_rows: list[dict[str, Any]],        # from BQ anomaly detection query
    index_configs: list[dict[str, Any]],       # from region config
    gap_indices: list[str],
    affected_h3_cells: list[str],
) -> AssetRiskResult:
    """
    Compute composite risk for one asset.

    anomaly_rows: [
      {"index_id": "NDVI", "z_score": -2.3, "index_value": 0.21,
       "baseline_p50": 0.61, "anomaly": True, "direction": "decline",
       "coverage_pct": 0.94, "threshold_stddev": 2.0, "weight": 0.35},
      ...
    ]
    """
    # Index configs keyed for quick lookup
    idx_cfg_map = {c["id"]: c for c in index_configs}

    computed_ids = {r["index_id"] for r in anomaly_rows}
    total_requested = len([c for c in index_configs if c.get("enabled", True)])

    index_scores: list[IndexScore] = []
    raw_score = 0.0

    for row in anomaly_rows:
        idx_id    = row["index_id"]
        cfg       = idx_cfg_map.get(idx_id, {})
        weight    = cfg.get("weight", 0.0)
        threshold = cfg.get("threshold_stddev", 2.0)
        z_score   = row.get("z_score") or 0.0
        signal    = compute_index_signal(z_score, threshold, weight) if row.get("anomaly") else 0.0
        raw_score += signal

        index_scores.append(IndexScore(
            index_id      = idx_id,
            z_score       = z_score,
            current_value = row.get("index_value", 0.0),
            baseline_p50  = row.get("baseline_p50", 0.0),
            anomaly       = bool(row.get("anomaly")),
            direction     = row.get("direction", "neutral"),
            weight        = weight,
            signal        = signal,
            confidence    = "high" if row.get("coverage_pct", 1.0) >= 0.5 else "low",
        ))

    # Apply criticality multiplier — higher criticality assets score higher
    # Formula: score × (0.5 + 0.5 × criticality), so range is [0.5×, 1.0×]
    final_score = min(raw_score * (0.5 + 0.5 * criticality_score), 1.0)

    quality_score = len(computed_ids) / total_requested if total_requested > 0 else 0.0
    data_quality  = DataQuality(
        computed_count = len(computed_ids),
        total_count    = total_requested,
        gap_indices    = gap_indices,
        quality_score  = quality_score,
        quality_tier   = _quality_tier(quality_score),
    )

    # Build human-readable reasoning string for the Reporting Agent
    anomalies = [s for s in index_scores if s.anomaly]
    reasoning_parts = []
    for s in sorted(anomalies, key=lambda x: abs(x.z_score), reverse=True):
        reasoning_parts.append(
            f"{s.index_id} z={s.z_score:+.1f} ({s.direction}, value={s.current_value:.3f} "
            f"vs baseline={s.baseline_p50:.3f})"
        )
    if gap_indices:
        reasoning_parts.append(f"Gaps (not scored): {', '.join(gap_indices)}")

    return AssetRiskResult(
        asset_id          = asset_id,
        asset_tier        = asset_tier,
        criticality_score = criticality_score,
        risk_score        = final_score,
        risk_tier         = _score_to_tier(final_score),
        index_scores      = index_scores,
        data_quality      = data_quality,
        affected_h3_cells = affected_h3_cells,
        reasoning         = "; ".join(reasoning_parts) if reasoning_parts else "No anomalies detected.",
    )


def to_bq_row(result: AssetRiskResult, scan_id: str, region_id: str, event_date: str) -> dict[str, Any]:
    """Serialize AssetRiskResult to a risk_events BQ row."""
    import json, uuid
    return {
        "event_id":        f"evt_{event_date.replace('-', '')}_{region_id}_{result.asset_id}_{uuid.uuid4().hex[:6]}",
        "scan_id":         scan_id,
        "region_id":       region_id,
        "asset_id":        result.asset_id,
        "event_date":      event_date,
        "risk_tier":       result.risk_tier.value,
        "composite_score": result.risk_score,
        "index_scores":    json.dumps({
            s.index_id: {
                "z_score":       s.z_score,
                "anomaly":       s.anomaly,
                "value":         s.current_value,
                "baseline_p50":  s.baseline_p50,
                "direction":     s.direction,
                "weight":        s.weight,
                "signal":        s.signal,
                "confidence":    s.confidence,
            }
            for s in result.index_scores
        }),
        "data_gap_indices": result.data_quality.gap_indices,
        "data_gap_penalty":  0.0,   # no longer used; kept for schema compatibility
        "quality_score":     result.data_quality.quality_score,
        "quality_tier":      result.data_quality.quality_tier.value,
        "affected_h3_cells": result.affected_h3_cells,
        "agent_reasoning":   result.reasoning,
    }
