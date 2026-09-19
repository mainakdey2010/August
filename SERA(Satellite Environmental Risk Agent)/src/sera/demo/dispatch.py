"""Explicit worker dispatch through Cloud Run Jobs, or a local subprocess."""
import os
import re
import subprocess
import sys


def dispatch(scan_id):
    if not re.fullmatch(r'demo_[a-f0-9]{32}',scan_id):
        raise ValueError('Invalid scan ID')
    job=os.environ.get('SERA_DEMO_JOB')
    if job:
        if not re.fullmatch(r'projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z0-9-]+',job):
            raise ValueError('SERA_DEMO_JOB must be a full Cloud Run Job resource name')
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        with AuthorizedSession(credentials) as session:
            response=session.post(f'https://run.googleapis.com/v2/{job}:run',
                json={'overrides':{'containerOverrides':[{'args':['-m','sera.demo.worker',scan_id]}]}},timeout=60)
            response.raise_for_status()
            return response.json()['name']
    if os.environ.get('SERA_DEMO_LOCAL_WORKER')=='true' and not os.environ.get('K_SERVICE'):
        subprocess.Popen([sys.executable,'-m','sera.demo.worker',scan_id],start_new_session=True)
        return 'local-subprocess'
    raise RuntimeError('Configure SERA_DEMO_JOB or explicitly enable SERA_DEMO_LOCAL_WORKER=true locally')
