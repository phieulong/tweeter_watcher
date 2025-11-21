# 1. Chọn base image Linux mới (GLIBC ≥ 2.28) + Python 3.10
FROM python:3.10-slim-bookworm

# 2. Cài các gói cần thiết
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    libxshmfence1 \
    xvfb \
    unzip \
    fonts-liberation \
    libcurl4 \
    git \
    && rm -rf /var/lib/apt/lists/*

# 3. Cài NodeJS vì Playwright cần
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g playwright

# 4. Set working directory
WORKDIR /app

# Explicit port environment for the app / Fly health check
ENV PORT=8080

# 5. Copy requirements và cài Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy toàn bộ code vào container
COPY . .

# 7. Nếu dùng Playwright, cài browser binaries
RUN playwright install --with-deps

# 8. Set command chạy app
CMD ["python", "main.py"]
