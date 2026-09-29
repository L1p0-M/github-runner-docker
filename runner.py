import os
import urllib.request
import shutil
import tarfile
import subprocess
import json
from pathlib import Path as pathlibpath


def check_env():
    if not os.environ.get("TOKEN"):
        print("Error: TOKEN environment variable is not set.")
        exit(1)
    if not os.environ.get("REPO"):
        print("Error: REPO environment variable is not set.")
        exit(1)
    return True


def config_runner():
    if is_configured():
        print("Runner is already configured!")
        return True

    os.chdir("/app/github-runner")
    github_token = os.environ.get("TOKEN")
    owner, repo = os.environ.get("REPO").split("/")
    token = get_token(owner=owner, repo=repo, token=github_token)

    repo = f'https://github.com/{os.environ.get("REPO")}'

    config_cmd = [
        "/bin/bash",
        "./config.sh",
        "--url",
        repo,
        "--token",
        token,
        "--replace",
        "--ephemeral",
        "--disableupdate",
    ]
    if os.environ.get("RUNNER_NAME"):
        config_cmd.extend(["--name", os.environ.get("RUNNER_NAME")])
    try:
        print("Configuring runner...")
        subprocess.run(config_cmd, check=True)
        print("Runner configured successfully.")
        return True

    except subprocess.CalledProcessError as e:
        print(f"Configuration failed with error code: {e.returncode}")
        return False

def run_runner():
    print("Starting runner...")
    try:
        subprocess.run(["/bin/bash", "./run.sh"])
    except Exception as e:
        print(f"Error while running the Runner: {e}")
        exit(1)

def is_configured():
    os.chdir("/app/github-runner")
    return os.path.exists(".runner")

def get_token(owner, repo, token):
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runners/registration-token"

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as response:
            status_code = response.getcode()
            response_body = response.read().decode("utf-8")
            if status_code == 201:
                print(f"Token is valid until: {json.loads(response_body).get("expires_at")}")
                return json.loads(response_body).get("token")
            else:
                raise RuntimeError

    except Exception as e:
        print(f"Error getting token: {e}")


def cleanup_docker():
    try:
        subprocess.run(["docker", "system", "prune", "-af", "--volumes"], check=True)
    except Exception as e:
        print(f"Error during Docker cleanup: {e}")
        exit(1)


if __name__ == "__main__":
    try:
        try:
            subprocess.run(["docker", "--version"], check=True)
        except Exception as e:
            print(f"Docker is not installed or not running: {e}")
            exit(1)

        env_is_set = check_env()
        if env_is_set:
            print("Environment variables are set. Proceeding with the script.")
            configured = config_runner()
            if not configured:
                print("Runner configuration failed or runner was removed. Exiting.")
                exit(1)
            try:
                run_runner()
            except Exception as e:
                print(f"Error while running the Runner: {e}")
                exit(1)
            finally:
                print("Cleaning up Docker resources...")
                cleanup_docker()
                print("Cleanup complete.")

    except KeyboardInterrupt:
        print("Script interrupted by user. Exiting.")
        exit(0)

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        exit(1)

