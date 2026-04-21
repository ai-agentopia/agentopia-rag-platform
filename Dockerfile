FROM python:3.11-slim

WORKDIR /app

# System deps for Pathway's xpack parsers:
#   libmagic1 — required by UnstructuredParser (file-type detection via
#   unstructured.partition.auto). Missing this raises
#   FileFormatOrDependencyError at parse time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

RUN useradd --no-create-home --uid 1000 pathway && \
    chown -R pathway:pathway /app
USER pathway

CMD ["python", "src/ingest/pipeline.py"]
