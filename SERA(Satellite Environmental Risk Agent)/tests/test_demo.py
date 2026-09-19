"""Contract/integration tests with synthetic adapters, never live success evidence."""
import asyncio
import copy
import json
import math
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sera.demo import api, reporting, worker
from sera.demo.evidence import summarize
from sera.demo.imagery import baseline_windows, bounds
from sera.demo.models import Region
from sera.demo.scenarios import SCENARIOS
from sera.demo.store import Store

ID='demo_'+'a'*32


@pytest.fixture
def store(tmp_path):
    s=Store('sqlite:///'+str(tmp_path/'scans.db'))
    s.migrate()
    return s


def measurements(region):
    props={'h3_cell':'test-cell','area_m2':100,'asset_overlap_m2':25}
    for idx in region.indices:
        before,after=(.6,.1) if idx in ('NBR','NDVI') else (-.2,.3)
        props.update({idx+'_before':before,idx+'_after':after,idx+'_delta':after-before,idx+'_coverage':.9})
        for year,value in [(2019,.4),(2020,.5),(2021,.6)]:
            props[idx+'_baseline_'+str(year)]=value
            props[idx+'_baseline_coverage_'+str(year)]=.9
    return {'type':'FeatureCollection','features':[{'type':'Feature','properties':props,
            'geometry':{'type':'Polygon','coordinates':[[[0,0],[1,0],[1,1],[0,0]]]}}],
            'manifest':{f'baseline_{y}':{} for y in [2019,2020,2021]},'bounds':bounds(region),'images':{},'missing':[]}


def report(packet):
    return {'scan_id':packet['scan_id'],'asset_id':packet['asset_id'],
        'observations':[{'text':'Synthetic contract-test observation.','evidence_ids':[packet['evidence'][0]['evidence_id']]}],
        'interpretation':'Test fixture only.','limitations':['Synthetic test fixture.'],
        'review_action':'Review evidence.','human_review_required':True}


async def fake_reporter(packet):
    return {'report':reporting.validate(report(packet),packet),'model':'test-double','provider':'test'}


def create(store,scenario=0):
    s=SCENARIOS[scenario]
    store.region(s)
    store.create(ID,'fingerprint',s.model_dump(mode='json'))
    return s


def test_dates_and_scope_validation():
    for s in SCENARIOS:
        assert all(end<s.after.start for _,end in baseline_windows(s))
        assert s.before.end<=s.after.start<s.after.end<=s.as_of
    cfg=SCENARIOS[0].model_dump(mode='json')
    cfg['as_of']='2022-09-10'
    with pytest.raises(ValidationError):
        Region.model_validate(cfg)
    cfg=SCENARIOS[0].model_dump(mode='json');cfg['indices']=['LST']
    with pytest.raises(ValidationError):
        Region.model_validate(cfg)


@pytest.mark.parametrize('scenario',[0,1])
def test_complete_pipeline_and_duplicate_worker(store,scenario):
    s=create(store,scenario)
    collector=Mock(return_value=measurements(s)); archive=Mock(return_value='test-load-job');save=Mock()
    assert worker.execute(ID,store=store,collector=collector,reporter=fake_reporter,archiver=archive,saver=save)
    row=store.get(ID)
    assert row['status']=='complete'
    assert row['result']['generation']['provider']=='test'
    assert len(row['result']['packet']['evidence'])==len(s.indices)*2
    assert not worker.execute(ID,store=store,collector=collector,reporter=fake_reporter,archiver=archive,saver=save)
    assert collector.call_count==archive.call_count==1


def test_archive_failure_resumes_without_model_or_ee_repeat(store):
    s=create(store);collect=Mock(return_value=measurements(s));calls=[]
    async def counted(p):
        calls.append(1);return await fake_reporter(p)
    with pytest.raises(RuntimeError):
        worker.execute(ID,store=store,collector=collect,reporter=counted,
                       archiver=Mock(side_effect=RuntimeError('test failure')),saver=Mock())
    assert store.get(ID)['status']=='failed'
    worker.execute(ID,store=store,collector=collect,reporter=counted,archiver=Mock(return_value='ok'),saver=Mock())
    assert collect.call_count==1 and len(calls)==1
    assert store.get(ID)['status']=='complete'


