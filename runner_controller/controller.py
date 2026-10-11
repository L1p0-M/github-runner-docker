import signal
import aiodocker
import aiohttp
import os
import asyncio
from jinja2 import Environment, FileSystemLoader
import json
import tomllib
from pathlib import Path as pathlibpath
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict


log_level_env = os.environ.get('DEBUG', 'INFO').upper()
log_level = getattr(logging, log_level_env, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("RunnerController")


class GitHubAPI:
    """Manage GitHub Actions API access for a repository and track runner queue state."""

    def __init__(self, repo):
        """Initialize the GitHub API session, repository metadata, and endpoint URLs."""
        self.etag = {}
        self.queue = 0
        self.repo = repo
        self.session = aiohttp.ClientSession()

        self.urls = {
            "runner": f"https://api.github.com/repos/{repo}/actions/runners",
            "jobs": f"https://api.github.com/repos/{repo}/actions/runs?status=queued",
        }

        self.token = os.environ.get('TOKEN')
        self.base_headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json"
        }

    async def force_unregister(self, runner):
        """Remove a runner from GitHub and return whether the call succeeded."""
        try:
            if not runner.id:
                logger.debug(f"No need to unregister {runner.name}")
                return True

            url = f"https://api.github.com/repos/{self.repo}/actions/runners/{runner.id}"
            async with self.session.delete(url=url, headers=self.base_headers) as resp:
                if resp.status in [204, 200]:
                    logger.debug(f"Runner {runner.name} deregistered via API: status {resp.status}")
                    return True
                else:
                    logger.warning(f"Failed to unregister {runner.name} via API")
                    return False

        except Exception as e:
            logger.error(f"Failed to unregister {runner.name} via API: {e}")
            return False

    async def get_github_datas(self):
        """Fetch runner and queued-job data from GitHub and update the cached state."""

        async def process_jobs(data):
            """Count queued workflow jobs returned in the GitHub run payload."""
            runs = data.get("workflow_runs", [])
            total_queued_jobs = 0

            if not runs:
                return

            for run in runs:
                jobs_url = run.get("jobs_url")
                headers = self.base_headers.copy()

                if jobs_url in self.etag:
                    headers["If-None-Match"] = self.etag[jobs_url]

                try:
                    async with self.session.get(jobs_url, headers=headers) as j_resp:
                        if j_resp.status == 304:
                            logger.debug(f"Got response 304, No changes in {self.repo}")
                            continue

                        if j_resp.status == 200:
                            self.etag[jobs_url] = j_resp.headers.get("ETag")
                            j_data = await j_resp.json()
                            queued_jobs = [j for j in j_data.get("jobs", []) if j["status"] == "queued"]
                            total_queued_jobs += len(queued_jobs)

                except Exception as e:
                    logger.error(f"Failed to get queued jobs for {self.repo}: {e}")

            logger.info(f"Remaining jobs: {total_queued_jobs}")
            self.queue = total_queued_jobs

        async def process_runner(data):
            """Update tracked runner status and IDs from the GitHub runners payload."""
            runners = data.get("runners", [])
            logger.debug(runners)

            self.idle_count = sum(1 for r in runners if r["busy"] is False and r["status"] == "online")
            self.total_count = len(runners)

            for runner in runners:
                deployed_runners = self.runners
                if runner["name"] in deployed_runners.keys():
                    deployed_runners[f"{runner['name']}"].state = runner["status"] if runner["busy"] is False else "busy"
                    deployed_runners[f"{runner['name']}"].id = runner["id"]

        to_check = ["runner", "jobs"]
        for data_type in to_check:
            headers = self.base_headers.copy()

            if data_type in self.etag:
                headers["If-None-Match"] = self.etag[data_type]

            try:
                async with self.session.get(self.urls[data_type], headers=headers) as resp:

                    if resp.status == 304:
                        logger.debug(f"Got response 304, No changes in {self.repo} ({data_type})")
                        continue

                    if resp.status == 200:
                        logger.debug(f"Got response 200, Changes detected in {self.repo} ({data_type})")
                        self.etag[data_type] = resp.headers.get("ETag")
                        data = await resp.json()

                        if data_type == "runner":
                            await process_runner(data=data)
                        elif data_type == "jobs":
                            await process_jobs(data=data)

            except Exception as e:
                logger.error(f"Error while talking to github's API: {e}")
                return False

        return True

    async def session_stop(self):
        """Close the active GitHub API session when the controller is shutting down."""
        if self.session:
            await self.session.close()
            logger.debug(f"Github API session stopped for {self.repo}")


