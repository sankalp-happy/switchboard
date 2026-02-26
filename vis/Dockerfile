FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir streamlit requests

COPY app.py .

EXPOSE 3000

CMD ["streamlit", "run", "app.py", "--server.port", "3000", "--server.headless", "true", "--server.address", "0.0.0.0"]
