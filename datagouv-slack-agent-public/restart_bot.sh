#!/bin/bash
# Relance le bot proprement en arrière-plan

pkill -f "python -m app.main"
sleep 1

cd ~/Downloads/datagouv-slack-agent || exit 1
source .venv/bin/activate
nohup python -m app.main > bot.log 2>&1 &

echo "✅ Bot relancé (PID $!) — logs dans bot.log"
