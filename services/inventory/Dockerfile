# Build from the services/ directory: docker build -f inventory/Dockerfile services/
FROM python:3.12-slim
WORKDIR /app
COPY inventory/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY inventory ./inventory
USER nobody
EXPOSE 8000
CMD ["uvicorn", "inventory.app:app", "--host", "0.0.0.0", "--port", "8000"]