class RepoRunners(GitHubAPI):
    """Represent and manage the set of runners assigned to a repository."""

    def __init__(self, repo, name, min_idle=1, max_total=1, image="ubuntu-latest"):
        """Initialize a repository runner pool and its runtime configuration."""
        super().__init__(repo=repo)
        self.image = image
        self.repo = repo
        self.name = name
        self.min_idle = min_idle
        self.max_total = max_total
        self.idle_count = 0
        self.total_count = 0
        self.runners = {}

    def to_dict(self):
        """Return the runner pool state in a serializable dictionary for compose generation."""
        return {
            "repo": self.repo,
            "name": self.name,
            "min_idle": self.min_idle,
            "max_total": self.max_total,
            "image": self.image,
            "idle_count": self.idle_count,
            "total_count": self.total_count,
            "etag": self.etag
        }

    async def add_runner(self, name=None):
        """Create or restore a runner when the pool needs more capacity."""
        if not name:
            for num in range(self.max_total):
                name = f"{self.name}-{num}"
                if name not in self.runners.keys():
                    runner = Runner(name=name, repo=self.repo)
                    self.runners[name] = runner
                    await self.runners[name].start()
                    break
        elif name and name in self.runners.keys():
            await self.runners[name].deploy_compose()

    async def remove_runner(self, states=["online"]):
        """Remove a runner from the pool when it matches the configured removal state."""
        to_delete = None
        for runner in self.runners.values():
            if runner.state in states:
                to_delete = runner.name
                runner.state = "removing"
                break

        if not to_delete:
            return

        logger.debug(f"Removing {to_delete}")
        if await self.runners[to_delete].deploy_compose(cmd=["down"]):
            del self.runners[to_delete]
        else:
            self.runners[to_delete].state = "online"

    async def stop(self):
        """Unregister all non-busy runners from GitHub."""
        logger.info(f"Stopping every runner for {self.repo}")
        to_kill = []
        for runner in self.runners.values():
            if runner.state != "busy":
                if await self.force_unregister(runner=runner):
                    to_kill.append(runner.name)
        if to_kill:
            return to_kill
        return False


class Runner:
    """Represent a single GitHub Actions runner deployed via docker-compose."""

    def __init__(self, name, repo):
        """Initialize a runner instance with its compose path and runtime state."""
        self.name = name
        self.repo = repo
        self.compose_file_path = f"/app/{repo}/{name}.yaml"
        self.state = "preparing"
        self.id = None

    async def start(self):
        """Deploy the runner compose stack and fail if the startup command does not succeed."""
        if not await self.deploy_compose():
            raise RuntimeError("Error while deploying runner")

    async def deploy_compose(self, cmd: list = ["up", "-d", "--force-recreate"]) -> bool:
        """Run docker-compose with the provided command arguments for this runner."""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "-f",
                self.compose_file_path,
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0 and cmd != ["down"]:
                logger.info(f"Sucessfully deployed: {self.compose_file_path}")
                return True

            elif process.returncode == 0 and cmd == ["down"]:
                logger.info(f"Sucessfully removed: {self.compose_file_path}")
                return True

            else:
                logger.error(f"Error while deploying ({self.compose_file_path}): {stderr.decode()}")
                return False

        except Exception as e:
            logger.error(f"Error while deploying ({self.compose_file_path}): {e}")
            return False


