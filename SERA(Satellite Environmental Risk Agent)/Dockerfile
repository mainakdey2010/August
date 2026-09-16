FROM python:3.11-slim

# PostGIS client libs (psycopg2 needs libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc gdal-bin libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

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



