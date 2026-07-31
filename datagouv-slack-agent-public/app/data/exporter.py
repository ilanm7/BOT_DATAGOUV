"""Export des résultats de recherche en CSV ou Excel."""

import asyncio
import csv
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# [NOTE] Pas de verrou nécessaire ici : les opérations write_text/read_text sont
# synchrones et bloquent l'event loop asyncio mono-threadé. Deux coroutines ne
# peuvent donc pas écrire simultanément dans le même fichier.


def _search_path(cache_dir: str, thread_ts: str = "") -> Path:
    """Chemin du fichier de recherche : global par défaut, par thread si précisé."""
    name = f"last_search_{thread_ts}.json" if thread_ts else "last_search.json"
    return Path(cache_dir) / name


def _enrich_path(cache_dir: str, thread_ts: str = "") -> Path:
    """Chemin du fichier d'état d'enrichissement."""
    name = f"enrich_state_{thread_ts}.json" if thread_ts else "enrich_state.json"
    return Path(cache_dir) / name


def save_enrich_state(
    cache_dir: str, thread_ts: str, query: str, next_offset: int, total: int
) -> None:
    """Sauvegarde l'état de l'enrichissement par lots (thread-safe)."""
    path = _enrich_path(cache_dir, thread_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "query": query,
        "next_offset": next_offset,
        "total": total,
        "timestamp": int(time.time()),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, default=str))


def load_enrich_state(cache_dir: str, thread_ts: str = "") -> dict[str, Any] | None:
    """Charge l'état de l'enrichissement par lots (thread-safe)."""
    path = _enrich_path(cache_dir, thread_ts)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return None


def save_search_results(
    cache_dir: str, query: str, results: list[dict], metadata: dict | None = None
) -> None:
    """Sauvegarde les résultats bruts de la dernière recherche (thread-safe)."""
    thread_ts = (metadata or {}).get("thread_ts", "")
    path = _search_path(cache_dir, thread_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"query": query, "results": results}
    if metadata:
        data.update(metadata)
    path.write_text(json.dumps(data, ensure_ascii=False, default=str))


def load_search_results(cache_dir: str, thread_ts: str = "") -> dict[str, Any] | None:
    """Charge les résultats de la dernière recherche (thread-safe).

    Cherche d'abord le fichier spécifique au thread.
    [ISOLATION THREAD] Pas de fallback sur le fichier global quand un thread_ts
    est fourni, pour éviter de mélanger les données entre conversations.
    """
    path = _search_path(cache_dir, thread_ts)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    # [FALLBACK GLOBAL] Uniquement si aucun thread_ts n'est demandé
    if not thread_ts:
        path = _search_path(cache_dir, "")
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
    return None


def export_to_csv(cache_dir: str, output_path: Path | None = None, thread_ts: str = "") -> Path:
    """Exporte la dernière recherche en CSV."""

    data = load_search_results(cache_dir, thread_ts)
    if not data or not data.get("results"):
        raise ValueError("Aucun résultat à exporter. Fais d'abord une recherche.")

    results = data["results"]
    if output_path is None:
        ts = thread_ts.replace(".", "_") if thread_ts else str(int(time.time()))
        output_path = Path(cache_dir) / f"export_{ts}.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not results:
        raise ValueError("Résultats vides.")

    # [FIX HETEROGÈNE] Certains résultats peuvent avoir des clés supplémentaires
    # (ex: _history ajouté par enrichissement acheteur sur les 3 premiers seulement).
    # On prend l'union de toutes les clés pour éviter l'erreur DictWriter.
    fieldnames = list(dict.fromkeys(k for r in results for k in r))
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    logger.info("CSV exporté: %s (%d lignes)", output_path, len(results))
    return output_path


def export_to_excel(cache_dir: str, output_path: Path | None = None, thread_ts: str = "") -> Path:
    """Exporte la dernière recherche en Excel."""
    import pandas as pd

    data = load_search_results(cache_dir, thread_ts)
    if not data or not data.get("results"):
        raise ValueError("Aucun résultat à exporter. Fais d'abord une recherche.")

    results = data["results"]
    if output_path is None:
        ts = thread_ts.replace(".", "_") if thread_ts else str(int(time.time()))
        output_path = Path(cache_dir) / f"export_{ts}.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(results)
    df.to_excel(output_path, index=False, engine="openpyxl")

    logger.info("Excel exporté: %s (%d lignes)", output_path, len(results))
    return output_path


TEL_PATTERN = re.compile(r"(?:\+33\s?[1-9]|0\s?[1-9])(?:[\s.\-]?\d{2}){4}")
# Numéro sans séparateur (10 chiffres commençant par 0)
TEL_PATTERN_RAW = re.compile(r"\b0[1-9]\d{8}\b")
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# Domaines à rejeter (faux positifs gouvernementaux étrangers, etc.)
EMAIL_BAD_DOMAINS = {
    "gov.qa", "gov.uk", "gov.au", "gov.ca", "gov.in", "gov.br",
    "edu", "ac.uk", "ac.in", "mil", "gob",
}


