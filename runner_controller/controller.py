import signal
from sys import exit
import aiodocker
import aiohttp
import aiohttp.web
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
from prometheus_client import Gauge
from prometheus_client.aiohttp import make_aiohttp_handler


log_level_env = os.environ.get('DEBUG', 'INFO').upper()
log_level = getattr(logging, log_level_env, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("RunnerController")
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

class Webserver:
    def __init__(self):
        self.app = aiohttp.web.Application()
        self.app.router.add_get("/metrics", make_aiohttp_handler())

    async def run(self):
        server = aiohttp.web.AppRunner(self.app)
        await server.setup()
        site = aiohttp.web.TCPSite(server, '0.0.0.0', 8080)

        try:
            await site.start()
            await asyncio.Event().wait()

        except asyncio.CancelledError:
            logger.info("Metrics server stopped")

        finally:
            await server.cleanup()

class GitHubAPI:
    """Manage GitHub Actions API access for a repository and track runner queue state."""

    def __init__(self, repo):
        """Initialize the GitHub API session, repository metadata, and endpoint URLs."""
        self.etag = {}
        self.job_etags = {}
        self.jobs_cached = {}
        self.queue = 0
        self.repo = repo
        self.runners = {}
        self.idle_count = 0
        self.total_count = 0
        self.job_queue_metric = Gauge(name=f"jobs_in_queue", documentation="Count of queued jobs in the Repo", namespace="runner_controller", subsystem=f"{repo.replace('/', '_')}")
        self.total_runners_metric = Gauge(name=f"total_runner_count", documentation="Count of runners in the Repo", namespace="runner_controller", subsystem=f"{repo.replace('/', '_')}")
        self.idle_count_metric = Gauge(name=f"idle_runner_count", documentation="Count of idle runners in the Repo", namespace="runner_controller", subsystem=f"{repo.replace('/', '_')}")
        self.session = aiohttp.ClientSession()

        self.urls = {
            "runner": f"https://api.github.com/repos/{self.repo}/actions/runners",
            "jobs": f"https://api.github.com/repos/{self.repo}/actions/runs?status=queued",
            "registration": f"https://api.github.com/repos/{self.repo}/actions/runners/registration-token",
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

    async def get_token(self):
        """Request a GitHub registration token for the target repository."""
        try:
            async with self.session.post(url=self.urls.get("registration"), headers=self.base_headers) as resp:
                    response_body = await resp.json()
                    if resp.status == 201:
                        logger.debug(f'Token is valid until: {response_body.get("expires_at")}')
                        return response_body.get("token")
                    else:
                        logger.error(f"Unable to get registration token for repo: {self.repo}")
                        return False

        except Exception as e:
            logger.error(f"Unable to get registration token for repo: {self.repo}: {e}")

    async def get_github_datas(self):
        """Fetch runner and queued-job data from GitHub and update the cached state."""
        try:
            tasks = [
                asyncio.create_task(self.update_queue()),
                asyncio.create_task(self.update_runners())
            ]
            sucess = await asyncio.gather(*tasks, return_exceptions=True)
            for resp_codes in sucess:
                if isinstance(resp_codes, Exception):
                    logger.error(f"API Call returned with error: {resp_codes}")
                elif isinstance(resp_codes, bool) and resp_codes:
                    logger.debug("API returned with sucess code")
            return True

        except Exception as e:
            logger.error(f"Error while talking to github's API: {e}")
            return False

    async def update_runners(self):
        headers = self.base_headers.copy()
        if "runner" in self.etag:
            headers["If-None-Match"] = self.etag["runner"]
        try:
            async with self.session.get(self.urls["runner"], headers=headers) as resp:
                if resp.status == 304:
                    logger.debug(f"Got response 304, No changes for runners in {self.repo}")
                    return True
        
                if resp.status == 200:
                    logger.debug(f"Got response 200, Changes detected for runners in {self.repo}")
                    self.etag["runner"] = resp.headers.get("ETag")
                    logger.debug(f"Saved ETag for runners: {self.etag["runner"]}")
                    data = await resp.json()
                    if data:
                        await self.process_runner(data=data)

        except Exception as e:
            logger.error("Error while getting/processing runners data")
            raise RuntimeError(e)

    async def update_queue(self):
        headers = self.base_headers.copy()
        if "jobs" in self.etag:
            headers["If-None-Match"] = self.etag["jobs"]
        try:
            async with self.session.get(self.urls["jobs"], headers=headers) as resp:
                if resp.status == 304:
                    logger.debug(f"Got response 304, No changes for jobs in {self.repo}")
                    return True
        
                if resp.status == 200:
                    logger.debug(f"Got response 200, Changes detected for jobs in {self.repo}")
                    self.etag["jobs"] = resp.headers.get("ETag")
                    logger.debug(f"Saved ETag for jobs: {self.etag["jobs"]}")
                    data = await resp.json()
                    if data:
                        self.job_etags.clear() 
                        self.jobs_cached.clear()
                        success = await self.process_jobs(data=data)
                    return success

        except Exception as e:
            logger.error("Error while getting/processing jobs data")
            raise RuntimeError(e)

    async def process_runner(self, data):
        """Update tracked runner status and IDs from the GitHub runners payload."""
        runners = data.get("runners", [])
        logger.debug(runners)

        if not runners:
            self.idle_count_metric.set(0)
            self.total_runners_metric.set(0)
            return False
                    
        self.idle_count = sum(1 for r in runners if r["busy"] is False and r["status"] == "online")
        self.total_count = len(runners)
        self.idle_count_metric.set(self.idle_count)
        self.total_runners_metric.set(self.total_count)
                    
        for runner in runners:
            deployed_runners = self.runners
            if runner["name"] in deployed_runners.keys():
                deployed_runners[f"{runner['name']}"].state = runner["status"] if runner["busy"] is False else "busy"
                deployed_runners[f"{runner['name']}"].id = runner["id"]
                logger.debug(f"{runner["name"]} updated with state: {runner["status"] if runner["busy"] is False else "busy"}")

        logger.debug(f"Updated data from API: idle: {self.idle_count}, total: {self.total_count}")
        return True

    async def process_jobs(self, data):
        """Count queued workflow jobs returned in the GitHub run payload."""
        try:
            runs = data.get("workflow_runs", [])
            logger.debug(f"API respons for queued jobs: {data}")
            total_queued_jobs = 0

            if int(data.get("total_count", 0)) == 0:
                self.queue = 0
                self.jobs_cached.clear()
                self.job_queue_metric.set(self.queue)
                return True

            for run in runs:
                jobs_url = run.get("jobs_url")
                headers = self.base_headers.copy()

                if jobs_url in self.job_etags:
                    headers["If-None-Match"] = self.job_etags[jobs_url]

                try:
                    async with self.session.get(jobs_url, headers=headers) as j_resp:
                        if j_resp.status == 304:
                            logger.debug(f"Got response 304, No changes in {self.repo}")
                            if jobs_url in self.jobs_cached:
                                total_queued_jobs += self.jobs_cached[jobs_url]
                            continue

                        if j_resp.status == 200:
                            self.job_etags[jobs_url] = j_resp.headers.get("ETag")
                            j_data = await j_resp.json()
                            logger.debug(f"Github returned data for jobs: {j_data.get("jobs", [])}")
                            queued_jobs = [j for j in j_data.get("jobs", []) if j["status"] == "queued"]
                            self.jobs_cached[jobs_url] = len(queued_jobs)
                            total_queued_jobs += len(queued_jobs)

                except Exception as e:
                    logger.error(f"Failed to process queued jobs for {self.repo}: {e}")
                    raise RuntimeError(e)

            logger.info(f"Remaining jobs: {total_queued_jobs}")
            self.queue = total_queued_jobs
            self.job_queue_metric.set(self.queue)
            return True

        except RuntimeError as e:
            raise RuntimeError(e)

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
                    return await self.runners[name].start()
                return False

        elif name and name in self.runners.keys():
            success = await self.runners[name].deploy_compose()
            if not success:
                self.runners.pop(name, None)
                return False
            return True

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
            logger.error(f"Failed to deploy container for {self.name} in {self.repo}")
            return False
        return True

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
        self.died_container = set()
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
                    self.died_container.add(path_to_compose)
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
                    if not base_name in name:
                        continue

                    compose_path = pathlibpath(container["Labels"]["com.docker.compose.project.config_files"]).parent
                    if compose_path == path:
                        found.append(name)

                else:
                    if base_name == name:
                        return True

            if found:
                return found
            return False

        except Exception as e:
            logger.warning(f"Failed to get existing runners: {e}")
            return False

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
        self.metrics = Webserver()
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
                asyncio.create_task(self.metrics.run()),
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

                if self.reporunners is None:
                    logger.info("No runners to stop, exiting...")
                    exit(1)

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

                    logger.debug(f"Killing containers: {containers_to_kill}")
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
            if self.reporunners is not None:
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

                image = self.config.default_image if runner.image not in self.available_images else runner.image
                logger.info(f"Using runner image: {image} for repo: {runner.repo}")

                for num in range(runner.max_total):
                    compose_name = f"{runner.name}-{num}"

                    if os.path.exists(f"/app/{runner.repo}/{compose_name}.yaml"):
                        logger.info(f"Compose file already exists for runner: {compose_name},  Recreating it...")

                    await self.create_compose(params={
                        "config": self.config,
                        "runner": runner.to_dict(),
                        "image": image,
                        "name": compose_name
                    })
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
        recreate_tasks, data = [], []
        while self.dockerclient.died_container:
            died = self.dockerclient.died_container.pop()
            if not died:
                continue

            try:
                died_in_repo = str(pathlibpath(died).relative_to("/app/").parent)
                died_name = str(pathlibpath(died).name).replace(".yaml", "")
                logger.info(f"Container {died} found in repo: {died_in_repo}, restarting...")
                task = self.reporunners.repos[died_in_repo].add_runner(name=died_name)
            except (TypeError, ValueError, KeyError):
                continue

            recreate_tasks.append(task)
            data.append((died_in_repo, died_name))

        if not recreate_tasks:
            return False

        success = await asyncio.gather(*recreate_tasks)
        return_value = False
        for (repo, name), response in zip(data, success):
            if response:
                logger.debug("Container recreated successfuly")
                return_value = True
                continue
            logger.warning(f"Failed to recreate {name} in {repo}: {response}")
        return return_value

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

            elif target_runner_count < 0 and not self.scale_down_event.is_set():
                allowed_min_total = busy_runners + runner.min_idle
                if current_total + target_runner_count < allowed_min_total:
                    target_runner_count = allowed_min_total - current_total

                if target_runner_count < 0:
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
            runner_name = params.get("name", None)

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
