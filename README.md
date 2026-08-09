# Self-Hosted GitHub Actions Runner Container

A lightweight, Docker-based self-hosted GitHub Actions runner supporting **Docker-out-of-Docker (DooD)** execution via host socket mounting.

## Features

* **Base:** Python 3.13-slim
* **Docker CLI & Buildx** pre-installed
* **Dynamic GID mapping:** Automatically matches container socket permissions with the host socket
* **Security:** Drops root privileges using `gosu` to run as the `runner` user
* **Included tools:** `git`, `curl`, `rsync`, `ssh`, `gosu`

---


## Quick Start
### Docker Run
```Bash
docker run -d \
  --name github-runner \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e TOKEN=*your selfhosted github runner token* \
  -e REPO=exampleuser/examplerepo
  -e PUID=1000 \
  -e PGID=1000 \
  -e RUNNER_NAME=selfhosted_runner \
  ghcr.io/l1p0-m/github-runner-docker:latest
```
### Docker Compose
```YAML
services:
  github-runner:
    image: ghcr.io/l1p0-m/github-runner-docker:latest
    container_name: github-runner
    restart: unless-stopped
    environment:
      - RUNNER_NAME=selfhosted_runner
      - PUID=1000
      - PGID=1000
      - TOKEN=*your selfhosted github runner token*
      - REPO=exampleuser/examplerepo
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

### Removing
To remove the runner, set the `REMOVE_RUNNER` environment variable to `true` and start the container!
