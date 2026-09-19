"""One durable scan per Cloud Run Job execution; no scale-from-zero Celery dependency."""
import argparse
import asyncio
import json
import logging
import sys
from sera.demo import artifacts, imagery, reporting
from sera.demo.evidence import summarize
from sera.demo.models import Region
from sera.demo.store import Store

log=logging.getLogger(__name__)


def execute(scan_id, *, store=None, collector=None, reporter=None, archiver=None, saver=None):
    store=store or Store()
    if not store.claim(scan_id):
        return False
    current=store.get(scan_id)
    region=Region.model_validate(current['config'])
    collector=collector or imagery.collect
    reporter=reporter or reporting.generate
    archiver=archiver or artifacts.archive
    saver=saver or artifacts.save
    def progress(stage):
        store.update(scan_id,'running',stage)
    stage='imagery'
    try:
        # Resume from durable measurements/model checkpoints, avoiding needless paid calls.
        checkpoint=current.get('result') or {}
        if 'packet' not in checkpoint:
            data=collector(region,progress)
            packet=summarize(region,scan_id,data)
            preview_names=[]
            for label,png in data.pop('images',{}).items():
                saver(scan_id,label+'.png',png,'image/png')
                preview_names.append(label)
            checkpoint={'packet':packet,'map':{'type':'FeatureCollection','features':data['features']},
                        'bounds':data['bounds'],'previews':preview_names}
            store.update(scan_id,'running','measurements_saved',checkpoint)
        packet=checkpoint['packet']
        if not packet['evidence']:
            store.update(scan_id,'insufficient_data','complete',checkpoint)
            return True
        stage='adk_reporting'
        progress(stage)
        if 'generation' not in checkpoint:
            checkpoint['generation']=asyncio.run(reporter(packet))
            store.update(scan_id,'running','report_saved',checkpoint)
        stage='archive'
        progress(stage)
        checkpoint['bq_job_id']=archiver(scan_id,packet,checkpoint['map']['features'])
        saver(scan_id,'evidence.json',json.dumps(checkpoint,allow_nan=False,indent=2).encode(),'application/json')
        store.update(scan_id,'complete','complete',checkpoint)
        return True
    except Exception as exc:
        log.exception('Scan %s failed at %s',scan_id,stage)
        store.update(scan_id,'failed',stage,error=f'{type(exc).__name__}: check worker logs and retry this scan')
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('scan_id')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    execute(args.scan_id)


if __name__=='__main__':
    main()