def test_model_failure_never_falls_back_to_template(store):
    s=create(store)
    async def failing(p):raise RuntimeError('model unavailable')
    with pytest.raises(RuntimeError):
        worker.execute(ID,store=store,collector=lambda *a:measurements(s),reporter=failing,archiver=Mock(),saver=Mock())
    row=store.get(ID)
    assert row['status']=='failed' and row['stage']=='adk_reporting'
    assert 'generation' not in row['result']


def test_no_usable_imagery_is_insufficient_not_low_risk(store):
    s=create(store);data=measurements(s)
    for key in list(data['features'][0]['properties']):
        if key.endswith('_coverage'):data['features'][0]['properties'][key]=0
    reporter=Mock(side_effect=AssertionError('must not call model'))
    worker.execute(ID,store=store,collector=lambda *a:data,reporter=reporter,archiver=Mock(),saver=Mock())
    assert store.get(ID)['status']=='insufficient_data'
    reporter.assert_not_called()


def test_direction_and_low_variability():
    s=SCENARIOS[1];data=measurements(s);p=summarize(s,ID,data)
    nbr=next(e for e in p['evidence'] if e['index']=='NBR')
    assert nbr['dnbr']==.5 and nbr['change_review_signal']
    for f in data['features']:
        for year in [2019,2020,2021]:f['properties'][f'NBR_baseline_{year}']=.5
    p=summarize(s,ID,data)
    assert next(e for e in p['evidence'] if e['index']=='NBR')['z_score'] is None
    assert p['data_gaps']


def test_unknown_citations_and_identity_rejected():
    p=summarize(SCENARIOS[0],ID,measurements(SCENARIOS[0]))
    r=report(p);r['observations'][0]['evidence_ids']=['invented']
    with pytest.raises(ValueError):reporting.validate(r,p)
    r=report(p);r['scan_id']='different'
    with pytest.raises(ValueError):reporting.validate(r,p)
    r=reporting.validate(report(p),p)
    assert all(x in r['limitations'] for x in p['limitations']+p['data_gaps'])


