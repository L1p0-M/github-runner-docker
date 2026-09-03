FROM python:3.14.7-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ssh \
    gosu \
    git \
    tini \
    ca-certificates \
    libkrb5-3 \
    zlib1g \
    liblttng-ust1t64 \
    liblttng-ust-common1t64 \
    liblttng-ust-ctl5t64 \
    libnuma1 \
    libicu-dev \
    rsync \
    unzip \
    nodejs \
    npm \
    gettext-base \
    iptables \
    fuse-overlayfs \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
    docker-ce \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
COPY entrypoint.sh /entrypoint.sh
RUN mkdir /app
ADD runner.py /app
WORKDIR /app
RUN mkdir github-runner

RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]

CMD ["python", "/app/runner.py"]
