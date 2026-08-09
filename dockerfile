FROM python:3.13.9-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ssh \
    gosu \
    git \
    ca-certificates \
    libkrb5-3 \
    zlib1g \
    liblttng-ust1t64 \
    liblttng-ust-common1t64 \
    liblttng-ust-ctl5t64 \
    libnuma1 \
    libicu-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
COPY entrypoint.sh /entrypoint.sh
RUN mkdir /app
ADD runner.py /app
WORKDIR /app
RUN mkdir github-runner

RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

CMD ["python", "/app/runner.py"]
