import subprocess
import os

cwd = "/Users/ilanmuller/Downloads/datagouv-slack-agent"
os.chdir(cwd)

python_path = os.path.join(cwd, ".venv", "bin", "python3")

with open("bot.log", "a") as log:
    proc = subprocess.Popen(
        [python_path, "-m", "app.main"],
        stdout=log,
        stderr=log,
        cwd=cwd,
    )
    print(f"Bot lancé avec PID {proc.pid}")