def test_concurrent_submit_and_claim(store):
    cfg=SCENARIOS[0].model_dump(mode='json')
    with ThreadPoolExecutor(max_workers=4) as pool:
        result=list(pool.map(lambda n:store.create('demo_'+str(n)*32,'same',cfg),range(4)))
    assert sum(created for _,created in result)==1
    actual=result[0][0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed=list(pool.map(lambda _:store.claim(actual),range(4)))
    assert sum(claimed)==1


def test_api_replay_dispatch_poll_retry_export(store):
    app=FastAPI();app.include_router(api.router);app.dependency_overrides[api.get_store]=lambda:store
    client=TestClient(app)
    assert len(client.get('/v1/demo/scenarios').json())==2
    s=SCENARIOS[0]
    assert client.post('/v1/demo/scenarios/'+s.region_id+'/register').status_code==201
    with patch.object(api,'dispatch',side_effect=RuntimeError('not configured')):
        result=client.post('/v1/demo/scans',json={'region_id':s.region_id}).json()
    sid=result['scan_id'];assert result['status']=='dispatch_failed'
    with patch.object(api,'dispatch') as dispatch:
        assert client.post('/v1/demo/scans',json={'region_id':s.region_id}).json()['scan_id']==sid
        dispatch.assert_not_called()
        assert client.post('/v1/demo/scans/'+sid+'/retry').status_code==202
        dispatch.assert_called_once()
    assert client.get('/v1/demo/scans/'+sid+'/export').status_code==200
    assert client.get('/v1/demo/scans/invalid').status_code==404
    assert client.get('/v1/demo/scans/'+sid+'/previews/before').status_code==404


def test_actual_adk_runner_with_model_double():
    # Runs installed ADK Agent/Runner/session/schema path; does NOT make a Vertex call.
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    packet=summarize(SCENARIOS[0],ID,measurements(SCENARIOS[0]))
    class ModelDouble(BaseLlm):
        async def generate_content_async(self,llm_request,stream=False):
            yield LlmResponse(content=types.Content(role='model',parts=[types.Part(text=json.dumps(report(packet)))]))
    result=asyncio.run(reporting.generate(packet,model_override=ModelDouble(model='test-double')))
    assert result['provider']=='test'
    assert result['report']['scan_id']==ID
    assert result['adk_version']=='2.9.2'


def test_cloud_sqlite_rejected(monkeypatch,tmp_path):
    monkeypatch.setenv('K_SERVICE','demo')
    with pytest.raises(RuntimeError):Store('sqlite:///'+str(tmp_path/'db'))


def test_real_ee_expression_graph_for_both_scenarios(monkeypatch):
    import ee
    from ee.apitestcase import ApiTestCase
    from sera.demo.imagery import collect
    harness=ApiTestCase();harness.setUp()
    graphs=[]
    try:
        monkeypatch.setenv('GCP_PROJECT','test-project')
        def reduced(self):
            graphs.append(self.serialize())
            return {'features':[]}
        def thumb(self,params):
            graphs.append(self.serialize())
            return 'https://example.invalid/test.png'
        response=Mock(content=b'\x89PNG\r\n\x1a\nfixture')
        with patch.object(ee,'Initialize'),patch.object(ee.Number,'getInfo',return_value=2), \
             patch.object(ee.List,'getInfo',return_value=['test-scene']), \
             patch.object(ee.FeatureCollection,'getInfo',reduced), \
             patch.object(ee.Image,'getThumbURL',thumb),patch('requests.get',return_value=response):
            for scenario in SCENARIOS:
                result=collect(scenario)
                assert set(result['images'])=={'before','after'}
        assert len(graphs)==6
        assert 'SCL' in graphs[0] and 'QA60' not in graphs[0]
        assert 'EPSG:6933' in graphs[0]
        assert '2023-08-31' in graphs[3]
    finally:
        harness.tearDown()


def test_submit_capacity_guard_and_force_recompute(store):
    cfg=SCENARIOS[0].model_dump(mode='json')
    store.create('demo_'+'1'*32,'one',cfg)
    store.create('demo_'+'2'*32,'two',cfg)
    with pytest.raises(RuntimeError,match='DEMO_CAPACITY'):
        store.create('demo_'+'3'*32,'three',cfg)
    assert store.create('demo_'+'4'*32,'one',cfg)==('demo_'+'1'*32,False)


def test_total_area_coverage_gate():
    s=SCENARIOS[0];data=measurements(s)
    absent=copy.deepcopy(data['features'][0]);absent['properties']['area_m2']=1000
    absent['properties']['asset_overlap_m2']=250
    for idx in s.indices:absent['properties'][idx+'_coverage']=0
    data['features'].append(absent)
    packet=summarize(s,ID,data)
    assert packet['evidence']==[]
    assert any('50%' in x for x in packet['data_gaps'])


def test_cross_year_baseline_dates():
    cfg=SCENARIOS[0].model_dump(mode='json')
    cfg['after']={'start':'2022-12-20','end':'2023-01-10'};cfg['as_of']='2023-01-10'
    scenario=Region.model_validate(cfg)
    for start,end in baseline_windows(scenario):
        assert start<end<scenario.after.start
        assert end.year==start.year+1


def test_archive_retry_uses_dataset_location(monkeypatch):
    from google.api_core.exceptions import Conflict
    from sera.demo.artifacts import archive
    client=Mock(project='test-project')
    client.get_dataset.return_value.location='asia-south1'
    client.load_table_from_json.side_effect=Conflict('already submitted')
    client.get_job.return_value.job_id='existing-load'
    client.get_job.return_value.error_result=None
    packet={'asset_id':'test','as_of':'2022-09-20'}
    with patch('google.cloud.bigquery.Client',return_value=client):
        assert archive(ID,packet,[])=='existing-load'
    assert client.get_job.call_args.kwargs['location']=='asia-south1'
    assert client.load_table_from_json.call_args.kwargs['location']=='asia-south1'


def test_model_cannot_disable_human_review():
    p=summarize(SCENARIOS[0],ID,measurements(SCENARIOS[0]))
    r=report(p);r['human_review_required']=False
    with pytest.raises(ValidationError):reporting.validate(r,p)


def test_failed_bq_load_can_be_retried_without_reusing_failed_job():
    from google.api_core.exceptions import Conflict
    from sera.demo.artifacts import archive
    client=Mock(project='test-project')
    client.get_dataset.return_value.location='asia-south1'
    completed=Mock(job_id='retry-load')
    client.load_table_from_json.side_effect=[Conflict('previous job'),completed]
    client.get_job.return_value.error_result={'reason':'accessDenied'}
    with patch('google.cloud.bigquery.Client',return_value=client):
        assert archive(ID,{'asset_id':'test','as_of':'2022-09-20'},[])=='retry-load'
    assert client.load_table_from_json.call_args.kwargs['job_id'].endswith('_retry_1')
