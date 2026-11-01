# Pinned to a digest, not just a tag: `python:3.11-slim` is a moving target, and a
# contributor rebuilding next month would otherwise get a different base than the
# one CI tested. Dependabot's docker ecosystem proposes the bumps.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

WORKDIR /app

RUN mkdir -p /app/data

# requirements.txt only, never requirements-dev.txt — the production image must
# not ship a test runner. CI asserts this; see .github/workflows/ci.yml.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Environment variable to indicate we're running in Docker
ENV IN_DOCKER=true

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
