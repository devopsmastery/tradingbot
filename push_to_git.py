import os
import sys
import subprocess
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

def run_command(command):
    """Executes a shell command and prints its output."""
    print(f"> {command}")
    result = subprocess.run(command, shell=True, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return result.returncode == 0

def push_to_git():
    print("="*50)
    print("  AUTO-PUSH TO GITHUB")
    print("="*50)

    # 1. Get the token
    git_token = os.getenv("GIT_TOKEN")
    if not git_token:
        print("Error: GIT_TOKEN not found in .env file.")
        sys.exit(1)

    # Repository info (hardcoded based on current setup, can be made dynamic)
    github_user = "devopsmastery"
    repo_name = "tradingbot"
    remote_url = f"https://{github_user}:{git_token}@github.com/{github_user}/{repo_name}.git"

    # 2. Update the origin URL with the fresh token
    print("\n[1/4] Configuring Git Remote...")
    # Using 'set-url' if origin exists, otherwise 'add'
    set_url_cmd = f"git remote set-url origin {remote_url}"
    if not run_command(set_url_cmd):
        print("Origin might not exist. Trying to add origin...")
        run_command(f"git remote add origin {remote_url}")

    # 3. Add all changed files
    print("\n[2/4] Staging changes (git add .)...")
    run_command("git add .")

    # 4. Commit changes
    print("\n[3/4] Committing changes...")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Check if there are actually changes to commit
    status_result = subprocess.run("git status --porcelain", shell=True, capture_output=True, text=True)
    if status_result.stdout.strip():
        commit_msg = f"Auto-commit: Trading Strategy updates on {timestamp}"
        run_command(f'git commit -m "{commit_msg}"')
    else:
        print("No new local file changes to commit. Proceeding to push existing commits.")

    # 5. Push to Github
    print("\n[4/4] Pushing to GitHub (origin main)...")
    success = run_command("git push -u origin main")

    if success:
        print("\n[SUCCESS] Successfully pushed code to GitHub!")
    else:
        print("\n[ERROR] Failed to push code. Check the errors above.")

if __name__ == "__main__":
    push_to_git()
