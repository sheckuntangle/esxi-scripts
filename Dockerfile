FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /workspace
COPY requirements.txt requirements-web.txt /tmp/dependencies/
RUN python -m pip install --upgrade "pip>=26.2,<27" \
    && pip install -r /tmp/dependencies/requirements-web.txt
COPY . /workspace
USER 1000:1000
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--worker-class", "gthread", "--threads", "16", "--timeout", "0", "--access-logfile", "-", "web.app:create_app()"]
