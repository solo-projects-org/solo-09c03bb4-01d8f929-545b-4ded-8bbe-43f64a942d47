FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RECALC_TIMEOUT_SEC=60 \
    RECALC_MAX_PARALLEL=2 \
    RECALC_WORK_DIR=/tmp/recalc_batches

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home recalc \
    && mkdir -p "$RECALC_WORK_DIR" \
    && chown -R recalc:recalc /srv "$RECALC_WORK_DIR"
USER recalc

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
