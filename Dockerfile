FROM python:3.12-slim
LABEL authors="jinseoki"

WORKDIR /app

# 의존성
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# 기본 실행 명령
CMD ["python", "main.py"]