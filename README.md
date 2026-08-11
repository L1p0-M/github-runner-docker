# Self-Hosted GitHub Actions Runner Container

A lightweight, Docker-based self-hosted GitHub Actions runner supporting **Docker-in-Docker (DiD) actions**.

## Features

* **Base:** Python 3.13-slim
* **Docker CLI & Buildx** pre-installed
* **Security:** Drops root privileges using `gosu` to run as the `runner` user
* **Included tools:** `git`, `curl`, `rsync`, `ssh`, `gosu`, `docker`, `envsubst`

---


## Quick Start
### Docker Run
```Bash
docker run -d \
  --privileged \
  --name github-runner \
  --restart unless-stopped \
  -v ./docker_files:/var/lib/docker \ 
  -e TOKEN=*your selfhosted github runner token* \
  -e REPO=exampleuser/examplerepo \
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
    privileged: true  # Needs to be set for docker-in-docker to work!
    restart: unless-stopped
    environment:
      - RUNNER_NAME=selfhosted_runner
      - PUID=1000
      - PGID=1000
      - TOKEN=*your selfhosted github runner token*
      - REPO=exampleuser/examplerepo
    volumes:
      - ./docker_files:/var/lib/docker # Bind mount docker folder,because we dont want to repull the action images at every restart
```

### Removing
To remove the runner, set the `REMOVE_RUNNER` environment variable to `true` and start the container!
