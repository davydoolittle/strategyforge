FROM python:3.12-slim
COPY app/ /app/
WORKDIR /app
CMD ["python", "-u", "main.py"]
