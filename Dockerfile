FROM python:3.11-slim

WORKDIR /app

# Install deps first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source — .dockerignore keeps secrets out
COPY main.py aws_waste_scanner.py dashboard.html ./

# Never run as root in production
RUN adduser --disabled-password --gecos "" appuser
USER appuser

EXPOSE 8000

# Reads SMTP_USER, SMTP_PASS, etc. from platform env vars — no secrets in image
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
