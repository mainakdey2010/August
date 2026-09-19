"""Persist evidence, model output and previews; private GCS in deployed runs."""
import json
import os
from pathlib import Path


def save(scan_id, name, data, content_type):
    bucket=os.environ.get('GCS_BUCKET')
    key=f'sera/demo/{scan_id}/{name}'
    if bucket:
        from google.cloud import storage
        blob=storage.Client().bucket(bucket).blob(key)
        blob.upload_from_string(data,content_type=content_type,timeout=180)
        return f'gs://{bucket}/{key}'
    if os.environ.get('K_SERVICE') or os.environ.get('CLOUD_RUN_JOB'):
        raise RuntimeError('GCS_BUCKET is required on Cloud Run')
    root=Path(os.environ.get('SERA_ARTIFACT_DIR','.sera-artifacts'))
    path=root/key
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data)
    return str(path)


def read(scan_id, name):
    key=f'sera/demo/{scan_id}/{name}'
    bucket=os.environ.get('GCS_BUCKET')
    if bucket:
        from google.cloud import storage
        return storage.Client().bucket(bucket).blob(key).download_as_bytes(timeout=60)
    return (Path(os.environ.get('SERA_ARTIFACT_DIR','.sera-artifacts'))/key).read_bytes()


def archive(scan_id, packet, features):
    """Load one immutable evidence snapshot per scan with a deterministic BQ load job ID."""
    import hashlib
    from google.cloud import bigquery
    dataset=os.environ.get('BQ_DATASET','sera_analytics_dev')
    client=bigquery.Client(project=os.environ.get('GCP_PROJECT'))
    row={'scan_id':scan_id,'asset_id':packet['asset_id'],'as_of':packet['as_of'],
         'evidence_json':json.dumps(packet,allow_nan=False),'feature_count':len(features)}
    schema=[bigquery.SchemaField('scan_id','STRING'),bigquery.SchemaField('asset_id','STRING'),
            bigquery.SchemaField('as_of','DATE'),bigquery.SchemaField('evidence_json','STRING'),
            bigquery.SchemaField('feature_count','INTEGER')]
    job_id='sera_demo_'+hashlib.sha256(scan_id.encode()).hexdigest()
    from google.api_core.exceptions import Conflict
    try:
        job=client.load_table_from_json([row],f'{client.project}.{dataset}.demo_evidence',job_id=job_id,
            job_config=bigquery.LoadJobConfig(schema=schema,write_disposition='WRITE_APPEND'))
    except Conflict:
        job=client.get_job(job_id)
    job.result(timeout=180)
    return job.job_id
