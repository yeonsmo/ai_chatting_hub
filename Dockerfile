FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --no-proxy-headers: X-Forwarded-For 해석은 앱의 IPWhitelistMiddleware가 단독 수행
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3899", "--no-proxy-headers"]
