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
            return True

def config_runner():
    token = os.environ.get("TOKEN")
    repo = f"https://github.com/{os.environ.get("REPO")}"

    if os.environ.get("RUNNER_NAME", False):
        runner_name = os.environ.get("RUNNER_NAME")
        config_command = f"/bin/bash ./config.sh --url {repo} --token {token} --name {runner_name} --replace"
    else:
        config_command = f"/bin/bash ./config.sh --url {repo} --token {token} --replace"

    print("Configuring runner...")
    subprocess.run(config_command, shell=True)
    print("Runner configured successfully.")

    if os.environ.get("REMOVE_RUNNER", "false") == "true":
        print("Removing runner...")
        config_command = f"/bin/bash ./config.sh remove --token {token}"
        subprocess.run(config_command, shell=True)
        print("Runner removed successfully.")
        return False
    
    return True

def run_runner():
    print("Starting runner...")
    subprocess.run("/bin/bash ./run.sh", shell=True)

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

