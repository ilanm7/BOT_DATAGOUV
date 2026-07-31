#!/bin/bash
cd /Users/ilanmuller/Downloads/datagouv-slack-agent || exit 1
source .venv/bin/activate
nohup python -m app.main > bot.log 2>&1 &
echo "Bot relance avec PID"
