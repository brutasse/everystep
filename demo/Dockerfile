FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the everystep library from the repo root (build context).
COPY pyproject.toml README.md ./
COPY everystep ./everystep
RUN pip install --no-cache-dir hatchling \
    && pip install --no-cache-dir --no-build-isolation .

# The demo project.
COPY demo/manage.py /app/demo/manage.py
COPY demo/config /app/demo/config
COPY demo/jobs /app/demo/jobs
COPY demo/entrypoint.sh /app/demo/entrypoint.sh
RUN chmod +x /app/demo/entrypoint.sh

WORKDIR /app/demo
EXPOSE 8000
CMD ["sh", "entrypoint.sh", "python manage.py runserver 0.0.0.0:8000"]
