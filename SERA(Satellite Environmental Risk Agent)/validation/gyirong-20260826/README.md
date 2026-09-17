# Gyirong historical replay: first implementation slice

This case tests whether SERA can detect observed environmental change associated
with the 26 August 2026 event near Gyirong Port and relate it to a registered asset.
It does not force a positive output or claim advance warning.

## Source checkpoint

Reviewed develop: 9e464202e81b731420be6d940f4ebe18cc9a6bdf.
PR #7 head/base for this work: 62f8eea51d1b71db533db298576793c95e9eb864.
PR #6 is merged; PR #7 was open when this work started.
The two supplied reviews disagree about deployment status. Live worker, scheduler,
database contents and deployed image have not been checked in this session.

## Implemented here

- Encode BigQuery DATE/TIMESTAMP values in anomaly checkpoints explicitly.
- Recursively convert scored asset dataclasses to dictionaries, so fresh and
  restored results both support checkpoint writes and report generation.
- Resolve completed checkpoint stages from their tables instead of looking for
  a stage field inside payload JSON.
- Reject unsupported objects and non-finite JSON numbers instead of silently
  converting them to strings.
- Add credential-free regression tests for nonempty output and checkpoint recovery.

These changes do not fix event-write idempotency, scientific baselines, workers,
polling, historical ingestion, or scoring aggregation. Keep this change in draft
until tests execute successfully.

## Event evidence and geography

The research preprint https://arxiv.org/abs/2609.04563v2 describes the event and
a representative source-to-port route of about 21.8 km. That route length is
different from a total downstream impact extent. It does not independently
validate the user's stated 100 km extent or the proposed barrier-lake coordinate.

The asset reference converts to latitude 28.27972222, longitude 85.37777778.
GeoJSON uses longitude first. All three user-supplied locations and their
verification status are preserved in case.json.

A point is not a surveyed customs-facility footprint. Obtain an independently
verified facility polygon and its valid-from date before registering it for a
historical asset-intersection test. A provisional buffer must be labelled as a
proxy, with a stated radius; it must not be described as the facility boundary.

Start with separate bounded areas around the source, reported lake and port.
Connect them only using a checked drainage route. Do not turn the 100 km claim
into a circular AOI or treat straight lines between points as a flood path.
case.json is an evaluation specification, not a POST /v1/regions request.

## Run the regression tests

From the SERA project directory, in an environment with its requirements installed:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_agent_checkpoints.py' -v
```

Tests use mocked storage and synthetic observations. They do not prove the event
was detected. No Python runner or authenticated GCP runtime was available in the
authoring session, so these tests have not yet been executed.

## Remaining gates before the real replay

1. Execute the tests, then verify agents queue consumption, worker IAM for BQ
   stage/event writes and PostGIS reads, and reliable export polling.
2. Fix durable event/load deduplication and recovery. Reporting currently creates
   random event IDs and appends rows, so retries can duplicate logical events.
3. Correct and validate baseline export/load order, readiness, duplicate rows,
   seasonal bins and matching observation aggregation. Freeze baseline evidence
   before the event and retain its period, version and statistical support.
4. Fix historical ingestion explicitly: current incremental checkpoints and
   scan windows must not silently skip old observations or mix pre/post-event
   scenes. Store requested dates separately from actual acquisitions.
5. Confirm region cache synchronization, H3 readiness and asset as-of validity.
6. Discover usable pre/post scenes in the proposed windows. Start with supported
   optical evidence where coverage permits. Treat cloud gaps as insufficient
   evidence. SAR requires a validated computation path and compatible orbit/
   terrain handling before it can be advertised as a fallback.
7. Check scoring invariance to duplicate rows and cell count, and retain all
   contributions needed to reconstruct the score.
8. Run pre-event controls, post-event affected locations and comparable unaffected
   controls. Compare against independently mapped impacts (seek original rapid
   mapping products referenced by the preprint). Do not tune thresholds solely
   until this one case passes.

## Completion evidence

Record source commit, deployed image digest, region/config version, asset geometry
validity, scan IDs, actual scene IDs/dates, coverage, baseline provenance,
per-cell observations, score contributions, stage statuses and event counts.
Report detection, adequate-evidence non-detection, insufficient evidence and
processing failure separately.

The first successful result is a traceable historical detection with controls.
A claim of advance warning requires a separate replay using only observations
and products available before the event.
