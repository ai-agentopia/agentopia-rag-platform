FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

RUN useradd --no-create-home --uid 1000 pathway && \
    chown -R pathway:pathway /app
USER pathway

CMD ["python", "src/ingest/pipeline.py"]
