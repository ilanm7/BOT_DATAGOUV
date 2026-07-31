"""Gestion des fichiers uploadés sur Slack."""

import logging
import os
import tempfile
from pathlib import Path

import httpx

from app.data.analyzer import AnalysisError, analyze_file

logger = logging.getLogger(__name__)
MAX_UPLOAD_MB = 50


async def download_slack_file(url: str, bot_token: str) -> Path:
    """Télécharge un fichier privé Slack et retourne son chemin temporaire."""
    headers = {"Authorization": f"Bearer {bot_token}", "User-Agent": "datagouv-slack-agent/0.1.0"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=60)) as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        # Vérifier la taille avant d'écrire
        content = response.content
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            raise AnalysisError(
                f"Fichier trop volumineux ({len(content) / 1e6:.0f} Mo, "
                f"limite {MAX_UPLOAD_MB} Mo)."
            )

        suffix = Path(url).suffix or ".bin"
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return Path(path)


async def analyze_slack_files(files: list[dict], bot_token: str) -> str:
    """Télécharge et analyse une liste de fichiers Slack, retourne un résumé textuel."""
    summaries = []
    for file_info in files:
        url = file_info.get("url_private_download")
        if not url:
            continue
        name = file_info.get("name", "fichier")
        path: Path | None = None
        try:
            path = await download_slack_file(url, bot_token)
            # [FICHIER VIDE] Vérifier que le fichier contient des données
            if path.stat().st_size == 0:
                raise AnalysisError("Fichier vide (0 octet).")
            result = analyze_file(path)
            cols = result.get("columns", [])
            rows = result.get("rows_scanned", 0)
            safe_name = name.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            if not cols:
                summaries.append(
                    f"*{safe_name}* : fichier lu mais aucune colonne détectée (format inconnu ?)"
                )
            else:
                summaries.append(
                    f"*{safe_name}* : {len(cols)} colonnes, {rows} ligne(s) scannée(s)"
                )
        except AnalysisError as exc:
            safe_name = name.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            summaries.append(f"*{safe_name}* : {exc}")
        except Exception:
            logger.exception("Erreur lors de l'analyse de %s", name)
            safe_name = name.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            summaries.append(
                f"*{safe_name}* : erreur inattendue (format non supporté ou fichier corrompu)"
            )
        finally:
            if path and path.exists():
                path.unlink(missing_ok=True)
    return "\n\n".join(summaries) if summaries else ""
