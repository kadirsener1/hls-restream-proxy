FROM python:3.12-slim

WORKDIR /app

# curl_cffi için gerekli sistem bağımlılıkları
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    libcurl4-openssl-dev libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8080

ENV PYTHONUNBUFFERED=1

CMD ["python", "hls-proxy.py"]
