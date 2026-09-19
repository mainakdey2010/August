#!/usr/bin/env python3
"""Authenticated live acceptance: run BOTH scenarios and retain actual output."""
import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

parser=argparse.ArgumentParser()
parser.add_argument('--url',default='http://localhost:8080',help='Use gcloud run services proxy for IAM authentication')
parser.add_argument('--output',default='demo-validation')
parser.add_argument('--timeout',type=int,default=2400)
args=parser.parse_args()
out=pathlib.Path(args.output);out.mkdir(parents=True,exist_ok=True)


def call(path,body=None):
    data=None if body is None else json.dumps(body).encode()
    request=urllib.request.Request(args.url.rstrip('/')+'/v1/demo'+path,data=data,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=120) as response:
        return json.load(response)


failed=False
for scenario in call('/scenarios'):
    call('/regions',scenario)
    submitted=call('/scans',{'region_id':scenario['region_id']})
    sid=submitted['scan_id'];deadline=time.monotonic()+args.timeout
    while True:
        row=call('/scans/'+sid)
        print(scenario['region_id'],sid,row['status'],row['stage'],flush=True)
        if row['status'] not in ('pending','running') or time.monotonic()>deadline:
            break
        time.sleep(10)
    (out/(scenario['region_id']+'.json')).write_text(json.dumps(row,indent=2))
    result=row.get('result') or {};gen=result.get('generation') or {}
    passed=(row['status']=='complete' and gen.get('provider')=='vertex_ai' and bool(gen.get('raw_model_response'))
            and bool(result.get('bq_job_id')) and set(result.get('previews',[]))=={'before','after'}
            and bool(result.get('map',{}).get('features'))
            and any(e['scope']=='asset_buffer' for e in result.get('packet',{}).get('evidence',[])))
    if passed:
        for label in ['before','after']:
            with urllib.request.urlopen(args.url.rstrip('/')+f'/v1/demo/scans/{sid}/previews/{label}',timeout=120) as response:
                png=response.read()
            if not png.startswith(b'\x89PNG\r\n\x1a\n'):
                passed=False
            (out/(scenario['region_id']+'-'+label+'.png')).write_bytes(png)
    print('PASS' if passed else 'FAIL',scenario['region_id'],flush=True)
    failed|=not passed
print('Model interpretation still requires human grounding review. JSON and previews saved to',out)
sys.exit(1 if failed else 0)
