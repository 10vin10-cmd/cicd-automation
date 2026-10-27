# Build from the services/ directory: docker build -f payments/Dockerfile services/
FROM python:3.12-slim
WORKDIR /app
COPY payments/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY payments ./payments
USER nobody
EXPOSE 8000
CMD ["uvicorn", "payments.app:app", "--host", "0.0.0.0", "--port", "8000"]
