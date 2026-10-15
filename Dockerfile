# Kept in a container (used for the public demo link). Free and keyless: Tesseract
# reads photos; the optional AI helper (Ollama) is not included online.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
USER user
ENV PATH=/home/user/.local/bin:$PATH KEPT_PUBLIC=1 KEPT_USE_AI=0 KEPT_DATA_DIR=/tmp/kept-data
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt gunicorn
COPY --chown=user . .
EXPOSE 7860
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:7860", "wsgi:app"]
