# 🐙 Ephemeral Self-Hosted GitHub Actions Runner Container

A lightweight, highly secure Docker-based self-hosted GitHub Actions runner with Docker-in-Docker (DinD) support, designed specifically for ephemeral execution (auto-deregistration after a single job).

---

## 🚀 Key Features

* **Ephemeral Engine:** Configured with --ephemeral mode. After executing 1 job, the runner automatically unregisters from GitHub, cleans up runtime secrets, and terminates (ensuring a 100% clean state for every CI/CD run).
* **Docker-in-Docker (DinD):** Native support for running Docker commands (`docker build`, `docker run`, `docker-compose`) inside Actions workflows.
* **Security First:** Drops root privileges on startup using `gosu` to execute tasks safely under an unprivileged runner user.
* **Dynamic Package Injection:** Install extra APT packages on-the-fly at runtime without rebuilding the Docker image.
* **Packages Included:** Pre-packaged with essential DevOps tools: `git`, `curl`, `rsync`, `ssh`, `envsubst`, `nodejs`, `npm`, `Docker CLI`, and `Buildx`.

---

## 🔄 Architecture & Ephemeral Lifecycle

+------------------------------------------------------------------------+
|                        Docker Container Loop                           |
|                                                                        |
|  1. Fetch fresh registration token from GitHub API                     |
|  2. Execute ./config.sh --ephemeral                                    |
|  3. Start ./run.sh and listen for incoming GitHub Actions job          |
|  4. Execute Workflow Job (e.g. Terraform plan, Docker build)           |
|  5. Job Complete -> GitHub deregisters runner & cleans .runner config  |
|  6. Process exits (Container stops)                                    |
|  7. Docker --restart unless-stopped spawns a NEW pristine container    |
+------------------------------------------------------------------------+

---

## ⚙️ Environment Variables

| Variable | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| TOKEN | Yes | - | Personal Access Token (PAT) or Fine-Grained Token with repo / actions:read_write permissions to request registration tokens. |
| REPO | Yes | - | Target GitHub repository in owner/repository format (e.g. L1p0-M/Homelab). |
| RUNNER_NAME | No | auto-generated | Custom name displayed in GitHub's Settings -> Actions -> Runners tab. |
| PUID | No | 1000 | User ID (UID) assigned to the runner user inside the container for file permission alignment. |
| PGID | No | 1000 | Group ID (GID) assigned to the runner user inside the container. |
| PACKAGES | No | - | Comma-separated list of additional Debian packages to install via apt-get at container startup (e.g., wget, jq, htop). |

---

## 🛠️ Quick Start

### 1. Docker Run

```bash
docker run -d \
  --privileged \
  --name github-runner \
  --restart unless-stopped \
  -e TOKEN="ghp_yourPersonalAccessTokenHere" \
  -e REPO="L1p0-M/Homelab" \
  -e RUNNER_NAME="pve-ephemeral-runner" \
  -e PUID=1000 \
  -e PGID=1000 \
  -e PACKAGES="wget, jq, gettext-base" \
  ghcr.io/l1p0-m/github-runner-docker:latest
```

### 2. Docker Compose (Recommended)

Create a docker-compose.yml file:

```yaml
services:
  github-runner:
    image: ghcr.io/l1p0-m/github-runner-docker:latest
    container_name: github-runner
    privileged: true # Required for Docker-in-Docker functionality!
    restart: unless-stopped
    environment:
      - TOKEN=${GITHUB_PAT_TOKEN}
      - REPO=L1p0-M/Homelab
      - RUNNER_NAME=pve-ephemeral-runner
      - PUID=1000
      - PGID=1000
      - PACKAGES=wget, jq, gettext-base
```

Start the service:
```bash
docker compose up -d
```

---

## 🛡️ Security & Permissions

* **Privileged Mode:** The --privileged flag is required exclusively to initialize the Docker daemon inside the container for DinD workloads.
* **Process Isolation:** The main entrypoint drops privileges immediately after setting up environment dependencies and runs the GitHub Actions runner binary strictly as a **non-root runner user** (PUID/PGID).