class DockerClient:
    """Watch Docker events and trigger scaling checks when runner containers die."""

    def __init__(self, poll_event, scale_down_event):
        """Initialize the Docker event watcher with the polling and scale-down signals."""
        self.client = None
        self.poll_event = poll_event
        self.scale_down_event = scale_down_event
        self.died_container = None
        self.last_die = time.time()

    async def connect(self):
        """Create and cache the Docker client when it has not been opened yet."""
        if not self.client:
            self.client = aiodocker.Docker()
            await asyncio.sleep(0.5)
            logger.debug("Docker client connected")

    async def close_connection(self):
        """Close the Docker client and release the connection when the watcher stops."""
        if self.client:
            await self.client.close()
            logger.debug("Docker client connection closed")

    async def watch_event_stream(self):
        """Subscribe to Docker container events and trigger a runner rescale check when needed."""

        await self.connect()
        event_filters = json.dumps({
            "type": ["container"],
            "event": ["die", "stop"]
        })

        subscriber = self.client.events.subscribe(filters=event_filters)
        logger.info("Event watcher started...")

        try:
            while True:
                event = await subscriber.get()

                if event is None:
                    break

                action = event.get("Action")
                actor = event.get("Actor", {})
                attributes = actor.get("Attributes", {})
                container_name = attributes.get("name", "")
                image = attributes.get("image", "")
                path_to_compose = attributes.get("com.docker.compose.project.config_files", None)

                if action == "die" and image.startswith("ghcr.io/l1p0-m/github-runner-docker") and self.scale_down_event.is_set() is False:
                    logger.info(f"Container {container_name} has died, triggering scale check...")
                    self.died_container = path_to_compose
                    self.last_die = time.time()
                    self.poll_event.set()

        except asyncio.CancelledError:
            logger.info("Event watcher stopped")

    async def get_running_containers(self, base_name, repo=None):
        """Return running containers that match the configured runner pattern for a repo."""
        try:
            await self.connect()
            containers = await self.client.containers.list()
            found = []

            for container in containers:
                name = container["Names"]

                if not name:
                    continue

                name = name[0].strip("/")

                if repo:
                    path = pathlibpath(f"/app/{repo}")
                    compose_path = pathlibpath(container["Labels"]["com.docker.compose.project.config_files"]).parent
                    if base_name in name and compose_path == path:
                        found.append(name)

                else:
                    if base_name == name:
                        return True

            if found:
                return found
            return False

        except Exception as e:
            logger.warning(f"Failed to get existing runners: {e}")

    async def remove_container(self, name):
        """Delete a running container by name when it is no longer needed."""
        await self.connect()
        if name and await self.get_running_containers(base_name=name):
            logger.debug(f"Killing container: {name}")
            container = await self.client.containers.get(name)
            await container.delete(force=True)
            return True

        return False


@dataclass(frozen=True)
class RepoRunnersData:
    """Store the repository runner pools keyed by repository name."""
    repos: Dict[str, Any] = field(default_factory=dict)


