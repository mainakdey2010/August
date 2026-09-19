from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sera.demo.models import Region, ScanRequest
from sera.demo.scenarios import SCENARIOS
from sera.demo.store import Store
from sera.demo.dispatch import dispatch

router=APIRouter(prefix='/v1/demo',tags=['Live demo'])
log=logging.getLogger(__name__)


def get_store():
    try:
        store=Store()
        store.migrate()
        return store
    except Exception:
        log.exception('Demo database unavailable')
        raise HTTPException(503,'Demo database unavailable; check DATABASE_URL and Cloud SQL connectivity')


@router.get('/health')
def health(store=Depends(get_store)):
    return {'database':'reachable','model_configured':bool(os.environ.get('SERA_GEMINI_MODEL')),
            'worker_configured':bool(os.environ.get('SERA_DEMO_JOB') or os.environ.get('SERA_DEMO_LOCAL_WORKER')=='true'),
            'live_model_verified':False,'note':'Configuration check only; a completed scan proves model execution.'}


@router.get('/scenarios')
def scenarios():
    return [s.model_dump(mode='json') for s in SCENARIOS]


@router.post('/regions',status_code=201)
def register(region: Region, store=Depends(get_store)):
    from sera.demo.imagery import bounds
    try:
        bounds(region)
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    store.region(region)
    return region.model_dump(mode='json')


@router.get('/regions')
def regions(store=Depends(get_store)):
    return store.regions()


@router.post('/scenarios/{scenario_id}/register',status_code=201)
def register_scenario(scenario_id: str,store=Depends(get_store)):
    scenario=next((s for s in SCENARIOS if s.region_id==scenario_id),None)
    if scenario is None:
        raise HTTPException(404,'Unknown scenario')
    store.region(scenario)
    return scenario.model_dump(mode='json')


@router.post('/scans',status_code=202)
def submit(request: ScanRequest,store=Depends(get_store)):
    config=next((r for r in store.regions() if r['region_id']==request.region_id),None)
    if config is None:
        raise HTTPException(404,'Register the region first')
    Region.model_validate(config)
    scan_id='demo_'+uuid.uuid4().hex
    fingerprint=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    if request.force_recompute:
        fingerprint+=':'+scan_id
    try:
        scan_id,created=store.create(scan_id,fingerprint,config)
    except RuntimeError as exc:
        if str(exc).startswith('DEMO_CAPACITY'):
            raise HTTPException(429,str(exc))
        raise
    if created:
        try:
            dispatch(scan_id)
        except Exception:
            log.exception('Dispatch failed for %s',scan_id)
            store.dispatch_failed(scan_id)
    return {'scan_id':scan_id,'created':created,'poll_url':f'/v1/demo/scans/{scan_id}',
            'status':store.get(scan_id)['status']}


def require_scan(scan_id,store):
    if not re.fullmatch(r'demo_[a-f0-9]{32}',scan_id):
        raise HTTPException(404,'Unknown scan')
    row=store.get(scan_id)
    if row is None:
        raise HTTPException(404,'Unknown scan')
    return row


@router.get('/scans/{scan_id}')
def scan(scan_id: str,store=Depends(get_store)):
    return require_scan(scan_id,store)


@router.post('/scans/{scan_id}/retry',status_code=202)
def retry(scan_id: str,store=Depends(get_store)):
    row=require_scan(scan_id,store)
    if not row['retryable'] or not store.prepare_retry(scan_id):
        raise HTTPException(409,'Scan is completed or still running; stale worker lease expires after 40 minutes')
    try:
        dispatch(scan_id)
    except Exception:
        log.exception('Retry dispatch failed for %s',scan_id)
        store.dispatch_failed(scan_id)
        raise HTTPException(503,'Could not dispatch worker; check API service logs')
    return {'scan_id':scan_id,'status':'retry_dispatched'}


@router.get('/scans/{scan_id}/previews/{label}')
def preview(scan_id: str,label: str,store=Depends(get_store)):
    row=require_scan(scan_id,store)
    if label not in ('before','after') or label not in (row['result'] or {}).get('previews',[]):
        raise HTTPException(404,'Preview unavailable')
    from sera.demo.artifacts import read
    return Response(read(scan_id,label+'.png'),media_type='image/png',headers={'Cache-Control':'private, max-age=3600'})


@router.get('/scans/{scan_id}/export')
def export(scan_id: str,store=Depends(get_store)):
    row=require_scan(scan_id,store)
    return Response(json.dumps(row,allow_nan=False,indent=2),media_type='application/json',
                    headers={'Content-Disposition':f'attachment; filename="{scan_id}.json"'})


def page():
    return FileResponse(Path(__file__).parent/'static'/'index.html')
