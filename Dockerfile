FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

# Data volume for config persistence
VOLUME ["/data"]

EXPOSE 7272

CMD ["python", "app/main.py"]
