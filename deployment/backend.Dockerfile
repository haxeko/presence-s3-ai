# Backend + huấn luyện (TensorFlow CPU). Phiên bản thư viện: requirements/*.txt
FROM python:3.12.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TF_CPP_MIN_LOG_LEVEL=2
WORKDIR /app
COPY requirements/ requirements/
RUN pip install --no-cache-dir -r requirements/all.txt
COPY pyproject.toml ./
COPY training/ training/
COPY backend/ backend/
COPY dashboard/ dashboard/
RUN pip install --no-cache-dir --no-deps -e .
EXPOSE 8000
CMD ["uvicorn", "presence_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