class RunnerController:
    """Coordinate runner bootstrap, health checks, scaling, and shutdown logic."""

    def __init__(self, client=None):
        """Initialize the controller, config state, and event-driven runner management."""
        self.client = client
        self.poll_event = asyncio.Event()
        self.scale_down_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.configloader = ConfigLoader(config_path="./config.toml", exit_event=self.stop_event)
        self.dockerclient = DockerClient(self.poll_event, self.scale_down_event)
        self.available_images = ["ubuntu-latest", "ubuntu-24.04", "debian-latest"]
        self.reporunners = None

    async def start(self):
        """Start the background watcher tasks and block until shutdown is requested."""
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self.stop(s))
                )

            await self.bootstrap()

            self.tasks = [
                asyncio.create_task(self.run_scale_loop(poll_event=self.poll_event)),
                asyncio.create_task(self.dockerclient.watch_event_stream()),
                asyncio.create_task(self.stop_event.wait())
            ]

            done, pending = await asyncio.wait(
                [*self.tasks], return_when=asyncio.FIRST_COMPLETED
            )

            if self.stop_event.is_set():
                logger.info("Stopping running tasks...")
                for task in pending:
                    task.cancel()

                await asyncio.gather(*pending, return_exceptions=True)

                logger.info("Stopping deployed runners...")
                stop_tasks = [runner.stop() for runner in self.reporunners.repos.values()]
                if stop_tasks:
                    self.scale_down_event.set()
                    resp = await asyncio.gather(*stop_tasks, return_exceptions=True)
                    containers_to_kill = []
                    for tasks_resp in resp:
                        if isinstance(tasks_resp, Exception):
                            logger.error(f"Error during runner stop tasks: {tasks_resp}")
                        elif isinstance(tasks_resp, list):
                            containers_to_kill.extend(tasks_resp)

                    logger.debug(f"Killing cotainers: {containers_to_kill}")
                    if containers_to_kill:
                        kill_tasks = [self.dockerclient.remove_container(name=container_name) for container_name in containers_to_kill]
                        await asyncio.gather(*kill_tasks, return_exceptions=True)

                    self.scale_down_event.clear()

        except asyncio.CancelledError:
            logger.info("cancelled")
            pass

        except Exception as e:
            logger.error(f"Error: {e}")

        finally:
            await self.dockerclient.close_connection()
            for reporunner in self.reporunners.repos.values():
                await reporunner.session_stop()

    async def stop(self, sig):
        """Handle shutdown signals by setting the stop event."""
        logger.info(f"({sig.name}) - Stopping...")
        self.stop_event.set()

    async def run_scale_loop(self, poll_event):
        """Continuously evaluate scaling needs and wait for the next polling interval."""
        while True:

            poll_time = self.config.poll_interval
            if int(time.time() - self.dockerclient.last_die) >= 300:
                poll_time = 120
                logger.debug(f"Last container died: {int(time.time() - self.dockerclient.last_die)}s ago... Polling timeout increased!")

            await self.manage_runners()
            try:
                await asyncio.wait_for(poll_event.wait(), timeout=poll_time)

            except asyncio.CancelledError:
                logger.info("Scale loop stopped")
                raise

            except asyncio.TimeoutError:
                pass

            except Exception as e:
                logger.error(f"Error: {e}")
                await asyncio.sleep(5)

            finally:
                poll_event.clear()

    async def bootstrap(self):
        """Load config, initialize repo runner pools, and create the compose files."""
        try:
            self.config = await self.configloader.load()
            if not self.config:
                raise RuntimeError("Error while reading config.toml file")

            if not self.config.configured_runners:
                return

            if not self.reporunners:
                runners = {}
                for runner in self.config.configured_runners:
                    repo_name = runner.get("repo")

                    if not repo_name:
                        logger.warning(f"Repo is not defined in runner config: {runner}")
                        continue

                    runners[repo_name] = RepoRunners(
                        min_idle=runner.get("min_idle", 1),
                        max_total=runner.get("max_total", 1),
                        name=runner.get("name", "ci-runner"),
                        repo=repo_name,
                        image=runner.get("image", None))

                self.reporunners = RepoRunnersData(repos=runners)

            for runner in self.reporunners.repos.values():
                found_names = await self.dockerclient.get_running_containers(base_name=runner.name, repo=runner.repo)
                if found_names:
                    for found_name in found_names:
                        exiting_runner = Runner(name=found_name, repo=runner.repo)
                        exiting_runner.state = "recovering"
                        runner.runners[found_name] = exiting_runner
                        logger.debug(f"Imported {found_name} to {runner.repo}")

                await runner.get_github_datas()

                old_name = runner.name
                for num in range(runner.max_total):
                    runner.name = f"{old_name}-{num}"

                    if os.path.exists(f"/app/{runner.repo}/{runner.name}.yaml"):
                        logger.info(f"Compose file already exists for runner: {runner.name}, skipping creation...")
                        runner.name = old_name
                        continue

                    image = self.config.default_image if runner.image not in self.available_images else runner.image

                    await self.create_compose(params={
                        "config": self.config,
                        "runner": runner.to_dict(),
                        "image": image
                    })
                    runner.name = old_name
                    await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Error while bootstrapping: {e}")

    async def manage_runners(self):
        """Check whether scaling or recreation is required and apply the needed changes."""
        try:
            if await self.auto_recreate():
                await asyncio.sleep(5)

            for repo in self.reporunners.repos.keys():
                await self.auto_scale(repo=repo)

        except asyncio.CancelledError:
            logger.info("Scale function stopped")
            raise

        except Exception as e:
            logger.error(f"Error while scaling runners: {e}")

    async def auto_recreate(self):
        """Recreate a runner when a tracked container has unexpectedly died."""
        try:
            died = self.dockerclient.died_container
            if died:
                died_in_repo = str(pathlibpath(died).relative_to("/app/").parent)
                died_name = str(pathlibpath(died).name).replace(".yaml", "")

                logger.info(f"Container {died} found in repo: {died_in_repo}, restarting...")
                await self.reporunners.repos[died_in_repo].add_runner(name=died_name)
                self.dockerclient.died_container = None
                return True
            return False

        except asyncio.CancelledError:
            logger.info("Recreate function stopped")

    async def auto_scale(self, repo):
        """Scale a repository's runner pool to match queued jobs and minimum idle requirements."""
        try:
            runner = self.reporunners.repos[repo]

            await runner.get_github_datas()

            current_total = len(runner.runners)
            busy_runners = sum(1 for r in runner.runners.values() if r.state == "busy")

            needed_total = busy_runners + runner.queue + runner.min_idle
            target_runner_count = min(needed_total, runner.max_total) - current_total

            if target_runner_count > 0:
                logger.info(f"Needed runners: {target_runner_count} for {repo}, Scaleing up!")
                add_tasks = []
                for num in range(target_runner_count):
                    add_tasks.append(asyncio.create_task(runner.add_runner()))
                await asyncio.gather(*add_tasks)

            elif target_runner_count < 0:
                logger.info(f"Needed runners: {target_runner_count} for {repo}, Scaleing down!")
                self.scale_down_event.set()
                for num in range(abs(target_runner_count)):
                    await runner.remove_runner()
                    await asyncio.sleep(0.5)
                self.scale_down_event.clear()
            await asyncio.sleep(0.1)
            return

        except asyncio.CancelledError:
            logger.info("Auto-Scale task stopped")
            raise

    async def create_compose(self, params=None):
        """Create the docker-compose YAML file for a runner from the Jinja template."""

        def generate_files(params):
            """Render the compose YAML template and write it to disk for a runner."""
            runner = params.get("runner", {})
            repo = runner.get("repo", None)
            runner_name = runner.get("name", None)

            env = Environment(
                loader=FileSystemLoader("./"),
                trim_blocks=True,
                lstrip_blocks=True
            )
            template = env.get_template("docker-compose.yaml.j2")
            output = template.render(params)

            if os.path.exists(f"/app/{repo}/{runner_name}.yaml"):
                return False

            try:
                os.makedirs(f"/app/{repo}", exist_ok=True)
                with open(f"/app/{repo}/{runner_name}.yaml", "x") as f:
                    f.write(output)
                    return True

            except Exception as e:
                logger.error(f"Error while creating compose file for runner: {runner_name}: {e}")
                return False

        if not os.path.exists("docker-compose.yaml.j2") or not params:
            return False

        if not params.get("runner", None) or not isinstance(params["runner"], dict) or "repo" not in params["runner"].keys():
            logger.warning(f"Repo not found in config file for runner: {params['runner']['name']}")
            return False

        try:
            return await asyncio.to_thread(generate_files, params=params)

        except Exception as e:
            logger.error(f"Error creating compose file: {e}")
            return False


