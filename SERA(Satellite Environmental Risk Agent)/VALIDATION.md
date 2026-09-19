# Demo branch validation — 19 September 2026

## Executed locally

- Python 3.12; actual installed Google ADK 2.9.2, Google GenAI 2.24.0, Earth Engine 1.7.43, H3 4.5.0.
- `PYTHONPATH=src python -m pytest tests -q`: 24 tests passed (plus 6 unittest subtests).
- Tests cover both replay pipelines using explicitly synthetic external-service adapters, actual ADK Agent/Runner execution with a model double, unknown citations, identity preservation, gaps and coverage, seasonal date ordering, concurrency/deduplication, model failure, checkpoint recovery and API retry/export.
- Both scenario computation graphs are constructed and serialized using the real Earth Engine SDK with Google's offline algorithm catalogue. Network responses are mocked. This catches SDK/expression construction errors, not remote imagery/permission/coverage errors.
- FastAPI root and OpenAPI return HTTP 200; scenario catalogue returns two scenarios; missing database configuration returns a visible HTTP 503.
- `bash -n deploy/demo.sh` and `node --check` for the extracted UI script pass.

- DOM smoke test passes: scenario selection, registration/submission, polling, map/table/report rendering, export links and safe text handling of untrusted asset labels. Uses jsdom and synthetic network responses; no screenshots.

## GitHub CI

[Run 35442768748](https://github.com/mainakdey2010/August/actions/runs/35442768748) passed on commit `91317bd5f0874caba12685cc478d6b46fade3b9a`: dependency installation, Python tests, shell validation, DOM smoke test and Docker image build.

## Not yet verified

- ADC lookup returns `DefaultCredentialsError`: no authenticated GCP access is available in this authoring session.
- No live Earth Engine pixels, Vertex model responses, Cloud SQL transactions, BigQuery archive writes, GCS previews or GCP deployments have been executed here.
- No Docker executable is available locally. The Docker image build passed in GitHub Actions; live container/cloud execution remains a separate gate.
- Browser rendering was not validated locally: Playwright's Chromium download failed with HTTP 502. The UI JavaScript syntax is checked; this is not a visual QA pass.
- Positive detection, scientific accuracy and model grounding remain unverified until actual output is retained and reviewed. The scenario locations are illustrative monitoring points.

## Completion gate

From the PR branch, run `bash deploy/demo.sh`, open the authenticated proxy, and run `python3 deploy/demo-verify.py`. It must pass for both real scenarios. Review the JSON, satellite comparisons, per-cell changes, gaps and each model claim. Preserve the output outside Git (the output directory is ignored). Continue any fixes on this same PR; do not declare full functionality based on the offline test suite.
