# GitHub Actions Runner Orchestrator

A robust, asynchronous Python-based orchestrator designed for homelab environments to manage ephemeral GitHub Actions runners efficiently via the Docker API. 

---

## Key Features

* **Asynchronous Architecture:** Non-blocking concurrent event loops, GitHub API polling with **ETag caching**, and parallel task execution.
* **Ephemeral Runners & Docker Event Monitoring:** Every runner is ephemeral and automatically destroyed after finishing a job. The orchestrator actively listens to real-time **Docker daemon events**, ensuring that if a runner container dies unexpectedly, it immediately cleans up and spawns a fresh, clean replacement if required.
* **Smart State Recovery & Shutdown:** Intelligent startup synchronization recovers unkilled containers from previous runs, while graceful shutdown ensures active busy runners finish their jobs before cleanup.
- **Observability & Metrics:** Built-in Prometheus metrics endpoint (`/metrics`) exposing real-time tracking for total runners, idle runners, and job counts broken down per configured repository.
* **Security & Process Management:** 
  * Runs on a lightweight **Alpine Linux** (`python:3.13.9-alpine`) base image.
  * Uses **`tini` as PID 1** for proper signal handling and zombie process reaping.
  * Drops privileges dynamically using **`gosu`** to match your custom `PUID`/`PGID`.

---

## Configuration (`config.toml`)

Create a `config.toml` file in your project directory to define your default settings and repository-specific runner pools. 

> **Supported Runner Images:** You can use any of the pre-configured base image tags: `debian-latest`, `ubuntu-latest`, or `ubuntu-24.04`.

```toml
[defaults]
check_interval_seconds = 10
tmpfs_enabled = true
tmpfs_size = 16
image = "debian-latest"

[[runners]]
name = "readme-runner"
repo = "L1p0-M/L1p0-M"
image = "ubuntu-latest"
min_idle = 2
max_total = 3

[[runners]]
name = "teszt-ci"
repo = "L1p0-M/Homelab"
image = "ubuntu-24.04"
min_idle = 3
max_total = 8
```

---

## Deployment

Make sure you have your `config.toml` prepared and a valid GitHub Personal Access Token (`TOKEN`) with appropriate repository/admin permissions.

### Option 1: Docker Compose (Recommended)

```yaml

services:
  runner-orchestrator:
    image: ghcr.io/l1p0-m/github-runner-controller:latest
    container_name: runner-orchestrator
    restart: unless-stopped
    environment:
      - PUID=1000
      - PGID=1000
      - DEBUG=info
      - TZ=Europe/Budapest
      - TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxx
    ports:
      - 8080:8080 # Prometheus /metrics endpoint port
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./config.toml:/app/config.toml
```

### Option 2: Docker Run

If you prefer deploying via a raw Docker CLI command, use the following:

```bash
docker run -d \
  --name runner-orchestrator \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PUID=1000 \
  -e PGID=1000 \
  -e DEBUG=info \
  -e TZ=Europe/Budapest \
  -e TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxx \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/config.toml:/app/config.toml \
  ghcr.io/l1p0-m/github-runner-controller:latest
```

---

## Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `TOKEN` | GitHub Personal Access Token with repo/runner scopes. | **Required** |
| `PUID` | User ID to run the orchestrator process inside the container. | `1000` |
| `PGID` | Group ID to run the orchestrator process inside the container. | `1000` |
| `DEBUG` | Logging level (`debug`, `info`, `warning`, `error`). | `info` |
| `TZ` | Timezone for the container environment. | `UTC` |

---

## Monitoring & Metrics

The orchestrator exposes a standard Prometheus metrics endpoint at `http://<host-ip>:8080/metrics`. 
It tracks key performance and scaling indicators individually for each specified repository, including:
- **Total Runner Count** (`total_runner_count`)
- **Idle Runner Count** (`idle_runner_count`)
- **Job/Queue Count** (`jobs_in_queue`)

### Example Prometheus Configuration (`prometheus.yml`)

Add the following scrape job to your `prometheus.yml` to start collecting metrics from the orchestrator:

```yaml
scrape_configs:
  - job_name: 'github-runner-orchestrator'
    static_configs:
      - targets: ['<host-ip-or-container-name>:8080']
    scrape_interval: 15s
```

And now you can easily start to visualize scaling behavior using `Grafana` dashboards.

---

## How It Works

1. **Bootstrap & Sync:** On startup, the orchestrator reads the configuration, connects to the Docker daemon, and adopts any orphaned runner containers left behind from unexpected restarts.
2. **Polling & Scaling:** It periodically queries the GitHub API (leveraging HTTP ETags to save rate limits) to monitor queued jobs and runner states, automatically scaling up or down based on `min_idle` and `max_total` constraints.
3. **Event-Driven Lifecycle Management:** Runners execute single jobs and terminate. The orchestrator streams Docker events and GitHub API hooks to clean up unregistered instances instantly, automatically spinning up fresh replacements to maintain desired pool capacity without causing name collisions.