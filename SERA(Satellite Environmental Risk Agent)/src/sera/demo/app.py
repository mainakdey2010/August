"""Focused demo service; legacy Celery endpoints remain in sera.api.main."""
from fastapi import FastAPI
from sera.demo.api import router, page

app=FastAPI(title='SERA live environmental replay',version='2.0.0')
app.include_router(router)
app.add_api_route('/',page,methods=['GET'],include_in_schema=False)
