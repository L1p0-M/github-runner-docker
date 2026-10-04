import signal
import aiodocker
import aiohttp
import os
import asyncio
from jinja2 import Template, Environment, FileSystemLoader
import json
import tomllib
from pathlib import Path as pathlibpath



class RepoRunners:
    def __init__(self, repo, name, min_idle=1, max_total=1, image="ghcr.io/l1p0-m/github-runner-docker:latest"):
        self.image = image
        self.repo = repo
        self.name = name
        self.min_idle = min_idle
        self.max_total = max_total
        self.idle_count = 0
        self.total_count = 0
        self.etag = None
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

    async def remove_runner(self, name=None):
        to_delete = name
        if not name:
            for runner in self.runners.values():
                if runner.state == "online" or runner.state == "preparing" or runner.state == "offline":
                    to_delete = runner.name
                    break

        if not to_delete:
            return

        print(f"[INFO] Removing {to_delete}")
        await self.runners[to_delete].deploy_compose(cmd=["down"])
        del self.runners[to_delete]

    async def stop(self):
        print(f"[INFO] Stopping every runner for {self.repo}")
        while self.runners != {}:
            await self.remove_runner()
        return True


class Runner:
    def __init__(self, name, repo):
        self.name = name
        self.repo = repo
        self.compose_file_path = f"/app/{repo}/{name}.yaml"
        self.state = "preparing"

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
                print(f"[INFO] Sucessfully deployed: {self.compose_file_path}")
                return True
        
                    
            elif process.returncode == 0 and cmd == ["down"]:
                print(f"[INFO] Sucessfully removed: {self.compose_file_path}")
                return True

            else:
                print(f"Error while deploying ({self.compose_file_path}): {stderr.decode()}")
                return False
        
        except Exception as e:
            print(f"Error while deploying ({self.compose_file_path}): {e}")
            return False


class EventWatcher:
    def __init__(self, client, poll_event, scale_down_event):
        self.client = client
        self.poll_event = poll_event
        self.scale_down_event = scale_down_event
        self.died_container = None

    async def watch_event_stream(self):

        event_filters = json.dumps({
                "type": ["container"],
                "event": ["die", "stop"]
            })

        subscriber = self.client.events.subscribe(filters=event_filters)
        print("[INFO] Event watcher started...")

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
                    print(f"[INFO] Container {container_name} has died, triggering scale check...")
                    self.died_container = path_to_compose
                    self.poll_event.set()

        except asyncio.CancelledError:
            print("[INFO] Event watcher stopped")


