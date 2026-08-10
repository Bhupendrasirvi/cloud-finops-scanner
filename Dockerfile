FROM python:3.11-slim

WORKDIR /app

# Install deps first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source — .dockerignore keeps secrets out
COPY main.py aws_waste_scanner.py dashboard.html ./

# Set permissions for appuser to allow writing sqlite database in /app
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Reads PORT env var from cloud platform (e.g. Render), defaulting to 8000
CMD ["python", "main.py"]
