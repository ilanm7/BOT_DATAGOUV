import subprocess
import sys
import time
from pathlib import Path

# Lancer le bot en arrière-plan et sauvegarder le PID
proc = subprocess.Popen(
    [sys.executable, "-m", "app.main"],
    cwd=str(Path(__file__).parent),
    stdout=open("bot.log", "a"),
    stderr=subprocess.STDOUT,
)
Path("bot.pid").write_text(str(proc.pid))
print(f"Bot démarré avec PID {proc.pid}")
# Laisser le processus s'initialiser
time.sleep(2)
print("OK")
