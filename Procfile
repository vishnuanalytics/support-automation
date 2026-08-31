web: uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m api.worker
cdc: python -m ingestion.sf_cdc_watch
poller: python -m ingestion.email_watch --interval 15
