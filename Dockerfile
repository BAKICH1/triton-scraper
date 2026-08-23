FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1
# Полный монитор: цикл скана каждые 60 c + публикация каждые 90 c + дашборд на :8080
CMD ["python3", "server.py", "8080"]
