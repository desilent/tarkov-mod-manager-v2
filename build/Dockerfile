FROM python:3.12-slim

WORKDIR /app

# Install p7zip for .7z, .rar, .zip extraction
RUN apt-get update && apt-get install -y --no-install-recommends p7zip-full && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

# Data volume for config persistence
VOLUME ["/data"]

EXPOSE 7272

CMD ["python", "app/main.py"]
