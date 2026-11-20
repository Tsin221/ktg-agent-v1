FROM python:3.12-slim-bookworm
WORKDIR /app
COPY wheels /wheels
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --no-index --find-links=/wheels --require-hashes -r requirements.lock && rm -rf /wheels
COPY bridge.py /app/bridge.py
COPY cards.py /app/cards.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
CMD ["python", "/app/bridge.py"]
