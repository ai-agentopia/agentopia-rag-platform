FROM python:3.11-slim

WORKDIR /app

# System deps for Pathway's xpack parsers:
#   libmagic1 — required by UnstructuredParser (file-type detection via
#   unstructured.partition.auto). Missing this raises
#   FileFormatOrDependencyError at parse time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libmagic1 \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# libgl1 + libglib2.0-0: required at import time by cv2 (pulled in by
# unstructured_inference, which unstructured.partition.auto imports
# eagerly). Without them, any partition call on a binary doc raises
# `ImportError: libGL.so.1: cannot open shared object file`.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download NLTK resources required by unstructured's text tokenizer.
# Without this, `unstructured.partition.text` tries to download at first
# call via `nltk.download(..., download_dir=<user home>)`, which fails
# under our non-root user (no writable home). Pin the shared download
# dir and set NLTK_DATA so every process finds them.
ENV NLTK_DATA=/opt/nltk_data
RUN mkdir -p /opt/nltk_data && \
    python -c "import nltk; \
               nltk.download('averaged_perceptron_tagger_eng', download_dir='/opt/nltk_data', quiet=True); \
               nltk.download('punkt_tab', download_dir='/opt/nltk_data', quiet=True)" && \
    chmod -R a+rX /opt/nltk_data

COPY src/ src/

# Give the pathway user a writable home — some parser deps still look
# at $HOME for caches even when XDG / project-scoped dirs would work.
RUN useradd --create-home --home-dir /home/pathway --uid 1000 pathway && \
    chown -R pathway:pathway /app /home/pathway
USER pathway

CMD ["python", "src/ingest/pipeline.py"]
