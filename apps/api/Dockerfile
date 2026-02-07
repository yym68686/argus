FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY apps/api/requirements.txt /app/apps/api/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/apps/api/requirements.txt

COPY apps/api/app.py /app/apps/api/app.py
COPY docs/templates /app/docs/templates

EXPOSE 8080
WORKDIR /app/apps/api

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