class RunnerController:
    def __init__(self, client=None):
        self.client = client
        self.poll_event = asyncio.Event()
        self.scale_down_event = asyncio.Event()
        self.config = {}
        self.event_watcher = EventWatcher(self.client, self.poll_event, self.scale_down_event)
        self.matrix = {}
        self.check_interval = 30  # seconds


    async def start(self):
        try:
            if not self.client:
                self.client = aiodocker.Docker()
                self.event_watcher.client = self.client

                await self.bootstrap()
                self.tasks = [
                    asyncio.create_task(self.run_scale_loop(poll_event=self.poll_event, poll_interval=self.check_interval)),
                    asyncio.create_task(self.event_watcher.watch_event_stream())
                ]
                loop = asyncio.get_running_loop()
                for s in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(s, lambda s=s: asyncio.create_task(self.stop(s, self.tasks)))
            
                await asyncio.gather(*self.tasks)

        except Exception as e:
            print(f"Error: {e}")


    async def stop(self, sig, tasks):
        try:
            print(f"[INFO] ({sig.name}) - Stopping...")

            for task in tasks:
                task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)
            print("[INFO] Shutdown complete.")

            for runner in self.matrix.values():
                self.scale_down_event.set()
                if await runner.stop():
                    continue
                self.scale_down_event.clear()

        except Exception as e:
            print("Error while stopping tasks")
        finally:
            if self.client:
                await self.client.close()
                print("[INFO] Docker socket connection closed")


    async def run_scale_loop(self, poll_event, poll_interval=30):

        while True:
            await self.scale_runners()
            try:
                await asyncio.wait_for(poll_event.wait(), timeout=poll_interval)

            except asyncio.CancelledError:
                print("[INFO] Scale loop stopped")
                raise

            except asyncio.TimeoutError:
                pass

            except Exception as e:
                print(f"Error: {e}")
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
                        print(f"Error: Repo is not defined in runner config: {runner}")
                        continue
            
                    if not repo_name in self.matrix.keys():
                        self.matrix[repo_name] = RepoRunners(
                            min_idle= runner.get("min_idle", 1),
                            max_total= runner.get("max_total", 1),
                            name= runner.get("name", "ci-runner"),
                            repo= repo_name,
                            image= runner.get("image", "ghcr.io/l1p0/github-runner-docker:latest"))
            
                for repo in self.matrix.keys():
                    runner = self.matrix.get(repo, None)
                    idle, total, etag = await self.check_github_status(repo_name=str(repo), etag=runner.etag if runner else None)
                    if idle is not None and total is not None and etag:      
                        runner.etag = etag
                        runner.idle_count = idle
                        runner.total_count = total
            
                    old_name = runner.name
                    for num in range(runner.max_total):
                        runner.name = f"{old_name}-{num}"

                        if os.path.exists(f"/app/{repo}/{runner.name}.yaml"):
                            print(f"[INFO] Compose file already exists for runner: {runner.name}, skipping creation...")
                            runner.name = old_name
                            continue

                        resp = await self.create_compose(params= {
                            "defaults": self.config.get("defaults", {}),
                            "runner": runner.to_dict(),
                            "config": self.config.get("config", {})
                        })
                        runner.name = old_name
                        await asyncio.sleep(0.1)


        except Exception as e:
            print(f"Error while bootstrapping: {e}")


    async def check_github_status(self, repo_name: str, etag= None):
        try:
            headers = {
                "Authorization": f"token {os.environ.get('TOKEN')}",
                "Accept": "application/vnd.github+json"
            }
            if etag:
                headers["If-None-Match"] = etag
            url = f"https://api.github.com/repos/{repo_name}/actions/runners"
            

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:

                    if resp.status == 304:
                        return None, None, etag

                    if resp.status == 200:
                        etag = resp.headers.get("ETag")
                        data = await resp.json()

                        runners = data.get("runners", [])
                        
                        idle_count = sum(1 for r in runners if r["busy"] is False and r["status"] == "online")
                        total_count = len(runners)

                        for runner in runners:
                            deployed_runners = self.matrix[repo_name].runners
                            if runner["name"] in deployed_runners.keys():
                                deployed_runners[f"{runner['name']}"].state = runner["status"] if runner["busy"] is False else "busy"


                        return idle_count, total_count, etag
            return None, None, etag
        
        except Exception as e:
            print(f"Error while talking to github's API: {e}")
            return None, None, etag


    async def get_runner_vars(self) -> bool:

        if not os.environ.get('TOKEN'):
            raise RuntimeError("TOKEN needs to be configured!")
        if not os.path.exists("./config.toml"):
            raise RuntimeError("config.toml not found!")

        env_var_data= {
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

    
    async def scale_runners(self):
        try:
            repo_name = list(self.matrix.keys())

            died = self.event_watcher.died_container
            if died:
                died_in_repo = str(pathlibpath(died).relative_to("/app/").parent)
                died_name = str(pathlibpath(died).name).replace(".yaml", "")

                if self.matrix[died_in_repo].min_idle >= self.matrix[died_in_repo].idle_count:
                    print(f"[INFO] Container {died} found in repo: {died_in_repo}, restarting...")
                    await self.matrix[died_in_repo].add_runner(name=died_name)
                    self.event_watcher.died_container = None

                else:
                    self.scale_down_event.set()
                    await self.matrix[died_in_repo].remove_runner(name=died_name)
                    self.scale_down_event.clear()
                    self.event_watcher.died_container = None

            for repo in repo_name:
                runner = self.matrix[repo]

                idle, total, etag = await self.check_github_status(repo_name=repo, etag=runner.etag if runner else None)
                if idle is not None and total is not None and etag:      
                    runner.etag = etag
                    runner.idle_count = idle
                    runner.total_count = total

                if (len(runner.runners) < runner.min_idle and runner.idle_count < runner.min_idle) and (len(runner.runners) < runner.max_total):
                    needed_runners = runner.min_idle - runner.idle_count
                    for num in range(runner.min_idle - runner.idle_count):
                        await runner.add_runner()
                        await asyncio.sleep(1)

                elif runner.idle_count > runner.min_idle:
                    needed_runners = runner.idle_count - runner.min_idle
                    self.scale_down_event.set()
                    await runner.remove_runner()
                    self.scale_down_event.clear()
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            print("Scale function stopped")

        except Exception as e:
            print(f"Error while scaling runners: {e}")


    async def create_compose(self, params= None):

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
                print(f"Error while creating compose file for runner: {runner_name}") 
                return False

        if not os.path.exists("docker-compose.yaml.j2") and not params:
            return False
        
        if isinstance(params["runner"], dict) and not "repo" in params["runner"].keys():
            print(f"Repo not found in config file for runner: {params["runner"]["name"]}")
            return False

        try:
            return await asyncio.to_thread(generate_files, params=params)

        except Exception as e:
            print(f"Error creating compose file: {e}")
            return False


if __name__ == '__main__':
    try:
        controller = RunnerController()
        main = controller.start()
        asyncio.run(main)

    except KeyboardInterrupt:
        print("Controller stopped by user")
        exit(1)

    except asyncio.CancelledError:
        exit(1)

    except Exception as e:
        print(f"Error: {e}")
        exit(1)
