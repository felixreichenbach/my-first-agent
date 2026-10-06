FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 agent
COPY requirements.txt requirements-dev.txt ./

# --- test image: adds pytest; tests are mounted by docker-compose ---
FROM base AS test
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY *.py ./
USER agent
ENTRYPOINT ["python", "-m", "pytest"]

# --- runtime image (default target) ---
FROM base AS runtime
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
USER agent
ENTRYPOINT ["python", "main.py"]