def _is_bad_email(email: str) -> bool:
    """Filtre les emails évidents faux positifs."""
    domain = email.split("@")[-1].lower()
    return any(domain.endswith(bad) for bad in EMAIL_BAD_DOMAINS)


def _normalise_phone(raw: str) -> str:
    """Normalise un numéro de téléphone français (supprime espaces, points, tirets)."""
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+33"):
        digits = "0" + digits[3:]
    return digits


def _extract_contacts(text: str) -> dict[str, str]:
    """Extrait téléphone et email d'un texte brut."""
    phones = TEL_PATTERN.findall(text)
    phones += TEL_PATTERN_RAW.findall(text)
    emails = EMAIL_PATTERN.findall(text)
    # Filtrer les faux emails
    emails = [e for e in emails if not _is_bad_email(e)]
    return {
        "telephone": _normalise_phone(phones[0]) if phones else "",
        "email": emails[0] if emails else "",
        "infos_brutes": text[:1200] if text else "",
    }


async def _search_contacts_duckduckgo(nom: str, ville: str, siret: str = "") -> dict[str, str]:
    """Cherche téléphone et email via DuckDuckGo avec requêtes ciblées."""
    try:
        from ddgs import DDGS

        # Requête 1 : Pages Jaunes (source la plus fiable pour téléphone en France)
        query = f'site:pagesjaunes.fr "{nom}" {ville}'
        all_text = ""

        def _search(q: str):
            with DDGS() as ddgs:
                return list(ddgs.text(q, max_results=5))

        results = await asyncio.wait_for(asyncio.to_thread(_search, query), timeout=12)
        for r in results:
            all_text += f"{r.get('title', '')}\n{r.get('href', '')}\n{r.get('body', '')}\n"

        contacts = _extract_contacts(all_text)

        # Si rien trouvé, essayer avec le SIRET (très précis)
        if not contacts["telephone"] and not contacts["email"] and siret:
            query2 = f'"{siret}" téléphone email'
            results2 = await asyncio.wait_for(asyncio.to_thread(_search, query2), timeout=12)
            for r in results2:
                all_text += f"{r.get('title', '')}\n{r.get('href', '')}\n{r.get('body', '')}\n"
            contacts = _extract_contacts(all_text)

        # Si toujours rien, requête générale plus large
        if not contacts["telephone"] and not contacts["email"]:
            query3 = f'"{nom}" {ville} téléphone OR email OR contact'
            results3 = await asyncio.wait_for(asyncio.to_thread(_search, query3), timeout=12)
            for r in results3:
                all_text += f"{r.get('title', '')}\n{r.get('href', '')}\n{r.get('body', '')}\n"
            contacts = _extract_contacts(all_text)

        return contacts
    except Exception:
        logger.warning("Recherche DuckDuckGo échouée pour %s %s", nom, ville, exc_info=False)
        return {"telephone": "", "email": "", "infos_brutes": ""}


async def enrich_results_with_tavily(
    results: list[dict],
    api_key: str,
) -> list[dict]:
    """Enrichit chaque entreprise avec téléphone, email via Tavily + DuckDuckGo."""
    from tavily import TavilyClient

    use_tavily = bool(api_key)
    client = TavilyClient(api_key=api_key) if use_tavily else None
    enriched = []

    for idx, r in enumerate(results):
        nom = r.get("nom", "")
        ville = r.get("ville", "")

        contacts = {"telephone": "", "email": "", "infos_brutes": ""}

        # --- Source 1: Tavily ---
        if use_tavily:
            siret = r.get("siret", "")
            if siret:
                query = f"{nom} {ville} SIRET {siret} téléphone email contact"
            else:
                query = f"{nom} {ville} téléphone email dirigeant contact"
            try:
                if idx > 0:
                    await asyncio.sleep(0.3)
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.search, query, max_results=3, include_answer=True
                    ),
                    timeout=20,
                )
                answer = response.get("answer", "")
                sources = "\n".join(
                    f"{s.get('title', '')}: {s.get('content', '')[:300]}"
                    for s in response.get("results", [])
                )
                combined = f"{answer}\n{sources}"
                contacts = _extract_contacts(combined)
            except Exception:
                logger.warning("Enrichissement Tavily échoué pour %s", nom, exc_info=False)

        # --- Source 2: DuckDuckGo (fallback gratuit) ---
        siret = r.get("siret", "")
        if not contacts["telephone"] or not contacts["email"]:
            try:
                ddgs = await _search_contacts_duckduckgo(nom, ville, siret)
                if not contacts["telephone"] and ddgs["telephone"]:
                    contacts["telephone"] = ddgs["telephone"]
                if not contacts["email"] and ddgs["email"]:
                    contacts["email"] = ddgs["email"]
                if ddgs["infos_brutes"]:
                    contacts["infos_brutes"] = (
                        f"[Tavily] {contacts['infos_brutes']}\n[DuckDuckGo] {ddgs['infos_brutes']}"
                    )
            except Exception:
                logger.warning("Recherche contacts DuckDuckGo échouée pour %s", nom, exc_info=False)

        enriched.append({
            **r,
            "telephone": contacts["telephone"],
            "email": contacts["email"],
            "données_complémentaires": contacts["infos_brutes"],
        })

    return enriched
