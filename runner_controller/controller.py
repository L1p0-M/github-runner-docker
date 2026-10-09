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


log_level_env = os.environ.get('DEBUG', 'INFO').upper()
log_level = getattr(logging, log_level_env, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("RunnerController")


class GitHubAPI:
    def __init__(self, repo):
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

    async def force_unregister_and_kill(self, runner):
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

        async def process_jobs(data):
            runs = data.get("workflow_runs", [])
            total_queued_jobs = 0
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


class RepoRunners(GitHubAPI):
    def __init__(self, repo, name, min_idle=1, max_total=1, image="ubuntu-latest", client=None):
        super().__init__(repo=repo)
        self.client = client
        self.image = image
        self.repo = repo
        self.name = name
        self.min_idle = min_idle
        self.max_total = max_total
        self.idle_count = 0
        self.total_count = 0
        self.runners = {}

    def to_dict(self):
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
        logger.info(f"Stopping every runner for {self.repo}")
        for runner in self.runners.values():
            if await self.force_unregister_and_kill(runner=runner):
                container = await self.client.containers.get(runner.name)
                await container.delete(force=True)
        await self.session.close()
        return True


class Runner:
    def __init__(self, name, repo):
        self.name = name
        self.repo = repo
        self.compose_file_path = f"/app/{repo}/{name}.yaml"
        self.state = "preparing"
        self.id = None

    async def start(self):
        if not await self.deploy_compose():
            raise RuntimeError("Error while deploying runner")

    async def deploy_compose(self, cmd: list = ["up", "-d", "--force-recreate"]) -> bool:
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


class EventWatcher:
    def __init__(self, client, poll_event, scale_down_event):
        self.client = client
        self.poll_event = poll_event
        self.scale_down_event = scale_down_event
        self.died_container = None
        self.last_die = time.time()

    async def watch_event_stream(self):

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

    async def get_running_containers(self, base_name, repo):
        try:
            containers = await self.client.containers.list()
            found = []

            for container in containers:
                name = container.get("Names", [])
                if not name:
                    continue
                name = name[0].strip("/")

                path = pathlibpath(f"/app/{repo}")
                compose_path = pathlibpath(container.get("Labels", {}).get("com.docker.compose.project.config_files")).parent

                if base_name in name and compose_path == path:
                    found.append(name)
            return found

        except Exception as e:
            logger.warning(f"Failed to get existing runners: {e}")


class RunnerController:
    def __init__(self, client=None):
        self.client = client
        self.poll_event = asyncio.Event()
        self.scale_down_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.config = {}
        self.event_watcher = EventWatcher(self.client, self.poll_event, self.scale_down_event)
        self.matrix = {}
        self.available_images = ["ubuntu-latest", "ubuntu-24.04", "debian-latest"]
        self.check_interval = 30  # seconds

    async def start(self):
        try:
            if not self.client:
                self.client = aiodocker.Docker()
                self.event_watcher.client = self.client

                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(
                        sig,
                        lambda s=sig: asyncio.create_task(self.stop(s))
                    )

                await self.bootstrap()
                self.tasks = [
                    asyncio.create_task(self.run_scale_loop(poll_event=self.poll_event, poll_interval=self.check_interval)),
                    asyncio.create_task(self.event_watcher.watch_event_stream()),
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
                    stop_tasks = [runner.stop() for runner in self.matrix.values()]
                    if stop_tasks:
                        self.scale_down_event.set()
                        await asyncio.gather(*stop_tasks, return_exceptions=True)
                        self.scale_down_event.clear()

        except asyncio.CancelledError:
            logger.info("cancelled")
            pass

        except Exception as e:
            logger.error(f"Error: {e}")

        finally:
            if self.client:
                await self.client.close()
                logger.info("Docker socket connection closed")

    async def stop(self, sig):
        logger.info(f"({sig.name}) - Stopping...")
        self.stop_event.set()

    async def run_scale_loop(self, poll_event, poll_interval=30):
        while True:

            poll_time = poll_interval
            if int(time.time() - self.event_watcher.last_die) >= 300:
                poll_time = 120
                logger.debug(f"Last container died: {int(time.time() - self.event_watcher.last_die)}s ago... Polling timeout increased!")

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
        try:
            sucess = await self.get_runner_vars()
            if not sucess:
                raise RuntimeError("Error while reading config.toml file")

            if "defaults" in self.config.keys():
                self.check_interval = self.config["defaults"].get("check_interval_seconds", 30)

            if "runners" in self.config.keys():
                for runner in self.config["runners"]:
                    repo_name = runner.get("repo")

                    if not repo_name:
                        logger.warning(f"Repo is not defined in runner config: {runner}")
                        continue

                    if repo_name not in self.matrix.keys():
                        self.matrix[repo_name] = RepoRunners(
                            min_idle=runner.get("min_idle", 1),
                            max_total=runner.get("max_total", 1),
                            name=runner.get("name", "ci-runner"),
                            repo=repo_name,
                            image=runner.get("image", None),
                            client=self.client)

                for repo in self.matrix.keys():
                    runner = self.matrix.get(repo, None)

                    found_names = await self.event_watcher.get_running_containers(base_name=runner.name, repo=runner.repo)
                    if found_names != []:
                        for found_name in found_names:
                            exiting_runner = Runner(name=found_name, repo=repo)
                            exiting_runner.state = "recovering"
                            runner.runners[found_name] = exiting_runner

                    await runner.get_github_datas()

                    old_name = runner.name
                    for num in range(runner.max_total):
                        runner.name = f"{old_name}-{num}"

                        if os.path.exists(f"/app/{repo}/{runner.name}.yaml"):
                            logger.info(f"Compose file already exists for runner: {runner.name}, skipping creation...")
                            runner.name = old_name
                            continue

                        image = self.config.get("defaults", {}).get("image", "ubuntu-latest") if runner.image not in self.available_images else runner.image

                        await self.create_compose(params={
                            "defaults": self.config.get("defaults", {}),
                            "runner": runner.to_dict(),
                            "config": self.config.get("config", {}),
                            "image": image
                        })
                        runner.name = old_name
                        await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Error while bootstrapping: {e}")

    async def get_runner_vars(self) -> bool:

        if not os.environ.get('TOKEN'):
            raise RuntimeError("TOKEN needs to be configured!")
        if not os.path.exists("./config.toml"):
            raise RuntimeError("config.toml not found!")

        env_var_data = {
            "pgid": os.environ.get('PGID', 1000),
            "puid": os.environ.get('PUID', 1000),
            "token": os.environ.get('TOKEN'),
            "version": os.environ.get('RUNNER_IMAGE_VERSION')
        }

        with open("./config.toml", "rb") as f:
            data = tomllib.load(f)

        data["config"] = env_var_data

        self.config = data

        if self.config == data:
            return True
        return False

    async def manage_runners(self):
        try:
            repo_name = list(self.matrix.keys())
            if await self.auto_recreate():
                await asyncio.sleep(5)

            for repo in repo_name:
                await self.auto_scale(repo=repo)

        except asyncio.CancelledError:
            logger.info("Scale function stopped")
            raise

        except Exception as e:
            logger.error(f"Error while scaling runners: {e}")

    async def auto_recreate(self):
        try:
            died = self.event_watcher.died_container
            if died:
                died_in_repo = str(pathlibpath(died).relative_to("/app/").parent)
                died_name = str(pathlibpath(died).name).replace(".yaml", "")

                logger.info(f"Container {died} found in repo: {died_in_repo}, restarting...")
                await self.matrix[died_in_repo].add_runner(name=died_name)
                self.event_watcher.died_container = None
                return True
            return False

        except asyncio.CancelledError:
            logger.info("Recreate function stopped")

    async def auto_scale(self, repo):
        try:
            runner = self.matrix[repo]

            await runner.get_github_datas()

            current_total = len(runner.runners)
            busy_runners = sum(1 for r in runner.runners.values() if r.state == "busy")

            needed_total = busy_runners + runner.queue + runner.min_idle
            target_runner_count = min(needed_total, runner.max_total) - current_total

            if target_runner_count > 0:
                logger.info(f"Needed runners: {target_runner_count} for {repo}, Scaleing up!")
                for num in range(target_runner_count):
                    await runner.add_runner()
                    await asyncio.sleep(1)

            elif target_runner_count < 0:
                logger.info(f"Needed runners: {target_runner_count} for {repo}, Scaleing down!")
                self.scale_down_event.set()
                for num in range(abs(target_runner_count)):
                    await runner.remove_runner()
                    await asyncio.sleep(1)
                self.scale_down_event.clear()
            await asyncio.sleep(0.1)
            return

        except asyncio.CancelledError:
            logger.info("Auto-Scale task stopped")
            raise

    async def create_compose(self, params=None):

        def generate_files(params):
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