@dataclass(frozen=True)
class Config:
    """Store the loaded controller configuration derived from environment variables and TOML."""
    token: str
    poll_interval: int
    configured_runners: list
    pgid: int
    puid: int
    version: str
    default_image: str
    tmpfs_enabled: bool
    tmpfs_size: int


class ConfigLoader:
    """Load and validate the controller TOML configuration and environment settings."""

    def __init__(self, config_path: str = "./config.toml", exit_event=None):
        """Initialize the config loader with the TOML file path and optional shutdown event."""
        self.config_path = config_path
        self.exit_event = exit_event

    async def load(self):
        """Load runner configuration and environment variables required for the controller."""
        if not os.environ.get('TOKEN'):
            logger.error("TOKEN needs to be configured!")
            if self.exit_event:
                self.exit_event.set()
            return False

        if not os.path.exists(self.config_path):
            logger.error("config.toml not found!")
            if self.exit_event:
                self.exit_event.set()
            return False

        with open(self.config_path, "rb") as f:
            data = tomllib.load(f)

        if "runners" not in data.keys():
            if self.exit_event:
                self.exit_event.set()
            return False

        return Config(
            token=os.environ.get('TOKEN'),
            poll_interval=int(data.get("defaults", {}).get("check_interval_seconds", 30)),
            configured_runners=data.get("runners", []),
            pgid=int(os.environ.get('PGID', 1000)),
            puid=int(os.environ.get('PUID', 1000)),
            version=os.environ.get('RUNNER_IMAGE_VERSION'),
            default_image=data.get("defaults", {}).get("image", "ubuntu-latest"),
            tmpfs_enabled=data.get("defaults", {}).get("tmpfs_enabled", False),
            tmpfs_size=int(data.get("defaults", {}).get("tmpfs_size", 0))
        )


if __name__ == '__main__':
    try:
        controller = RunnerController()
        main = controller.start()
        asyncio.run(main)

    except KeyboardInterrupt:
        logger.error("Controller stopped by user")
        exit(1)

    except asyncio.CancelledError:
        exit(1)

    except Exception as e:
        logger.error(f"Error: {e}")
        exit(1)
