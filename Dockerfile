FROM python:3.11-alpine

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx

COPY app.py .
COPY static ./static

EXPOSE 9832

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9832"]
