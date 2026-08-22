# Pinned to a digest, not just a tag: `python:3.11-slim` is a moving target, and a
# contributor rebuilding next month would otherwise get a different base than the
# one CI tested. Dependabot's docker ecosystem proposes the bumps.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

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
