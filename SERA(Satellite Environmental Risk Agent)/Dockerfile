FROM python:3.12-slim

# All spatial reduction is server-side in Earth Engine. psycopg2-binary
# supplies libpq; no GDAL toolchain is needed in the serving image.

WORKDIR /app

# Python dependencies are stored inside src/sera
COPY src/sera/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ src/
COPY config/ config/

ENV PYTHONPATH=/app/src

# Default: API. Override CMD for worker variants.
CMD ["uvicorn", "sera.api.main:app", "--host", "0.0.0.0", "--port", "8080"]




