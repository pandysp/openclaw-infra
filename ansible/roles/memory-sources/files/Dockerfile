# Text extraction for memory search: no network, read-only workspace, runs as UID 1000.
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN apt-get update && apt-get install -y --no-install-recommends \
      poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu \
      python3 python3-docx python3-openpyxl \
    && rm -rf /var/lib/apt/lists/*
COPY memory-sources.py /usr/local/bin/memory-sources
RUN chmod 755 /usr/local/bin/memory-sources
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/memory-sources"]
