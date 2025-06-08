# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (if needed for cryptography)
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY main.py .

# Create directory for session files
RUN mkdir -p /app/sessions

# Set environment variable for session storage
ENV TELETHON_SESSION_DIR=/app/sessions

# Run the bot
CMD ["python", "main.py"]