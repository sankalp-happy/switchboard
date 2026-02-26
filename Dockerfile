FROM python:3.11-slim

WORKDIR /app

RUN mkdir -p /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Environment variable to indicate we're running in Docker
ENV IN_DOCKER=true

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
