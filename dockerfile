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
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
    docker-buildx-plugin \
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
