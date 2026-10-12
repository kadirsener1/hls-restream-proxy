FROM python:3.11-slim

WORKDIR /app

# Sistem bağımlılıklarını yükle (curl_cffi'nin derlenmesi için gerekli)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    libcurl4-openssl-dev libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["python", "hls-proxy.py"]
