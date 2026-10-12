# ---- 1단계: 기본 이미지 ----
# Python 3.11이 설치된 가벼운 리눅스 (Debian slim)
FROM python:3.11-slim

# ---- 2단계: Playwright 브라우저에 필요한 시스템 패키지 ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

# ---- 3단계: 작업 디렉토리 설정 ----
WORKDIR /app

# ---- 4단계: 의존성 먼저 설치 (캐시 활용) ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

# ---- 5단계: 코드 복사 ----
COPY . .

# ---- 6단계: data 폴더 생성 (DB 저장용) ----
RUN mkdir -p /app/data

# ---- 7단계: 기본 실행 명령 ----
CMD ["python", "main.py", "crawl", "--limit-per-site", "30", "--report", "data/report.md"]
