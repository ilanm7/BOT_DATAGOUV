#!/bin/bash
# Déploie le code modifié vers ~/dev/datagouv-slack-agent/ et relance le bot

DEV_DIR="$HOME/dev/datagouv-slack-agent"
CURRENT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🔄 Copie du code vers $DEV_DIR..."
cp -r "$CURRENT_DIR/app" "$DEV_DIR/"
cp "$CURRENT_DIR/pyproject.toml" "$DEV_DIR/"

echo "🚀 Redémarrage du bot..."
cd "$DEV_DIR" || exit 1
bash restart_bot.sh