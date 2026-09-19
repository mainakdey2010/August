# Demo branch validation — 19 September 2026

## Executed locally

- Python 3.12; actual installed Google ADK 2.9.2, Google GenAI 2.24.0, Earth Engine 1.7.43, H3 4.5.0.
- `PYTHONPATH=src python -m pytest tests -q`: 29 tests passed, one PostgreSQL integration test skipped locally (plus 6 unittest subtests).
- Tests cover all four replay pipelines using explicitly synthetic external-service adapters, actual ADK Agent/Runner execution with a model double, unknown citations, identity preservation, gaps and coverage, seasonal date ordering, concurrency/deduplication, model failure, checkpoint recovery and API retry/export.
- All four scenario computation graphs are constructed and serialized using the real Earth Engine SDK with Google's offline algorithm catalogue. Network responses are mocked. This catches SDK/expression construction errors, not remote imagery/permission/coverage errors.
- FastAPI root and OpenAPI return HTTP 200; scenario catalogue returns four scenarios; missing database configuration returns a visible HTTP 503.
- `bash -n deploy/demo.sh` and `node --check` for the extracted UI script pass.

- DOM smoke test passes: scenario selection, registration/submission, polling, map/table/report rendering, export links and safe text handling of untrusted asset labels. Uses jsdom and synthetic network responses; no screenshots.

## GitHub CI

[Previous run 35448984721](https://github.com/mainakdey2010/August/actions/runs/35448984721) passed on commit `3458b37023252fb983a7300b5e06a4ba0abbe409`, including real PostgreSQL tests, Docker build and serving-container HTTP smoke. The four-scenario update reruns the same gates; see the PR checks for its exact commit result.

## Not yet verified

- ADC lookup returns `DefaultCredentialsError`: no authenticated GCP access is available in this authoring session.
- No live Earth Engine pixels, Vertex model responses, Cloud SQL transactions, BigQuery archive writes, GCS previews or GCP deployments have been executed here.
- No Docker executable is available locally. The Docker image build passed in GitHub Actions; local container HTTP smoke passed in CI; authenticated cloud execution remains a separate gate.
- Browser rendering was not validated locally: Playwright's Chromium download failed with HTTP 502. The UI JavaScript syntax is checked; this is not a visual QA pass.
- Positive detection, scientific accuracy and model grounding remain unverified until actual output is retained and reviewed. The scenario locations are illustrative monitoring points.

## Completion gate

From the PR branch, run `bash deploy/demo.sh`, open the authenticated proxy, and run `python3 deploy/demo-verify.py`. It must pass for all four real scenarios. Review the JSON, satellite comparisons, per-cell changes, gaps and each model claim. Preserve the output outside Git (the output directory is ignored). Continue any fixes on this same PR; do not declare full functionality based on the offline test suite.
