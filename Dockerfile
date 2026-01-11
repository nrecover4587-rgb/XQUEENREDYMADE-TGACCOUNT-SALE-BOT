# Heroku-compatible Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system packages required by some libs
RUN apt-get update && apt-get install -y \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy full project
COPY . .

# Start bot
CMD ["python", "bot.py"]
