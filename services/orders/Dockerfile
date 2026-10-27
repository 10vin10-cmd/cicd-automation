# Build from the services/ directory: docker build -f orders/Dockerfile services/
FROM python:3.12-slim
WORKDIR /app
COPY orders/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY orders ./orders
USER nobody
EXPOSE 8000
CMD ["uvicorn", "orders.app:app", "--host", "0.0.0.0", "--port", "8000"]
