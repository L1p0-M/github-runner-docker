import os
import subprocess
import json
from pathlib import Path as pathlibpath
import signal
import asyncio
import aiohttp



async def check_env():
    if not os.environ.get("TOKEN"):
        print("Error: TOKEN environment variable is not set.")
        exit(1)
    if not os.environ.get("REPO"):
        print("Error: REPO environment variable is not set.")
        exit(1)
    return True


async def config_runner():
    if await is_configured():
        print("[INFO] Runner is already configured!")
        return True

    os.chdir("/app/github-runner")
    github_token = os.environ.get("TOKEN")
    owner, repo = os.environ.get("REPO").split("/")
    token = await get_token(owner=owner, repo=repo, token=github_token)

    repo = f'https://github.com/{os.environ.get("REPO")}'

    config_cmd = [
        "/bin/bash",
        "./config.sh",
        "--url",
        str(repo),
        "--token",
        str(token),
        "--replace",
        "--ephemeral",
        "--disableupdate",
    ]
    if os.environ.get("RUNNER_NAME"):
        config_cmd.extend(["--name", os.environ.get("RUNNER_NAME")])
    try:
        print("[INFO] Configuring runner...")
        proc = await asyncio.create_subprocess_exec(*config_cmd)
        await proc.wait()
        print("[INFO] Runner configured successfully.")
        return True

    except subprocess.CalledProcessError as e:
        print(f"Configuration failed with error code: {e.returncode}")
        return False


async def unregister_runner():
    if not await is_configured():
        return True
    print("[INFO] Unregistering Runner!")
    os.chdir("/app/github-runner")
    github_token = os.environ.get("TOKEN")
    owner, repo = os.environ.get("REPO").split("/")
    token = await get_token(owner=owner, repo=repo, token=github_token, token_type="remove-token")


    config_cmd = [
        "/bin/bash",
        "./config.sh",
        "remove",
        "--token",
        str(token),
        ]
    try:
        proc = await asyncio.create_subprocess_exec(*config_cmd)
        await proc.wait()
        print("[INFO] Runner unregistered successfully.")
        return True
    
    except subprocess.CalledProcessError as e:
        print(f"Unregister failed with error code: {e.returncode}")
        return False


async def run_runner(stop_event):
    print("[INFO] Starting runner...")
    try:
        cmd = ["/bin/bash", "./run.sh"]
        proc = await asyncio.create_subprocess_exec(*cmd)

        runner_task = asyncio.create_task(proc.wait())
        stop_task = asyncio.create_task(stop_event.wait())

        done, pending = await asyncio.wait(
            [runner_task, stop_task], return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

        if stop_event.is_set() and proc.returncode is None:
            print("[INFO] Terminating run.sh process...")
            proc.terminate()
            await proc.wait()

    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            proc.terminate()
            await proc.wait()
    except Exception as e:
        print(f"Error while running the Runner: {e}")

async def is_configured():
    os.chdir("/app/github-runner")
    return os.path.exists(".runner")

async def get_token(owner, repo, token, token_type="registration-token"):
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runners/{token_type}"

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url=url, headers=headers) as response:
                response_body = await response.json()
                if response.status == 201:
                    print(f"[INFO] Token is valid until: {response_body.get("expires_at")}")
                    return response_body.get("token")
                else:
                    raise RuntimeError

    except Exception as e:
        print(f"Error getting token: {e}")

async def main():
    try:
        proc = await asyncio.create_subprocess_exec("docker", "--version")
        await proc.wait()
    except Exception as e:
        print(f"Docker is not installed or not running: {e}")
        exit(1)

    await check_env()
    print("[INFO] Environment variables are set. Proceeding with the script.")

    configured = await config_runner()
    if not configured:
        print("Runner configuration failed or runner was removed. Exiting.")
        exit(1)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def signal_handler(sig):
        print(f"\n[INFO] Received signal {sig.name}. Initiating graceful shutdown...")
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, signal_handler, s)

    runner_task = asyncio.create_task(run_runner(stop_event))
    await runner_task

    await unregister_runner()

async def stop(sig, tasks):
    print(sig.name)

    try:
        for task in tasks:
            task.cancel()

        if await unregister_runner():
            await asyncio.gather(*tasks, return_exceptions=True)
        print("[INFO] Shutdown complete.")
    
    except Exception as e:
        print("Error while stopping tasks")


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("Script interrupted by user. Exiting.")
        exit(0)

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        exit(1)

