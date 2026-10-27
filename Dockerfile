FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps --no-build-isolation .
COPY data ./data
COPY experiments ./experiments
COPY tests ./tests
COPY scripts ./scripts
COPY start.sh ./start.sh
ENTRYPOINT ["supply-risk"]
CMD ["--help"]
