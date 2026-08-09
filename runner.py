import os
import urllib.request
import shutil
import tarfile
import subprocess


def check_env():
    if not os.environ.get("TOKEN"):
        print("Error: TOKEN environment variable is not set.")
        exit(1)
    if not os.environ.get("REPO"):
        print("Error: REPO environment variable is not set.")
        exit(1)
    return True


def install_runner():
    if not os.path.exists("github-runner"):
        os.makedirs("github-runner")
    os.chdir("github-runner")

    if os.path.exists("config.sh"):
        print("Runner already extracted.")
        return True
    
    url = "https://github.com/actions/runner/releases/download/v2.336.0/actions-runner-linux-x64-2.336.0.tar.gz"
    output_file = "actions-runner-linux.tar.gz"
    print("Downloading runner...")
    with urllib.request.urlopen(url) as response, open(output_file, 'wb') as out_file:
        shutil.copyfileobj(response, out_file)
    print("Download complete!")
    if os.path.exists(output_file):
        print(f"Extracting {output_file}...")
        with tarfile.open(output_file, "r:gz") as tar:
            tar.extractall(path=".", filter='data')
        if os.path.exists("config.sh"):
            print("Runner extracted successfully.")
            os.remove(output_file)
            return True

def config_runner():
    if is_configured():
        print("Runner is already configured!")
        return True
    
    token = os.environ.get("TOKEN")
    repo = f'https://github.com/{os.environ.get("REPO")}'

    config_cmd = [
        "/bin/bash",
        "./config.sh",
        "--url",
        repo,
        "--token",
        token,
        "--replace",
    ]
    if os.environ.get("RUNNER_NAME"):
        config_cmd.extend(["--name", os.environ.get("RUNNER_NAME")])
    try:
        print("Configuring runner...")
        subprocess.run(config_cmd, check=True)
        print("Runner configured successfully.")

    except subprocess.CalledProcessError as e:
        print(f"Configuration failed with error code: {e.returncode}")
        return False

    if os.environ.get("REMOVE_RUNNER", "false").lower() == "true":
        print("Removing runner...")
        remove_cmd = [
            "/bin/bash",
            "./config.sh",
            "remove",
            "--token",
            token,
        ]
        try:
            subprocess.run(remove_cmd, check=True)
            print("Runner removed successfully.")
            return False
        except subprocess.CalledProcessError as e:
            print(f"Removing runner failed with error code: {e.returncode}")
    return True

def run_runner():
    print("Starting runner...")
    try:
        subprocess.run(["/bin/bash", "./run.sh"])
    except Exception as e:
        print(f"Error while running the Runner: {e}")
        exit(1)

def is_configured():
    return os.path.exists(".runner")

if __name__ == "__main__":
    try:
        env_is_set = check_env()
        if env_is_set:
            print("Environment variables are set. Proceeding with the script.")
            installed = install_runner()
            if installed:
                configured = config_runner()
                if not configured:
                    print("Runner configuration failed or runner was removed. Exiting.")
                    exit(1)
                run_runner()

    except KeyboardInterrupt:
        print("Script interrupted by user. Exiting.")
        exit(0)

