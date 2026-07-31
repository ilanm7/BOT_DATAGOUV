"""Tools locaux exposés au LLM, à côté des tools MCP.

Le tool `download_and_analyze_resource` est le pont entre le raisonnement LLM
et le calcul déterministe : le fichier est téléchargé et analysé côté Python,
seul un résumé compact structuré revient dans le contexte du modèle.
"""

import asyncio
import ipaddress
import json
import logging
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings
from app.data.analyzer import AnalysisError, analyze_file, query_file, summary_for_llm
from app.data.downloader import DownloadError, download_resource
from app.data.dvf import query_dvf_details, query_dvf_stats, query_dvf_trend
from app.data.energy import get_electricity_consumption_by_region
from app.data.exporter import enrich_results_with_tavily, save_enrich_state, save_search_results
from app.data.sau import get_sau_by_region
from app.data.sncf import get_sncf_punctuality
from app.data.traffic import get_traffic_summary
from app.external.france_travail import FranceTravailClient, format_job
from app.external.hubeau import HubEauClient, format_piezo
from app.external.legifrance import LegifranceClient, format_legifrance_result
from app.external.meteo import MeteoClient, format_forecast
from app.external.rnb import RNBClient, format_building

logger = logging.getLogger(__name__)


@contextmanager
def _get_pme_db(cache_dir: str):
    db_path = Path(cache_dir) / "pme_cache.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pme_check ("
            "siren TEXT PRIMARY KEY, is_pme INTEGER, checked_at INTEGER"
            ")"
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


def _is_pme_cached(cache_dir: str, siren: str) -> bool | None:
    try:
        with _get_pme_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT is_pme FROM pme_check WHERE siren = ?", (siren,)
            ).fetchone()
            if row:
                return bool(row[0])
    except Exception:
        logger.warning("Cache PME lecture échouée pour %s", siren, exc_info=False)
    return None


def _save_pme_cache(cache_dir: str, siren: str, is_pme: bool) -> None:
    try:
        with _get_pme_db(cache_dir) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pme_check (siren, is_pme, checked_at) VALUES (?, ?, ?)",
                (siren, 1 if is_pme else 0, int(time.time()))
            )
            conn.commit()
    except Exception:
        logger.warning("Cache PME écriture échouée pour %s", siren, exc_info=False)

LOCAL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "geocode_address",
            "description": (
                "Géocode une adresse française (rue, ville, code postal) pour obtenir "
                "ses coordonnées GPS (latitude/longitude), l'adresse normalisée, le "
                "code INSEE de la commune, et le score de fiabilité. Utilise l'API "
                "officielle adresse.data.gouv.fr. À utiliser pour localiser des établissements, "
                "normaliser des adresses, ou préparer des données géographiques."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Adresse à géocoder (ex: '10 rue de la Paix Paris')",
                    },
                    "postcode": {
                        "type": "string",
                        "description": "Code postal optionnel pour affiner",
                    },
                    "city": {
                        "type": "string",
                        "description": "Ville optionnelle pour affiner",
                    },
                },
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_commune_info",
            "description": (
                "Récupère les données démographiques et administratives d'une commune "
                "française : population, superficie, densité, code INSEE, codes postaux, "
                "département, région. Utilise l'API Geo officielle (geo.api.gouv.fr). "
                "À utiliser pour contextualiser des données locales, comparer des communes, "
                "ou enrichir une recherche d'entreprises/marchés publics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nom": {
                        "type": "string",
                        "description": "Nom de la commune (ex: 'Toulon', 'Paris')",
                    },
                    "code_postal": {
                        "type": "string",
                        "description": "Code postal optionnel pour lever l'ambiguïté",
                    },
                },
                "required": ["nom"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_schools",
            "description": (
                "Recherche des établissements scolaires (écoles, collèges, lycées) "
                "dans une commune française. Retourne le nom, l'adresse, le type "
                "(public/privé), le nombre d'élèves, et les coordonnées. À utiliser "
                "pour 'trouver les écoles à Toulon', 'lycées publics à Paris', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commune": {
                        "type": "string",
                        "description": "Nom de la commune (ex: 'Toulon')",
                    },
                    "type_etablissement": {
                        "type": "string",
                        "description": "Type : 'Ecole', 'Collège', 'Lycée', ou laisser vide pour tous",
                    },
                    "statut": {
                        "type": "string",
                        "description": "'Public', 'Privé', ou vide pour les deux",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre max de résultats (défaut: 10, max: 25)",
                    },
                },
                "required": ["commune"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_water_quality",
            "description": (
                "Récupère les dernières mesures de qualité de l'eau (nappes phréatiques, "
                "cours d'eau) pour une commune ou une station de mesure. Retourne les "
                "paramètres chimiques et biologiques (nitrates, pesticides, etc.). "
                "Utilise l'API Hub'Eau (eaufrance.fr). À utiliser pour les questions "
                "environnementales, la qualité de l'eau potable, ou les analyses des nappes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commune": {
                        "type": "string",
                        "description": "Nom de la commune (ex: 'Toulon')",
                    },
                    "parametre": {
                        "type": "string",
                        "description": "Paramètre recherché : 'nitrates', 'pesticides', 'température', ou vide pour tous",
                    },
                },
                "required": ["commune"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_statistics",
            "description": (
                "Calcule des statistiques avancées (moyenne, médiane, écart-type, "
                "min, max, total, nombre de lignes) sur une colonne numérique d'un "
                "fichier data.gouv (CSV, Excel, Parquet, JSON). À utiliser quand "
                "l'utilisateur demande des stats, des agrégations, des moyennes, "
                "ou une analyse quantitative sur un fichier. Retourne les valeurs "
                "calculées avec interprétation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL directe du fichier à analyser",
                    },
                    "column": {
                        "type": "string",
                        "description": "Nom de la colonne numérique à analyser (ex: 'montant', 'effectif', 'surface')",
                    },
                    "operation": {
                        "type": "string",
                        "description": "Statistique(s) demandées : 'mean', 'median', 'std', 'min', 'max', 'sum', 'count', 'all'",
                    },
                },
                "required": ["url", "column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_report",
            "description": (
                "Génère un rapport structuré en markdown récapitulant toutes les "
                "données collectées dans ce thread : datasets analysés, entreprises "
                "trouvées, marchés publics, statistiques. À utiliser quand "
                "l'utilisateur demande un résumé, un compte-rendu, un rapport, "
                "ou une synthèse de la conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_scatter_map",
            "description": (
                "Génère une carte de points (scatter plot, PNG) à partir d'une liste de "
                "coordonnées GPS (latitude, longitude). À utiliser quand l'utilisateur "
                "demande une carte, une visualisation géographique, ou veut voir où sont "
                "situés des établissements, stations, communes, entreprises, etc. "
                "Le résultat contient une balise [FICHIER_GÉNÉRÉ] que tu dois recopier "
                "exactement dans ta réponse pour que le fichier soit uploadé sur Slack."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "points": {
                        "type": "array",
                        "description": "Liste de points. Chaque point est un dict avec 'lat', 'lon' et optionnellement 'label'.",
                        "items": {"type": "object"},
                    },
                    "title": {
                        "type": "string",
                        "description": "Titre de la carte (ex: 'Stations Vélib à Paris')",
                    },
                },
                "required": ["points"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_interactive_map",
            "description": (
                "Génère une carte interactive (HTML, Leaflet) à partir d'une liste de "
                "coordonnées GPS. Les marqueurs sont cliquables et affichent les détails. "
                "À utiliser quand l'utilisateur demande une carte interactive, une carte "
                "HTML, ou veut explorer les points géographiques de manière interactive. "
                "Le résultat contient une balise [FICHIER_GÉNÉRÉ] que tu dois recopier "
                "exactement dans ta réponse pour que le fichier soit uploadé sur Slack."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "points": {
                        "type": "array",
                        "description": "Liste de points. Chaque point est un dict avec 'lat', 'lon' et optionnellement 'label' et 'category'.",
                        "items": {"type": "object"},
                    },
                    "title": {
                        "type": "string",
                        "description": "Titre de la carte (ex: 'Stations Vélib à Paris')",
                    },
                    "category_key": {
                        "type": "string",
                        "description": "Clé dans les points qui définit la catégorie/couleur (ex: 'category'). Optionnel.",
                    },
                },
                "required": ["points"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_legifrance",
            "description": (
                "Recherche des articles de loi, décrets, arrêtés et textes juridiques "
                "français sur Legifrance ou via les sources officielles. À utiliser "
                "quand l'utilisateur pose une question juridique (RGPD, marchés "
                "publics, code du travail, urbanisme, etc.) ou veut connaître le "
                "cadre légal d'un sujet. Ne PAS inventer d'articles de loi."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Requête juridique (ex: 'marchés publics intelligence artificielle', 'RGPD données personnelles collectivités')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre max de résultats (défaut: 5, max: 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_notified_contracts",
            "description": (
                "Recherche dans les marchés publics DÉJÀ NOTIFIÉS et attribués, "
                "stockés dans le dataset officiel 'Données essentielles de la commande "
                "publique' (DECP) sur data.gouv.fr. Retourne des résultats structurés "
                "avec objet du marché, montant, date de notification, acheteur et "
                "attributaire. À utiliser quand l'utilisateur demande l'HISTORIQUE des "
                "marchés passés, des montants déjà attribués, ou des données consolidées. "
                "Ne PAS utiliser pour les appels d'offre EN COURS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Mot-clé OBLIGATOIRE à chercher dans l'objet du marché. Si l'utilisateur ne précise pas de sujet, utilise 'marché' ou 'travaux'. Jamais vide.",
                    },
                    "year": {
                        "type": "string",
                        "description": "Année du fichier DECP à interroger (défaut: 2026)",
                    },
                    "region": {
                        "type": "string",
                        "description": "Nom de région (ex: 'Occitanie', 'Île-de-France', 'Bretagne')",
                    },
                    "departement": {
                        "type": "string",
                        "description": "Code département numérique (ex: '31', '75') ou liste séparée par virgule",
                    },
                    "montant_min": {
                        "type": "integer",
                        "description": "Montant minimum du marché en euros",
                    },
                    "montant_max": {
                        "type": "integer",
                        "description": "Montant maximum du marché en euros",
                    },
                    "pme_only": {
                        "type": "boolean",
                        "description": "Filtrer pour ne garder que les marchés attribués à des PME (vérification via API Entreprises). Plus lent.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre max de résultats (défaut: 10, max: 30)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_boamp_tenders",
            "description": (
                "Recherche les appels d'offre (AO) et consultations en cours sur le "
                "BOAMP (Bulletin Officiel des Marchés Publics) et les portails officiels. "
                "Retourne les annonces récentes avec titre, acheteur, date de publication, "
                "date limite de réponse, département et lien direct vers l'annonce. "
                "À utiliser EXCLUSIVEMENT et DÈS QUE l'utilisateur demande des appels d'offre, "
                "marchés publics, consultations ouvertes, ou AO en cours. "
                "Ne PAS utiliser search_datasets, web_search, download_and_analyze_resource "
                "ni search_public_tenders pour les AO en cours — cet outil est le SEUL autorisé."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Sujet de l'appel d'offre (ex: 'intelligence artificielle', 'cybersécurité', 'informatique')",
                    },
                    "perimeter": {
                        "type": "string",
                        "description": "Périmètre géographique : 'national', 'europe', ou un nom de région/département",
                    },
                    "published_since": {
                        "type": "string",
                        "description": "Filtre temporel de publication. Formats acceptés : '7d' (7 derniers jours), '1w' (1 semaine), '1m' (1 mois), '3m' (3 mois), '1y' (1 an), ou une date ISO '2026-07-14'. IMPORTANT : si l'utilisateur demande 'les derniers' sans préciser de période explicite (ni 'cette semaine', ni 'ce mois'), utilise '30d' (30 derniers jours) pour obtenir des résultats significatifs. Si absent et aucune période n'est précisée dans la requête, laisse ce champ vide.",
                    },
                    "related_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Liste de synonymes ou termes voisins à rechercher en parallèle (ex: ['sécurité informatique', 'SSI', 'audit sécurité'] pour 'cybersécurité').",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_and_analyze_resource",
            "description": (
                "Télécharge une ressource data.gouv.fr (ou toute URL de fichier de "
                "données) et l'analyse localement en Python via DuckDB. À utiliser quand la "
                "Tabular API ne couvre pas la ressource : fichiers ZIP, TXT, "
                "JSON/JSONL/GeoJSON, Parquet, gros CSV. Retourne un résumé structuré : "
                "colonnes et types détectés, nombre de lignes scannées, échantillon, "
                "statistiques numériques (min/max/moyenne), colonnes géographiques et "
                "temporelles détectées, valeurs manquantes. Si une requête SQL SELECT est "
                "fournie, elle est exécutée sur le fichier et les résultats sont retournés "
                "(filtres, agrégations, top/flop). Le fichier complet n'est "
                "jamais transmis, seulement le résumé ou les résultats SQL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL directe de téléchargement du fichier",
                    },
                    "sql_query": {
                        "type": "string",
                        "description": "Requête SQL SELECT optionnelle à exécuter sur le fichier (ex: SELECT * FROM scan WHERE dpt_id = '83' LIMIT 10)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_enterprises",
            "description": (
                "Recherche des entreprises et établissements français via l'API "
                "Recherche d'Entreprises de l'État. Permet de trouver des artisans, "
                "commerces, professions libérales, etc. par métier, activité (NAF), "
                "localisation (code postal, département, commune). L'outil pagine "
                "automatiquement pour remonter jusqu'à 75 résultats. Retourne les "
                "informations clés : nom, adresse, SIRET, activité, effectif. "
                "IMPORTANT : le résultat de cet outil est un [RÉSULTAT BRUT] qui doit "
                "être recopié EXACTEMENT dans la réponse, sans reformulation, sans "
                "suppression de colonnes ni modification du format."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Terme de recherche : nom d'entreprise, métier, activité (ex: 'plombier', 'boulangerie')",
                    },
                    "code_postal": {
                        "type": "string",
                        "description": "Code postal pour filtrer (ex: '75016')",
                    },
                    "departement": {
                        "type": "string",
                        "description": "Numéro de département (ex: '75' pour Paris)",
                    },
                    "commune": {
                        "type": "string",
                        "description": "Nom de la commune (ex: 'Paris')",
                    },
                    "section_naf": {
                        "type": "string",
                        "description": "Section NAF à 2 caractères (ex: 'FZ' pour construction)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre max de résultats à remonter (défaut: 25, max: 75). À mettre 75 quand l'utilisateur demande 'tous' ou 'toutes'.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Nombre de résultats à sauter avant d'afficher (pour la pagination : 0 = première page, 20 = page 2, 40 = page 3, etc.)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_leaders",
            "description": (
                "Récupère la liste des dirigeants (président, directeur général, "
                "gérant, etc.) d'une entreprise française à partir de son SIREN "
                "(9 chiffres) ou SIRET (14 chiffres). Utilise l'API Annuaire des "
                "Entreprises de l'État."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "siren": {
                        "type": "string",
                        "description": "Numéro SIREN de l'entreprise (9 chiffres). Si un SIRET est fourni, ne garder que les 9 premiers chiffres.",
                    },
                },
                "required": ["siren"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Effectue une recherche web via Tavily pour trouver des informations "
                "à jour sur internet : coordonnées d'entreprises, actualités, "
                "données complémentaires, pages jaunes, avis clients, etc. "
                "À utiliser quand les API officielles ne fournissent pas assez "
                "d'informations (téléphones, emails, dirigeants récents)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Requête de recherche (ex: 'LE MEILLEUR PLOMBIER Paris 16ème téléphone', 'dirigeant SCI ALEX Paris')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre de résultats (défaut: 5, max: 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_web_tables",
            "description": (
                "Télécharge une page web et extrait les tableaux HTML sous forme de "
                "données structurées (markdown). Indispensable quand data.gouv.fr n'a "
                "pas le fichier plat et que la source officielle (INSEE, Wikipédia, etc.) "
                "publie les chiffres directement dans un tableau sur son site. "
                "À utiliser APRÈS `web_search` quand un lien pertinent a été identifié. "
                "Ne PAS utiliser pour télécharger des fichiers binaires (PDF, ZIP, Excel)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL complète de la page contenant le tableau (ex: 'https://www.insee.fr/fr/statistiques/XXXX')",
                    },
                    "table_index": {
                        "type": "integer",
                        "description": "Index du tableau à extraire (0 = premier tableau de la page). Si non précisé, retourne tous les tableaux.",
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Nombre maximum de lignes à retourner par tableau (défaut: 100)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_sirene_api",
            "description": (
                "Interroge l'API officielle Sirene de l'INSEE pour rechercher des "
                "entreprises françaises par état administratif (active A / cessée C), "
                "code NAF, période de radiation, ou département. Permet de compter "
                "les radiations par secteur (ex: BTP = codes NAF 41, 42, 43) sur une "
                "période donnée. Nécessite une inscription gratuite sur api.insee.fr. "
                "Si l'API n'est pas configurée, l'outil retourne les instructions "
                "d'inscription. À utiliser pour les questions précises sur les "
                "radiations, créations, ou état des entreprises par secteur d'activité."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "etat_administratif": {
                        "type": "string",
                        "description": "État administratif : 'A' (active) ou 'C' (cessée/radiée). Laisser vide pour tous.",
                    },
                    "naf_code": {
                        "type": "string",
                        "description": "Code NAF à 2 chiffres (ex: '41' pour construction de bâtiments, '43' pour travaux de construction spécialisés). Laisser vide pour tous secteurs.",
                    },
                    "date_debut": {
                        "type": "string",
                        "description": "Date de début de période au format YYYY-MM-DD (ex: '2024-01-01'). Filtrera sur la date de dernière mise à jour INSEE.",
                    },
                    "date_fin": {
                        "type": "string",
                        "description": "Date de fin de période au format YYYY-MM-DD (ex: '2024-12-31').",
                    },
                    "departement": {
                        "type": "string",
                        "description": "Code département pour filtrer (ex: '75', '31'). Laisser vide pour la France entière.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre max de résultats (défaut: 20, max: 100). Utiliser un petit nombre pour un décompte rapide.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_dvf_stats",
            "description": (
                "Interroge les données DVF (Demandes de Valeurs Foncières) de la DGFiP "
                "pour un code postal ou un département et une année donnés. Retourne le "
                "nombre de transactions, le prix moyen/médian au m², la répartition par type "
                "de bien. À utiliser DÈS QUE l'utilisateur demande des données immobilières "
                "précises sur une année et un lieu. Ne PAS utiliser web_search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code_postal": {
                        "type": "string",
                        "description": "Code postal (ex: '75011', '69001'). Laisser vide si département est renseigné.",
                    },
                    "departement": {
                        "type": "string",
                        "description": "Numéro de département (ex: '75', '35'). Laisser vide si code_postal est renseigné.",
                    },
                    "annee": {
                        "type": "string",
                        "description": "Année au format YYYY (ex: '2023', '2024')",
                    },
                    "voie": {
                        "type": "string",
                        "description": "Nom de voie optionnel pour filtrer, SANS le type de voie (ex: 'Batignolles', 'Rivoli', 'Faubourg Saint-Honoré'). Ne PAS inclure 'boulevard', 'rue', 'avenue', 'allée', 'place', etc. Le champ DVF 'Voie' ne contient que le nom propre.",
                    },
                },
                "required": ["annee"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_dvf_details",
            "description": (
                "Interroge les données DVF (Demandes de Valeurs Foncières) de la DGFiP "
                "et retourne les N dernières transactions individuelles (date, adresse, prix, surface, pièces). "
                "À utiliser DÈS QUE l'utilisateur demande les 'dernières ventes', 'liste des ventes', "
                "'détail des transactions', ou des exemples concrets de biens vendus. "
                "Ne PAS utiliser pour des statistiques agrégées (prix moyen, nombre total) — utiliser query_dvf_stats à la place. "
                "Ne PAS utiliser web_search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code_postal": {
                        "type": "string",
                        "description": "Code postal (ex: '75011', '69001'). Laisser vide si département est renseigné.",
                    },
                    "departement": {
                        "type": "string",
                        "description": "Numéro de département (ex: '75', '35'). Laisser vide si code_postal est renseigné.",
                    },
                    "annee": {
                        "type": "string",
                        "description": "Année au format YYYY (ex: '2023', '2024')",
                    },
                    "voie": {
                        "type": "string",
                        "description": "Nom de voie optionnel pour filtrer, SANS le type de voie (ex: 'Batignolles', 'Rivoli', 'Faubourg Saint-Honoré'). Ne PAS inclure 'boulevard', 'rue', 'avenue', 'allée', 'place', etc.",
                    },
                    "type_local": {
                        "type": "string",
                        "description": "Type de bien à filtrer : 'Appartement', 'Maison', ou vide pour tous. Défaut : 'Appartement'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Nombre maximum de transactions à retourner (défaut: 15, max: 50).",
                    },
                },
                "required": ["annee"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_dvf_trend",
            "description": (
                "Analyse la tendance des prix immobiliers sur plusieurs années pour une "
                "région ou un département. Interroge les données DVF (DGFiP) via DuckDB "
                "pour agréger rapidement les ventes et prix au m² année par année. "
                "À utiliser pour les questions comme 'évolution des prix sur 5 ans', "
                "'tendance immobilière en Bretagne', 'comparer 2020 et 2024'. "
                "Ne PAS utiliser web_search pour ces questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Région (ex: 'Bretagne', 'Île-de-France') ou département (ex: '35', '75')",
                    },
                    "annees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Liste d'années à comparer (ex: ['2020', '2021', '2022', '2023', '2024'])",
                    },
                    "type_local": {
                        "type": "string",
                        "description": "Type de bien à filtrer : 'Maison', 'Appartement', ou vide pour tous",
                    },
                },
                "required": ["location", "annees"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": (
                "Recherche des offres d'emploi via l'API France Travail (anciennement Pôle emploi). "
                "Retourne : intitulé, entreprise, lieu, type de contrat, salaire. "
                "À utiliser pour analyser le marché du travail local, la tension sur un métier, "
                "ou les recrutements dans un secteur géographique."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Métier ou mot-clé (ex: 'développeur', 'maçon', 'infirmier')",
                    },
                    "commune": {
                        "type": "string",
                        "description": "Nom de la commune ou code INSEE pour filtrer (optionnel)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Nombre max de résultats (défaut: 5, max: 25)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_water_level",
            "description": (
                "Récupère les dernières mesures de niveau des nappes phréatiques "
                "via l'API Hub'Eau (piézométrie). Retourne le niveau d'eau en mètres "
                "et la tendance sur les 30 derniers jours. "
                "À utiliser pour les questions environnementales, l'état des nappes, "
                "ou la sécheresse dans une région. Nécessite un code_bss (code station) "
                "ou une bbox (zone géographique)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code_bss": {
                        "type": "string",
                        "description": "Code de la station piézométrique (ex: '06512X0223/PA8')",
                    },
                    "bbox": {
                        "type": "string",
                        "description": "Bounding box 'min_lon,min_lat,max_lon,max_lat' (ex: '5.5,43.1,6.0,43.4')",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Nombre de jours d'historique (défaut: 30)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_legifrance",
            "description": (
                "Recherche dans le droit français via l'API Legifrance (Piste). "
                "Retourne les articles de code, lois, décrets correspondant à la requête. "
                "À utiliser pour les questions juridiques sur les marchés publics, "
                "le code du travail, ou toute réglementation française."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Termes de recherche (ex: 'délai de rétractation', 'marché public PME')",
                    },
                    "code_name": {
                        "type": "string",
                        "description": "Code à consulter : 'Code des marchés publics', 'Code du travail', ou vide pour tous",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Nombre max de résultats (défaut: 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meteo",
            "description": (
                "Récupère les prévisions météo sur 5 jours pour une commune ou des coordonnées GPS. "
                "Retourne températures, probabilité de pluie, et conditions. "
                "À utiliser pour planifier des visites de chantier, des travaux extérieurs, "
                "ou évaluer l'impact climatique sur un projet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude",
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude",
                    },
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_building_rnb",
            "description": (
                "Recherche un bâtiment dans le Référentiel National des Bâtiments (RNB) "
                "par adresse ou coordonnées GPS. Retourne l'identifiant RNB, le statut, "
                "l'adresse et la parcelle cadastrale. "
                "À utiliser pour identifier précisément un bâtiment dans le cadre de travaux BTP, "
                "d'urbanisme, ou d'immobilier."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Adresse complète (ex: '10 rue de la Paix 75002 Paris')",
                    },
                    "lat": {
                        "type": "number",
                        "description": "Latitude (optionnel, avec lon)",
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude (optionnel, avec lat)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "electricity_consumption_by_region",
            "description": (
                "Retourne la consommation électrique totale par région française pour une année donnée, "
                "en TWh, avec le détail par région, la part de chacune et la répartition par secteur "
                "d'usage (Industrie, Résidentiel, Tertiaire, Agriculture). "
                "Source : SDES (Données locales de consommation d'électricité). "
                "Années disponibles : 2018 à 2024. "
                "À utiliser dès que l'utilisateur demande la consommation électrique par région, "
                "province, ou zone géographique en France. "
                "À utiliser ÉGALEMENT comme proxy indicatif sur les questions de mix énergétique, "
                "production électrique, ou face énergétique régionale lorsque les données de production "
                "par filière (nucléaire / renouvelable) ne sont pas disponibles."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": "string",
                        "description": "Année de référence (ex: '2023', '2024')",
                    },
                },
                "required": ["year"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "traffic_summary",
            "description": (
                "Retourne le trafic moyen journalier annuel (TMJA) sur le réseau routier national "
                "(autoroutes et routes nationales) pour une région ou un département français, "
                "avec le détail par route, le taux de poids lourds, et la répartition concédé / non concédé. "
                "Source : Ministère de la Transition écologique (TMJA). "
                "Années disponibles : 2022, 2023, 2024. "
                "À utiliser dès que l'utilisateur demande le trafic routier, les comptages de véhicules, "
                "la circulation sur autoroutes, le TMJA, ou les statistiques de trafic par région ou département."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": (
                            "Nom de la région ou du département (ex: 'Île-de-France', '75', 'Provence-Alpes-Côte d'Azur'). "
                            "Laisse vide pour obtenir les stats France entière."
                        ),
                    },
                    "year": {
                        "type": "string",
                        "description": "Année de référence (ex: '2023', '2024')",
                    },
                },
                "required": ["year"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sncf_punctuality",
            "description": (
                "Retourne la ponctualité TGV SNCF (retards, annulations, causes) par liaison ou sur l'ensemble du réseau. "
                "Source : SNCF Open Data (régularité mensuelle TGV). "
                "Données de janvier 2018 à mars 2026. "
                "À utiliser dès que l'utilisateur demande les retards des trains, la ponctualité SNCF, "
                "les statistiques de retard TGV, les annulations, ou les horaires de trains en France. "
                "Si l'utilisateur ne précise pas de ligne, le tool retourne les stats nationales + top 10 des liaisons les plus en retard."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "depart": {
                        "type": "string",
                        "description": "Gare de départ (ex: 'Paris Gare de Lyon', 'Lyon Part Dieu'). Laisse vide pour toutes.",
                    },
                    "arrivee": {
                        "type": "string",
                        "description": "Gare d'arrivée (ex: 'Marseille Saint-Charles'). Laisse vide pour toutes.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sau_by_region",
            "description": (
                "Retourne la Surface Agricole Utile (SAU) par région en France, en kilohectares, "
                "avec le classement décroissant, la part de chaque région et l'année de mesure. "
                "Source : Ecolab / Agreste. "
                "À utiliser dès que l'utilisateur demande la surface agricole utilisée, la SAU, "
                "les terres agricoles, l'exploitation agricole par région ou la répartition des surfaces agricoles en France."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

LOCAL_TOOL_NAMES = {t["function"]["name"] for t in LOCAL_TOOLS}


def _format_dirigeant(dirigeants: list[dict]) -> str:
    """Formate le premier dirigeant d'une liste en string lisible."""
    if not dirigeants:
        return ""
    d = dirigeants[0]
    prenoms = d.get("prenoms", "")
    nom = d.get("nom", "")
    qualite = d.get("qualite", "")
    identite = f"{prenoms} {nom}".strip()
    if identite:
        return f"{identite} ({qualite})" if qualite else identite
    return ""


async def _fetch_dirigeant(siren: str) -> str:
    """Récupère le dirigeant principal via l'API Recherche d'Entreprises (fallback)."""
    if not siren or len(siren) != 9:
        return ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10), headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
            response = await client.get(
                f"https://recherche-entreprises.api.gouv.fr/search?q={siren}"
            )
            response.raise_for_status()
            data = response.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        if results:
            return _format_dirigeant(results[0].get("dirigeants", []))
    except Exception:
        logger.warning("Fallback dirigeant échoué pour SIREN %s", siren, exc_info=False)
    return ""


async def _deep_enrich(
    results: list[dict],
    _query: str,
    settings: Settings,
    offset: int = 0,
    limit: int = 20,
) -> str:
    """Enrichit un lot d'entreprises avec dirigeants, téléphones et emails.

    Cette fonction est appelée directement par bot.py (court-circuit)
    pour éviter de surcharger le LLM avec le parsing de tableaux markdown.
    """
    slice_results = results[offset:offset + limit]
    if not slice_results:
        return f"Aucune entreprise à enrichir pour offset={offset} (total: {len(results)})."

    # [ENRICHISSEMENT DIRIGEANTS] Fallback uniquement si pas déjà présent
    for r in slice_results:
        if r.get("dirigeant"):
            continue
        siren = r.get("siren", "")
        if siren and len(siren) == 9:
            r["dirigeant"] = await _fetch_dirigeant(siren)

    # [ENRICHISSEMENT TAVILY] Téléphone / email
    if getattr(settings, "tavily_api_key", ""):
        try:
            to_enrich = []
            for r in slice_results:
                to_enrich.append({
                    "nom": r.get("nom", ""),
                    "ville": r.get("ville", ""),
                    "adresse": r.get("adresse", ""),
                    "siret": r.get("siret", ""),
                })
            enriched = await enrich_results_with_tavily(to_enrich, settings.tavily_api_key)
            for i, r in enumerate(slice_results):
                r["telephone"] = enriched[i].get("telephone", "")
                r["email"] = enriched[i].get("email", "")
        except Exception:
            logger.exception("Échec enrichissement Tavily dans _deep_enrich")

    # Formatage
    lines = [f"**Enrichissement des entreprises {offset + 1} à {offset + len(slice_results)}** :\n"]
    for idx, r in enumerate(slice_results, start=offset + 1):
        nom = r.get("nom", "?")
        dirigeant = r.get("dirigeant", "Non trouvé") or "Non trouvé"
        telephone = r.get("telephone", "Non trouvé") or "Non trouvé"
        email = r.get("email", "Non trouvé") or "Non trouvé"
        lines.append(
            f"{idx}. {nom}\n"
            f"   👤 Dirigeant: {dirigeant}\n"
            f"   📞 Tél: {telephone}\n"
            f"   ✉️ Email: {email}"
        )

    next_offset = offset + len(slice_results)
    has_more = next_offset < len(results)
    if has_more:
        lines.append(
            f"\n💡 Souhaites-tu que je continue avec le lot suivant "
            f"({next_offset + 1} à {min(next_offset + limit, len(results))}) ?"
        )
    else:
        lines.append("\n✅ Toutes les entreprises ont été enrichies.")

    return "\n".join(lines)


def _extract_sirens(titulaires_data: Any) -> set[str]:
    """Extrait les SIREN (9 premiers caractères) de la structure DECP titulaires.

    Gère les variations :
    - [{"id": "..."}]
    - [{"titulaire": {"id": "..."}}]
    - {"titulaire": {"id": "..."}}
    - {"titulaire": [{"id": "..."}, {"id": "..."}]}
    """
    sirens: set[str] = set()
    if isinstance(titulaires_data, list):
        for item in titulaires_data:
            if isinstance(item, dict):
                if "titulaire" in item:
                    inner = item["titulaire"]
                    if isinstance(inner, dict):
                        s = inner.get("id", "")
                        if s and len(s) >= 9:
                            sirens.add(s[:9])
                    elif isinstance(inner, list):
                        for t in inner:
                            if isinstance(t, dict):
                                s = t.get("id", "")
                                if s and len(s) >= 9:
                                    sirens.add(s[:9])
                else:
                    s = item.get("id", "")
                    if s and len(s) >= 9:
                        sirens.add(s[:9])
    elif isinstance(titulaires_data, dict):
        if "titulaire" in titulaires_data:
            inner = titulaires_data["titulaire"]
            if isinstance(inner, dict):
                s = inner.get("id", "")
                if s and len(s) >= 9:
                    sirens.add(s[:9])
            elif isinstance(inner, list):
                for t in inner:
                    if isinstance(t, dict):
                        s = t.get("id", "")
                        if s and len(s) >= 9:
                            sirens.add(s[:9])
        else:
            s = titulaires_data.get("id", "")
            if s and len(s) >= 9:
                sirens.add(s[:9])
    return sirens


def _row_has_pme_siren(row: dict[str, Any], pme_sirens: set[str]) -> bool:
    """Vérifie si une ligne contient au moins un titulaire PME."""
    tits = row.get("titulaires", [])
    if isinstance(tits, list):
        for item in tits:
            if isinstance(item, dict):
                if "titulaire" in item:
                    inner = item["titulaire"]
                    if isinstance(inner, dict):
                        s = inner.get("id", "")
                        if s and len(s) >= 9 and s[:9] in pme_sirens:
                            return True
                    elif isinstance(inner, list):
                        for t in inner:
                            if isinstance(t, dict):
                                s = t.get("id", "")
                                if s and len(s) >= 9 and s[:9] in pme_sirens:
                                    return True
                else:
                    s = item.get("id", "")
                    if s and len(s) >= 9 and s[:9] in pme_sirens:
                        return True
    elif isinstance(tits, dict):
        if "titulaire" in tits:
            inner = tits["titulaire"]
            if isinstance(inner, dict):
                s = inner.get("id", "")
                if s and len(s) >= 9 and s[:9] in pme_sirens:
                    return True
            elif isinstance(inner, list):
                for t in inner:
                    if isinstance(t, dict):
                        s = t.get("id", "")
                        if s and len(s) >= 9 and s[:9] in pme_sirens:
                            return True
        else:
            s = tits.get("id", "")
            if s and len(s) >= 9 and s[:9] in pme_sirens:
                return True
    return False


def _is_safe_url(url: str) -> bool:
    """Vérifie que l'URL n'est pas une IP privée / localhost (protection SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if host.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        pass  # Ce n'est pas une IP, c'est un nom de domaine — OK
    return True


async def _get_insee_token(settings: Settings) -> str | None:
    """Obtient un token OAuth2 pour l'API Sirene INSEE (ancien portail WSO2).
    Si seule la clé est fournie (nouveau portail), retourne la clé directement."""
    if not settings.insee_consumer_key:
        return None
    # Nouveau portail api.insee.fr : clé d'API directe, pas besoin de token OAuth2
    if not settings.insee_consumer_secret:
        return settings.insee_consumer_key
    # Ancien portail WSO2 : OAuth2 client_credentials
    import base64
    credentials = f"{settings.insee_consumer_key}:{settings.insee_consumer_secret}"
    auth = base64.b64encode(credentials.encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.insee.fr/token",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials",
        )
        resp.raise_for_status()
        return resp.json().get("access_token")


async def _query_sirene_api(arguments: dict[str, Any], settings: Settings) -> str:
    """Interroge l'API Sirene 3.11 de l'INSEE avec les filtres fournis."""
    etat = (arguments.get("etat_administratif") or "").strip().upper()
    naf = (arguments.get("naf_code") or "").strip()
    # NOTE: date_debut/date_fin sont ignorés car l'API Sirene 3.11 ne permet
    # pas de filtrer par année de radiation (retourne systématiquement 404).
    departement = (arguments.get("departement") or "").strip()
    max_results = min(arguments.get("max_results", 20), 100)

    token_or_key = await _get_insee_token(settings)
    if not token_or_key:
        return (
            "ERREUR: API Sirene INSEE non configurée. "
            "Pour utiliser cet outil, vous devez :\n"
            "1. Créer un compte gratuit sur https://api.insee.fr\n"
            "2. Créer une application et souscrire à l'API Sirene\n"
            "3. Copier la clé d'API (Clés d'API) dans le fichier .env :\n"
            "   INSEE_CONSUMER_KEY=votre_cle\n"
            "4. Redémarrer le bot."
        )

    # Détection du mode d'authentification
    is_api_key_mode = not settings.insee_consumer_secret
    if is_api_key_mode:
        headers = {
            "X-INSEE-Api-Key-Integration": token_or_key,
            "Accept": "application/json",
            "User-Agent": "datagouv-slack-agent/0.1.0",
        }
    else:
        headers = {
            "Authorization": f"Bearer {token_or_key}",
            "Accept": "application/json",
            "User-Agent": "datagouv-slack-agent/0.1.0",
        }

    # Construction de la requête q (syntaxe Lucene INSEE Sirene 3.11)
    # Les filtres doivent être wrappés dans periode() pour les champs historiques
    periode_filters: list[str] = []
    if etat in ("A", "C"):
        periode_filters.append(f"etatAdministratifUniteLegale:{etat}")
    if naf:
        # Le BTP = 41, 42, 43. On utilise le wildcard .* pour les sous-codes
        periode_filters.append(f"activitePrincipaleUniteLegale:{naf}.*")
    # NOTE: L'API Sirene 3.11 ne permet PAS de filtrer par année de radiation
    # via dateDernierTraitementUniteLegale (retourne systématiquement 404).
    # On ignore donc date_debut/date_fin ici.

    q = f"periode({' AND '.join(periode_filters)})" if periode_filters else "*"

    params: dict[str, Any] = {"q": q, "nombre": max_results}
    if departement:
        params["codePostalEtablissement"] = f"{departement}*"

    # URL de l'API Sirene 3.11 (nouveau portail)
    api_url = "https://api.insee.fr/api-sirene/3.11/siren"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api_url, headers=headers, params=params)
            if resp.status_code == 401:
                return "ERREUR: Clé INSEE invalide ou non autorisée. Vérifiez que vous avez souscrit à l'API Sirene sur https://api.insee.fr et que la clé est correcte."
            if resp.status_code == 404:
                return f"Aucune entreprise trouvée avec les critères : {arguments}"
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return f"ERREUR API Sirene (HTTP {exc.response.status_code}): {exc.response.text[:500]}"
    except Exception as exc:
        return f"ERREUR API Sirene: {exc}"

    unites = data.get("unitesLegales", [])
    header = data.get("header", {})
    total = header.get("total", len(unites))

    if not unites:
        return f"Aucune entreprise trouvée avec les critères : {arguments}"

    lines = [f"🔍 Résultats API Sirene INSEE ({len(unites)} affichés sur {total} total) :\n"]
    for i, u in enumerate(unites[:max_results], 1):
        siren = u.get("siren", "")
        periodes = u.get("periodesUniteLegale", [{}])
        per = periodes[0] if periodes else {}
        nom = per.get("denominationUniteLegale", "")
        if not nom:
            nom = per.get("nomUniteLegale", "") + " " + per.get("prenom1UniteLegale", "")
        naf_code = per.get("activitePrincipaleUniteLegale", "")
        etat_ul = per.get("etatAdministratifUniteLegale", "")
        cat = per.get("categorieJuridiqueUniteLegale", "")
        lines.append(f"{i}. {nom or 'N/A'} (SIREN: {siren})")
        lines.append(f"   • NAF: {naf_code}")
        lines.append(f"   • État: {'Active' if etat_ul == 'A' else 'Cessée' if etat_ul == 'C' else etat_ul}")
        if cat:
            lines.append(f"   • Catégorie juridique: {cat}")
        adresse = u.get("adresseEtablissement", {})
        if adresse:
            cp = adresse.get("codePostalEtablissement", "")
            commune = adresse.get("libelleCommuneEtablissement", "")
            if cp or commune:
                lines.append(f"   • Adresse: {cp} {commune}")
        lines.append("")

    if total > max_results:
        lines.append(f"... et {total - max_results} résultats supplémentaires.")
    return "\n".join(lines)


async def fetch_boamp_tenders(
    query: str,
    perimeter: str = "",
    published_since: str = "",
    related_terms: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int, date | None]:
    """
    Recherche les appels d'offre sur BOAMP et retourne les résultats bruts.

    Retourne (results_list, total_nhits, since_date).
    """
    from datetime import datetime, timedelta

    today = datetime.now().date()
    since_date = None

    # --- Auto-synonymes pour domaines techniques ---
    auto_related: list[str] = []
    q_lower = query.lower()
    if any(k in q_lower for k in ("cybersécurité", "cybersecurite", "cyber sécurité")):
        auto_related = ["sécurité informatique", "SSI", "audit sécurité", "sécurité des systèmes d'information"]
    elif any(k in q_lower for k in ("intelligence artificielle", "ia ")):
        auto_related = ["machine learning", "deep learning", "traitement automatique du langage", "NLP"]
    elif any(k in q_lower for k in ("blockchain", "crypto")):
        auto_related = ["distributed ledger", "smart contract", "web3"]
    elif any(k in q_lower for k in ("cloud", "hébergement")):
        auto_related = ["infogérance", "SaaS", "IaaS", "hébergement données"]

    # --- Parsing explicite published_since ---
    if published_since:
        try:
            since_date = datetime.strptime(published_since, "%Y-%m-%d").date()
        except ValueError:
            ps = published_since.lower()
            if ps in ("7d", "1w", "week"):
                since_date = today - timedelta(days=7)
            elif ps in ("30d", "1m", "month"):
                since_date = today - timedelta(days=30)
            elif ps in ("90d", "3m", "trimestre"):
                since_date = today - timedelta(days=90)
            elif ps in ("365d", "1y", "year"):
                since_date = today - timedelta(days=365)
    else:
        q_lower = query.lower()
        if any(k in q_lower for k in ("cette semaine", "7 derniers jours", "semaine dernière", "depuis une semaine")):
            since_date = today - timedelta(days=7)
        elif any(k in q_lower for k in ("ce mois", "30 derniers jours", "depuis un mois", "ce mois-ci")):
            since_date = today - timedelta(days=30)
        elif any(k in q_lower for k in ("ce trimestre", "3 derniers mois", "depuis 3 mois")):
            since_date = today - timedelta(days=90)
        elif any(k in q_lower for k in ("cette année", "depuis le début de l'année", "en 2026")):
            since_date = today.replace(month=1, day=1)
        elif any(k in q_lower for k in ("dernier", "derniers", "dernières", "récents", "dernieres")):
            since_date = today - timedelta(days=30)

    related = related_terms or []
    if isinstance(related, str):
        related = [related]
    queries = [query] + auto_related + [r for r in related if isinstance(r, str) and r.strip()]
    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))

    boamp_url = "https://boamp.fr/api/records/1.0/search/"
    all_results: dict[str, dict[str, Any]] = {}
    total_nhits = 0

    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": "datagouv-slack-agent/0.1.0"},
        follow_redirects=True,
    ) as client_http:
        tasks = []
        for q in queries:
            params: dict[str, Any] = {
                "dataset": "boamp",
                "q": q,
                "rows": 50,
                "sort": "dateparution",
                "facet": "nature",
                "refine.nature": "APPEL_OFFRE",
            }
            if perimeter:
                peri_clean = perimeter.replace(" ", "").replace("'", "").replace("-", "")
                if peri_clean.isdigit() and len(peri_clean) in (1, 2, 3):
                    params["refine.code_departement"] = peri_clean.zfill(2) if len(peri_clean) == 1 else peri_clean
                else:
                    params["q"] = f"{q} {perimeter}"
            tasks.append(client_http.get(boamp_url, params=params))

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for resp in responses:
            if isinstance(resp, Exception):
                logger.warning("BOAMP call failed for a term: %s", resp)
                continue
            resp.raise_for_status()
            data = resp.json()
            total_nhits += data.get("nhits", 0)

            for r in data.get("records", []):
                fields = r.get("fields", {})
                if not fields:
                    continue

                obj = (fields.get("objet") or "").strip()
                org = (fields.get("nomacheteur") or "").strip()
                dp = fields.get("dateparution")
                dl = fields.get("datelimitereponse")
                idweb = fields.get("idweb", "")
                url_avis = fields.get("url_avis", "")
                dept = fields.get("code_departement")
                descripteurs = fields.get("descripteur_libelle")

                if not obj or obj.lower() == "objet" or org.lower() == "organisme" or obj.lower().startswith("test "):
                    continue

                if since_date and dp:
                    try:
                        dp_date = datetime.strptime(str(dp), "%Y-%m-%d").date()
                        if dp_date < since_date:
                            continue
                    except Exception:
                        logger.warning("Date de parution invalide ignorée : %s (idweb=%s)", dp, idweb)
                        continue

                # Score de pertinence pour classer les résultats (élimine les faux positifs)
                score = 0
                obj_lower = obj.lower()
                desc_lower = " ".join(d.lower() for d in (descripteurs if isinstance(descripteurs, list) else [descripteurs]) if d)
                for q_term in queries:
                    q_l = q_term.lower()
                    if q_l in obj_lower:
                        score += 10  # Mot-clé dans l'objet = très pertinent
                    elif q_l in desc_lower:
                        score += 5   # Mot-clé dans les descripteurs = pertinent
                    elif any(word in obj_lower for word in q_l.split()):
                        score += 2   # Mot partiel dans l'objet = peu pertinent
                # Pénaliser les objets très courts ou génériques sans lien technique
                if len(obj) < 20:
                    score -= 3
                if any(bad in obj_lower for bad in ("ma pa", "mapa", "formation", "remplacement", "achat de ", "fourniture de bureau")):
                    score -= 5

                if idweb and idweb not in all_results:
                    all_results[idweb] = {
                        "idweb": idweb,
                        "objet": obj,
                        "org": org,
                        "dateparution": dp,
                        "datelimitereponse": dl,
                        "url_avis": url_avis,
                        "departements": dept if isinstance(dept, list) else ([dept] if dept else []),
                        "descripteurs": descripteurs if isinstance(descripteurs, list) else ([descripteurs] if descripteurs else []),
                        "_score": score,
                    }

    # Trier par pertinence décroissante, puis par date de publication décroissante
    results_list = list(all_results.values())
    results_list.sort(key=lambda r: (r.get("_score", 0), str(r.get("dateparution", ""))), reverse=True)
    return results_list, total_nhits, since_date


async def execute_local_tool(
    name: str,
    arguments: dict[str, Any],
    settings: Settings,
    context: dict[str, Any] | None = None,
) -> str:
    """Exécute un tool local et retourne un texte injectable dans le contexte LLM.

    Les erreurs contrôlées (taille, format, réseau) sont retournées comme texte
    préfixé ERREUR, pour que le modèle puisse les expliquer à l'utilisateur.
    """
    thread_ts = (context or {}).get("thread_ts", "")

    if name == "geocode_address":
        address = (arguments.get("address") or "").strip()
        postcode = (arguments.get("postcode") or "").strip()
        city = (arguments.get("city") or "").strip()
        if not address:
            return "ERREUR: adresse manquante."
        try:
            params = {"q": address, "limit": 5}
            if postcode:
                params["postcode"] = postcode
            if city:
                params["city"] = city
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                resp = await client.get(
                    "https://api-adresse.data.gouv.fr/search/", params=params
                )
                resp.raise_for_status()
                data = resp.json()
            features = data.get("features", [])
            if not features:
                return f"Aucune adresse trouvée pour '{address}'."
            lines = [f"📍 Résultats pour *{address}* :\n"]
            for i, feat in enumerate(features[:3], 1):
                props = feat.get("properties", {})
                geom = feat.get("geometry", {}).get("coordinates", [])
                label = props.get("label", "Adresse inconnue")
                score = props.get("score", 0)
                insee = props.get("citycode", "")
                context_str = props.get("context", "")
                lat = geom[1] if len(geom) >= 2 else None
                lon = geom[0] if len(geom) >= 2 else None
                lines.append(f"{i}. {label}")
                lines.append(f"   • Score : {score:.2f}")
                lines.append(f"   • Code INSEE : {insee}")
                if context_str:
                    lines.append(f"   • Contexte : {context_str}")
                if lat is not None and lon is not None:
                    lines.append(f"   • Coordonnées GPS : {lat}, {lon} (lat, lon)")
                    lines.append(f"   • <https://www.google.com/maps?q={lat},{lon}|Voir sur Google Maps>")
                    lines.append(f"   • <https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=15|Voir sur OpenStreetMap>")
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur géocodage")
            return f"ERREUR géocodage: {exc}"

    if name == "get_commune_info":
        nom = (arguments.get("nom") or "").strip()
        code_postal = (arguments.get("code_postal") or "").strip()
        if not nom:
            return "ERREUR: nom de commune manquant."
        try:
            params = {"nom": nom, "fields": "nom,code,codesPostaux,population,surface,departement,region"}
            if code_postal:
                params["codePostal"] = code_postal
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                resp = await client.get(
                    "https://geo.api.gouv.fr/communes", params=params
                )
                resp.raise_for_status()
                data = resp.json()
            if not data:
                return f"Commune '{nom}' non trouvée."
            c = data[0]
            lines = [f"🏛️ *{c.get('nom', nom)}*"]
            lines.append(f"• Code INSEE : {c.get('code', 'N/A')}")
            cp = c.get("codesPostaux", [])
            if cp:
                lines.append(f"• Codes postaux : {', '.join(cp)}")
            pop = c.get("population")
            if pop:
                lines.append(f"• Population : {pop:,}")
            surf = c.get("surface")
            if surf:
                lines.append(f"• Superficie : {surf / 100:.0f} km²")
            dep = c.get("departement", {})
            if dep:
                lines.append(f"• Département : {dep.get('nom', '')} ({dep.get('code', '')})")
            reg = c.get("region", {})
            if reg:
                lines.append(f"• Région : {reg.get('nom', '')} ({reg.get('code', '')})")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur commune info")
            return f"ERREUR: {exc}"

    if name == "search_schools":
        commune = (arguments.get("commune") or "").strip()
        type_etab = (arguments.get("type_etablissement") or "").strip()
        statut = (arguments.get("statut") or "").strip().lower()
        max_results = min(arguments.get("max_results", 10), 25)
        if not commune:
            return "ERREUR: commune manquante."
        try:
            # Récupérer le code INSEE de la commune via l'API Geo
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                resp = await client.get(
                    "https://geo.api.gouv.fr/communes",
                    params={"nom": commune, "fields": "code"},
                )
                resp.raise_for_status()
                comm_data = resp.json()
                if not comm_data:
                    return f"Commune '{commune}' non trouvée."
                code_insee = comm_data[0].get("code", "")
            if not code_insee:
                return f"Code INSEE introuvable pour {commune}."

            # Appel API Annuaire Éducation
            where_clauses = [f'code_commune="{code_insee}"']
            if type_etab:
                where_clauses.append(f'type_etablissement="{type_etab}"')
            if statut:
                where_clauses.append(f'statut_public_prive="{statut.capitalize()}"')
            where_str = " AND ".join(where_clauses)

            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                resp = await client.get(
                    "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/fr-en-annuaire-education/records",
                    params={"limit": max_results, "where": where_str},
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"Aucun établissement scolaire trouvé à {commune}."

            lines = [f"🎓 Établissements scolaires à *{commune}* :\n"]
            for i, r in enumerate(results, 1):
                nom = r.get("nom_etablissement", "Sans nom")
                adresse = r.get("adresse_1", "")
                type_str = r.get("type_etablissement", "")
                stat = r.get("statut_public_prive", "")
                uai = r.get("identifiant_de_l_etablissement", "")
                lines.append(f"{i}. {nom}")
                if type_str:
                    lines.append(f"   • Type : {type_str}")
                if stat:
                    lines.append(f"   • Statut : {stat}")
                if adresse:
                    lines.append(f"   • Adresse : {adresse}")
                if uai:
                    lines.append(f"   • Code UAI : {uai}")
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur recherche écoles")
            return f"ERREUR: {exc}"

    if name == "get_water_quality":
        commune = (arguments.get("commune") or "").strip()
        parametre = (arguments.get("parametre") or "").strip().lower()
        if not commune:
            return "ERREUR: commune manquante."
        try:
            # 1. Code INSEE
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                resp = await client.get(
                    "https://geo.api.gouv.fr/communes",
                    params={"nom": commune, "fields": "code"},
                )
                resp.raise_for_status()
                comm_data = resp.json()
                if not comm_data:
                    return f"Commune '{commune}' non trouvée."
                code_insee = comm_data[0].get("code", "")
            if not code_insee:
                return f"Code INSEE introuvable pour {commune}."

            # 2. Hub'Eau — qualité des nappes
            params = {"code_commune": code_insee, "size": 10, "sort": "desc"}
            if parametre:
                params["nom_parametre"] = parametre

            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                resp = await client.get(
                    "https://hubeau.eaufrance.fr/api/v1/qualite_nappes/analyses",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("data", [])
            if not results:
                return f"Aucune donnée de qualité de l'eau trouvée pour {commune}."

            lines = [f"💧 Qualité de l'eau à *{commune}* (dernières mesures) :\n"]
            for i, r in enumerate(results[:5], 1):
                param = r.get("nom_param", "Inconnu")
                val = r.get("resultat", "N/A")
                unit = r.get("code_unite", "")
                date_mes = r.get("date_prelevement", "")
                station = r.get("nom_bss", "")
                lines.append(f"{i}. {param}")
                lines.append(f"   • Résultat : {val} {unit}")
                if date_mes:
                    lines.append(f"   • Date : {date_mes}")
                if station:
                    lines.append(f"   • Station : {station}")
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur qualité eau")
            return f"ERREUR: {exc}"

    if name == "compute_statistics":
        url = (arguments.get("url") or "").strip()
        column = (arguments.get("column") or "").strip()
        operation = (arguments.get("operation") or "all").strip().lower()
        if not url or not column:
            return "ERREUR: URL et colonne obligatoires."
        if not _is_safe_url(url):
            return "ERREUR: URL invalide."
        try:
            path = await download_resource(
                url, settings.cache_dir, settings.max_download_mb
            )
            ops_map = {
                "mean": "avg",
                "moyenne": "avg",
                "median": "median",
                "médiane": "median",
                "std": "stddev",
                "ecart-type": "stddev",
                "écart-type": "stddev",
                "min": "min",
                "max": "max",
                "sum": "sum",
                "total": "sum",
                "count": "count",
            }
            ALLOWED_OPS = {"count", "avg", "median", "stddev", "min", "max", "sum"}
            if operation in ("all", "tout", ""):
                sel_ops = ["count", "avg", "median", "stddev", "min", "max", "sum"]
            else:
                op_mapped = ops_map.get(operation, operation)
                if op_mapped not in ALLOWED_OPS:
                    return (
                        "ERREUR: opération non supportée. "
                        "Utilise : count, avg, median, stddev, min, max, sum."
                    )
                sel_ops = [op_mapped]

            # [SÉCURITÉ] Valider que le nom de colonne est alphanumérique (évite injection SQL)
            if not column.replace(" ", "").replace("_", "").replace("-", "").isalnum():
                return "ERREUR: nom de colonne invalide (caractères spéciaux interdits)."
            safe_col = column.replace('"', '""')
            aggs = ", ".join(f"{op}(\"{safe_col}\") as {op}" for op in sel_ops)
            sql = f"SELECT {aggs} FROM scan"

            rows = await asyncio.wait_for(
                asyncio.to_thread(query_file, Path(path), sql, settings.max_scan_rows),
                timeout=60,
            )
            if not rows:
                return "Aucune donnée trouvée."
            result = rows[0]
            lines = [f"📊 Statistiques pour *{column}* :"]
            for k, v in result.items():
                if v is not None:
                    label = {"count": "Nombre de lignes", "avg": "Moyenne",
                             "median": "Médiane", "stddev": "Écart-type",
                             "min": "Minimum", "max": "Maximum",
                             "sum": "Total"}.get(k, k)
                    val = f"{v:,.2f}" if isinstance(v, float) else str(v)
                    lines.append(f"• {label} : {val}")
                else:
                    lines.append(f"• {k} : non disponible")
            return "\n".join(lines)
        except (DownloadError, AnalysisError) as exc:
            return f"ERREUR: {exc}"
        except Exception as exc:
            logger.exception("Erreur compute_statistics")
            return f"ERREUR: {exc}"

    if name == "generate_report":
        try:
            from app.data.exporter import load_search_results
            # Récupérer les données du thread courant
            ts = thread_ts or ""
            data = load_search_results(settings.cache_dir, ts)
            lines = ["# 📋 Rapport de recherche\n"]
            if data and data.get("results"):
                lines.append(f"**Requête** : {data.get('query', 'N/A')}\n")
                lines.append(f"**Résultats** : {len(data['results'])} ligne(s)\n")
                # Tableau markdown
                if data["results"]:
                    keys = list(data["results"][0].keys())
                    lines.append("| " + " | ".join(keys) + " |")
                    lines.append("| " + " | ".join(["---"] * len(keys)) + " |")
                    for r in data["results"][:20]:
                        vals = [str(r.get(k, ""))[:30] for k in keys]
                        lines.append("| " + " | ".join(vals) + " |")
                    if len(data["results"]) > 20:
                        lines.append(f"\n... et {len(data['results']) - 20} lignes supplémentaires.")
            else:
                lines.append("Aucune donnée enregistrée dans ce thread pour le moment.")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur generate_report")
            return f"ERREUR: {exc}"

    if name == "generate_scatter_map":
        points = arguments.get("points", [])
        title = arguments.get("title", "Carte")
        if not points:
            return "ERREUR: aucun point fourni."
        try:
            from app.data.charts import generate_scatter_map
            path = generate_scatter_map(
                points,
                lat_key="lat",
                lon_key="lon",
                label_key="label",
                title=title,
                cache_dir=settings.cache_dir,
            )
            return f"[FICHIER_GÉNÉRÉ] {path}"
        except Exception as exc:
            logger.exception("Erreur génération carte")
            return f"ERREUR génération carte: {exc}"

    if name == "generate_interactive_map":
        points = arguments.get("points", [])
        title = arguments.get("title", "Carte interactive")
        category_key = arguments.get("category_key")
        if not points:
            return "ERREUR: aucun point fourni."
        try:
            from app.data.charts import generate_interactive_map
            path = generate_interactive_map(
                points,
                lat_key="lat",
                lon_key="lon",
                label_key="label",
                category_key=category_key,
                title=title,
                cache_dir=settings.cache_dir,
            )
            return f"[FICHIER_GÉNÉRÉ] {path}"
        except Exception as exc:
            logger.exception("Erreur génération carte interactive")
            return f"ERREUR génération carte interactive: {exc}"

    if name == "search_legifrance":
        query = (arguments.get("query") or "").strip()
        max_results = min(arguments.get("max_results", 5), 10)
        if not query:
            return "ERREUR: requête juridique manquante."
        try:
            from tavily import TavilyClient
            api_key = getattr(settings, "tavily_api_key", "")
            if not api_key:
                return "ERREUR: clé API Tavily non configurée."
            client = TavilyClient(api_key=api_key)
            search_query = f"site:legifrance.gouv.fr OR site:vie-publique.fr {query} loi décret arrêté"
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.search, search_query, max_results=max_results, include_answer=True
                ),
                timeout=30,
            )
            results = response.get("results", [])
            answer = response.get("answer", "")
            lines = []
            if answer:
                lines.append(f"**Réponse synthétique** : {answer}\n")
            if results:
                lines.append("**Textes juridiques trouvés** :")
                for i, r in enumerate(results, 1):
                    title = r.get("title", "Sans titre")
                    url = r.get("url", "")
                    content = r.get("content", "")[:350]
                    lines.append(f"{i}. {title}\n   {content}...\n   {url}")
            else:
                lines.append("Aucun texte juridique trouvé pour cette recherche.")
            return "\n\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur recherche Legifrance")
            return f"ERREUR recherche juridique: {exc}"

    if name == "query_notified_contracts":
        query = (arguments.get("query") or "").strip()
        year = (arguments.get("year") or "2026").strip()
        region = (arguments.get("region") or "").strip()
        departement = (arguments.get("departement") or "").strip()
        montant_min = arguments.get("montant_min")
        montant_max = arguments.get("montant_max")
        pme_only = arguments.get("pme_only", False)
        max_results = min(arguments.get("max_results", 10), 30)
        if not query:
            return "ERREUR: requête manquante."

        # --- Mapping région → départements ---
        REGION_DEPTS = {
            "occitanie": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
            "auvergne-rhône-alpes": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
            "provence-alpes-côte d'azur": ["04", "05", "06", "13", "83", "84"],
            "provence-alpes-cote-d-azur": ["04", "05", "06", "13", "83", "84"],
            "nouvelle-aquitaine": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
            "bretagne": ["22", "29", "35", "56"],
            "centre-val de loire": ["18", "28", "36", "37", "41", "45"],
            "centre-val-de-loire": ["18", "28", "36", "37", "41", "45"],
            "bourgogne-franche-comté": ["21", "25", "39", "58", "70", "71", "89", "90"],
            "bourgogne-franche-comte": ["21", "25", "39", "58", "70", "71", "89", "90"],
            "grand est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
            "grand-est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
            "hauts-de-france": ["02", "59", "60", "62", "80"],
            "île-de-france": ["75", "77", "78", "91", "92", "93", "94", "95"],
            "ile-de-france": ["75", "77", "78", "91", "92", "93", "94", "95"],
            "normandie": ["14", "27", "50", "61", "76"],
            "pays de la loire": ["44", "49", "53", "72", "85"],
            "pays-de-la-loire": ["44", "49", "53", "72", "85"],
            "corse": ["2A", "2B"],
        }
        dept_filter: list[str] = []
        if departement:
            dept_filter = [
                d.strip().zfill(2) if d.strip().isdigit() else d.strip().upper()
                for d in departement.split(",") if d.strip()
            ]
        if region:
            r_key = region.lower().replace(" ", "-").replace("'", "")
            mapped = REGION_DEPTS.get(r_key)
            if mapped:
                dept_filter = mapped

        dataset_id = "5cd57bf68b4c4179299eb0e9"
        try:
            # 1. Récupérer la liste des ressources DECP
            async with httpx.AsyncClient(
                timeout=15, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}
            ) as client:
                resp = await client.get(
                    f"https://www.data.gouv.fr/api/1/datasets/{dataset_id}/"
                )
                resp.raise_for_status()
                data = resp.json()

            resources = data.get("resources", [])
            monthly_targets = []
            consolidated_target = None
            for r in resources:
                title = r.get("title", "")
                fmt = r.get("format", "").lower()
                if fmt == "json":
                    if title.startswith(f"decp-{year}-") and title.endswith(".json"):
                        monthly_targets.append(r)
                    elif title == f"decp-{year}.json":
                        consolidated_target = r

            monthly_targets.sort(key=lambda r: r.get("title", ""))

            if monthly_targets:
                targets = monthly_targets
                is_monthly = True
            elif consolidated_target:
                targets = [consolidated_target]
                is_monthly = False
            else:
                return f"ERREUR: aucun fichier DECP trouvé pour l'année {year}."

            # 2. Construire la requête SQL commune
            import re as _re
            keywords = [
                _re.sub(r"[^\w\s-]", "", k).strip()
                for k in query.split()
                if len(k.strip()) > 2
            ]
            if not keywords:
                keywords = [_re.sub(r"[^\w\s-]", "", query)]

            # Mots trop génériques qui ne filtrent pas l'objet du marché
            GENERIC_WORDS = {
                "marche", "marches", "marché", "marchés", "public", "publics",
                "publique", "publiques", "contrat", "contrats", "attribution",
                "attribue", "attribuee", "attribué", "attribuée", "attribues",
                "attribués", "attribuees", "attribuées", "notifie", "notifié",
                "notifiee", "notifiée", "notifies", "notifiés", "notifiees",
                "notifiées", "notification", "notifications", "passe", "passé",
                "passee", "passée", "passes", "passés", "passees", "passées",
                "historique", "historiques", "deja", "déjà", "titulaire",
                "titulaires", "acheteur", "acheteurs", "commande", "commandes",
                "administration", "administrations", "collectivite", "collectivités",
                "etat", "état", "commune", "communes", "departement",
                "département", "departements", "départements", "region", "région",
                "regions", "régions", "france",
            }
            filtered_keywords = [k for k in keywords if k.lower() not in GENERIC_WORDS]

            def _escape_like(s: str) -> str:
                # Échappe les wildcards SQL (% _ \) et les quotes
                return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")

            if filtered_keywords:
                like_conditions = " OR ".join(
                    f"lower(unnest.objet) LIKE '%{_escape_like(k.lower())}%' ESCAPE '\\'"
                    for k in filtered_keywords if k
                )
            else:
                like_conditions = "1=1"

            if not like_conditions:
                return "ERREUR: requête trop courte ou invalide."

            where_clauses = [like_conditions]
            if montant_min is not None:
                where_clauses.append(f"unnest.montant >= {int(montant_min)}")
            if montant_max is not None:
                where_clauses.append(f"unnest.montant <= {int(montant_max)}")

            where_str = " AND ".join(where_clauses)
            if pme_only:
                sql_limit = max_results * 2
            elif dept_filter:
                sql_limit = max_results * 4
            else:
                sql_limit = max_results

            sql = (
                f"SELECT unnest.objet as objet, unnest.montant as montant, "
                f"unnest.dateNotification as dateNotification, "
                f"unnest.acheteur.id as acheteur_id, "
                f"unnest.titulaires as titulaires, "
                f"unnest.lieuExecution.code as lieu_code, "
                f"unnest.lieuExecution.typeCode as lieu_type "
                f"FROM scan, unnest(marches.marche) "
                f"WHERE {where_str} "
                f"ORDER BY dateNotification DESC "
                f"LIMIT {sql_limit}"
            )

            # 3. Télécharger tous les fichiers en parallèle (I/O bound)
            async def _download_one(target: dict) -> Path:
                url = target["url"]
                if is_monthly:
                    return await download_resource(
                        url, settings.cache_dir, settings.max_download_mb, max_mb_override=300
                    )
                else:
                    return await download_resource(
                        url, settings.cache_dir, settings.max_download_mb, max_mb_override=800
                    )

            download_tasks = [_download_one(t) for t in targets]
            download_results = await asyncio.gather(*download_tasks, return_exceptions=True)

            # 4. Parser en parallèle avec un pool de 3 workers (CPU/RAM bound)
            from concurrent.futures import ThreadPoolExecutor

            all_rows: list[dict] = []
            paths_to_process = []
            for target, dl_result in zip(targets, download_results, strict=False):
                if isinstance(dl_result, Exception):
                    logger.warning("Téléchargement échoué pour %s: %s", target["title"], dl_result)
                    continue
                paths_to_process.append((target["title"], dl_result))

            def _query_one(args):
                file_title, path = args
                try:
                    rows = query_file(Path(path), sql, settings.max_scan_rows)
                    return file_title, rows, None
                except Exception as exc:
                    return file_title, None, exc

            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    loop.run_in_executor(executor, _query_one, args)
                    for args in paths_to_process
                ]
                for file_title, rows, exc in await asyncio.gather(*futures):
                    if exc:
                        logger.warning("Requête échouée pour %s: %s", file_title, exc)
                        continue
                    all_rows.extend(rows)

            # 4. Normaliser et filtrer par département (post-traitement car lieuExecution.code
            # peut contenir des codes postaux, départements ou communes)
            def _normalize_dept(code: str, type_code: str) -> str:
                if not code:
                    return ""
                tc = (type_code or "").strip()
                c = str(code).strip()
                if tc == "Code département":
                    return c.upper()
                if tc == "Code postal":
                    if c.startswith(("200", "201")):
                        return "2A"
                    if c.startswith("202"):
                        return "2B"
                    if len(c) == 5 and c[:2] in ("97", "98"):
                        return c[:3]
                    return c[:2] if len(c) >= 2 else c
                if tc == "Code commune":
                    return c[:2].upper() if len(c) >= 2 else c.upper()
                # Fallback
                return c[:2] if len(c) >= 2 else c

            if dept_filter:
                all_rows = [
                    r for r in all_rows
                    if _normalize_dept(r.get("lieu_code"), r.get("lieu_type")) in dept_filter
                ]

            # 5. Trier l'ensemble des résultats
            all_rows.sort(key=lambda r: r.get("dateNotification", "") or "", reverse=True)
            logger.info("DECP query: %d rows after dept filter, query=%s year=%s", len(all_rows), query, year)

            if not all_rows:
                msg = f"Aucun marché notifié trouvé pour *{query}*"
                if year:
                    msg += f" en {year}"
                if dept_filter:
                    msg += f" (départements : {', '.join(dept_filter)})"
                msg += "."
                return msg

            # 5. Filtrage PME post-traitement
            rows = all_rows
            pme_skipped = False
            if pme_only:
                siren_set: set[str] = set()
                for row in rows:
                    siren_set.update(_extract_sirens(row.get("titulaires")))
                logger.info("DECP query: %d unique SIREN extracted for PME check", len(siren_set))

                if len(siren_set) > 60:
                    logger.warning("Trop de SIREN à vérifier (%d), filtre PME désactivé", len(siren_set))
                    rows = rows[:max_results]
                    pme_skipped = True
                else:
                    pme_sirens: set[str] = set()
                    sem = asyncio.Semaphore(10)

                    async def _check_pme(siren: str) -> str | None:
                        cached = _is_pme_cached(str(settings.cache_dir), siren)
                        if cached is True:
                            return siren
                        if cached is False:
                            return None
                        async with sem:
                            try:
                                async with httpx.AsyncClient(
                                    timeout=3, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}
                                ) as c:
                                    eresp = await c.get(
                                        f"https://recherche-entreprises.api.gouv.fr/search?q={siren}"
                                    )
                                    if eresp.status_code == 429:
                                        await asyncio.sleep(1.0)
                                        eresp = await c.get(
                                            f"https://recherche-entreprises.api.gouv.fr/search?q={siren}"
                                        )
                                    eresp.raise_for_status()
                                    edata = eresp.json()
                                    if edata.get("results"):
                                        tranche = edata["results"][0].get("siege", {}).get("tranche_effectif_salarie")
                                        if tranche == "NN" or tranche is None:
                                            _save_pme_cache(str(settings.cache_dir), siren, False)
                                            return None
                                        is_pme_flag = int(tranche) <= 32
                                        _save_pme_cache(str(settings.cache_dir), siren, is_pme_flag)
                                        if is_pme_flag:
                                            return siren
                            except Exception:
                                logger.warning("Vérification PME échouée pour SIREN %s", siren, exc_info=False)
                        return None

                    pme_results = await asyncio.gather(*[_check_pme(s) for s in siren_set])
                    pme_sirens = {r for r in pme_results if r}
                    logger.info("DECP query: %d SIREN identified as PME out of %d", len(pme_sirens), len(siren_set))

                    filtered_rows = []
                    for row in rows:
                        if _row_has_pme_siren(row, pme_sirens):
                            filtered_rows.append(row)
                    rows = filtered_rows[:max_results]
            else:
                rows = rows[:max_results]

            if not rows:
                msg = f"Aucun marché notifié trouvé pour *{query}*"
                if year:
                    msg += f" en {year}"
                if pme_only:
                    msg += " attribué à une PME"
                if dept_filter:
                    msg += f" (départements : {', '.join(dept_filter)})"
                msg += "."
                return msg

            source_label = f"DECP {year}" + (" (fichiers mensuels)" if is_monthly else "")
            lines = [
                f"**{len(rows)} marchés publics notifiés** pour *{query}* "
            ]
            if year:
                lines[0] += f"en {year} "
            if pme_only:
                lines[0] += "(PME) "
            lines[0] += f"| Source : {source_label}\n"
            if pme_skipped:
                lines.append(
                    "⚠️ Filtre PME non appliqué : trop d'entreprises à vérifier en temps réel. "
                    "Ces résultats incluent des entreprises de toutes tailles.\n"
                )

            for i, r in enumerate(rows, 1):
                obj = str(r.get("objet", "Sans titre"))[:140]
                mnt = r.get("montant", "")
                dt = r.get("dateNotification", "")
                ach = r.get("acheteur_id", "SIRET inconnu")
                lieu = r.get("lieu_code", "")
                tits = r.get("titulaires", [])
                tit_id = ""
                if isinstance(tits, list) and tits:
                    t0 = tits[0]
                    if isinstance(t0, dict) and "titulaire" in t0:
                        tit_id = t0["titulaire"].get("id", "")
                    elif isinstance(t0, dict):
                        tit_id = t0.get("id", "")
                elif isinstance(tits, dict):
                    tit_id = tits.get("id", "")

                lines.append(f"{i}. {obj}")
                if mnt:
                    lines.append(f"   • Montant : {mnt:,.0f} €".replace(",", " "))
                if dt:
                    lines.append(f"   • Notifié le : {dt}")
                if lieu:
                    lines.append(f"   • Département : {lieu}")
                lines.append(f"   • Acheteur (SIRET) : {ach}")
                if tit_id:
                    lines.append(f"   • Attribué à (SIRET) : {tit_id}")
                lines.append("")

            return "\n".join(lines)

        except (DownloadError, AnalysisError) as exc:
            logger.warning("DECP query échouée: %s", exc)
            return f"ERREUR: {exc}"
        except Exception as exc:
            logger.exception("Erreur inattendue query_notified_contracts")
            return f"ERREUR inattendue: {exc}"

    if name in ("fetch_boamp_tenders", "search_public_tenders"):
        query = (arguments.get("query") or "").strip()
        perimeter = (arguments.get("perimeter") or "").strip()
        published_since = (arguments.get("published_since") or "").strip()
        if not query:
            return "ERREUR: requête de recherche manquante."

        try:
            results, nhits, since_date = await fetch_boamp_tenders(
                query, perimeter, published_since, arguments.get("related_terms")
            )
        except Exception as exc:
            logger.exception("Erreur recherche appels d'offre BOAMP")
            return f"ERREUR recherche AO: {exc}"

        # Sauvegarder les résultats bruts dans le cache du thread pour les graphiques futurs
        if results and context:
            thread_ts = context.get("thread_ts", "")
            logger.info("Sauvegarde AO dans cache thread_ts=%s cache_dir=%s", thread_ts, settings.cache_dir)
            if thread_ts:
                save_search_results(
                    settings.cache_dir,
                    f"Appels d'offre {query}",
                    results,
                    metadata={"thread_ts": thread_ts, "timestamp": int(time.time())}
                )
                logger.info("Sauvegarde AO OK — %s résultats", len(results))
            else:
                logger.warning("Sauvegarde AO SKIPPÉE — thread_ts vide dans context")

        # --- Enrichissement historique acheteur (top 3 uniquement, timeout global 15s) ---
        if results:
            from app.slack.buyer_history import fetch_buyer_history, format_buyer_history
            try:
                enrich_tasks = [
                    asyncio.wait_for(fetch_buyer_history(p["org"], "2025", settings), timeout=10)
                    for p in results[:3]
                ]
                enrich_results = await asyncio.gather(*enrich_tasks, return_exceptions=True)
                for i, hist in enumerate(enrich_results):
                    if isinstance(hist, list) and hist:
                        results[i]["_history"] = format_buyer_history(hist)
            except Exception:
                logger.exception("Enrichissement historique acheteur échoué")

        if results:
            searched_terms = " / ".join(dict.fromkeys([query]))
            header = f"📋 *Appels d'offre — {searched_terms}*"
            sub = f"Source : BOAMP (bulletin officiel) | {len(results)} annonce(s) unique(s) trouvée(s)"
            if since_date:
                sub += f" depuis le {since_date.strftime('%d/%m/%Y')}"

            lines = [
                "[RÉSULTAT BRUT — NE PAS MODIFIER LE FORMAT]",
                header,
                sub,
                "",
            ]
            for i, p in enumerate(results[:10], 1):
                obj = p['objet'] or "—"
                org = p['org'] or "—"
                dp = str(p['dateparution'] or "—")
                dl_raw = str(p['datelimitereponse'] or "")
                if dl_raw:
                    try:
                        dl_date = datetime.fromisoformat(dl_raw.replace("Z", "+00:00"))
                        dl = dl_date.strftime("%d/%m/%Y")
                    except Exception:
                        dl = dl_raw
                else:
                    dl = "—"
                depts = ", ".join(str(d) for d in p['departements']) if p['departements'] else "—"
                idweb = p['idweb']
                url = p['url_avis']
                lines.append(f"**{i}. {obj}**")
                lines.append(f"   • Acheteur : {org}")
                lines.append(f"   • Publié le : {dp} | Limite : {dl}")
                lines.append(f"   • Département(s) : {depts}")
                lines.append(f"   • <{url}|Voir l'annonce {idweb} sur le BOAMP>")
                if p.get("_history"):
                    for hl in p["_history"].splitlines():
                        lines.append(f"   {hl}")
                lines.append("")

            org_counter = Counter(p["org"] for p in results if p["org"])
            dept_counter = Counter()
            for p in results:
                for d in p["departements"]:
                    dept_counter[str(d)] += 1

            lines.extend([
                "",
                "---",
                f"📊 *Résumé statistique sur les {len(results)} annonces*",
                "",
                "*Top acheteurs :*",
            ])
            for org, cnt in org_counter.most_common(15):
                lines.append(f"• {org} — {cnt} annonce(s)")

            if dept_counter:
                lines.extend(["", "*Répartition par département :*"])
                for dept, cnt in dept_counter.most_common(15):
                    lines.append(f"• {dept} — {cnt} annonce(s)")

            lines.append(
                "\nTu veux que j'affine sur un périmètre géographique particulier "
                "(région, département) ou sur un type de prestation spécifique ?"
            )
            formatted_text = "\n".join(lines)
            # Sauvegarder le texte formaté pour le court-circuit dans bot.py
            if thread_ts:
                save_search_results(
                    settings.cache_dir,
                    f"Appels d'offre {query}",
                    results,
                    metadata={"thread_ts": thread_ts, "timestamp": int(time.time()), "formatted_text": formatted_text}
                )
            return formatted_text

        msg = f"Aucun appel d'offre trouvé pour *{query}* sur le BOAMP"
        if since_date:
            msg += f" publié depuis le {since_date.strftime('%d/%m/%Y')}"
        msg += "."
        if nhits > 0:
            msg += f" (Le BOAMP compte {nhits} résultats bruts sans filtre temporel.)"
        msg += "\n\n"
        msg += (
            "Tu veux que j'élargisse la période de recherche ou que j'essaie "
            "avec des termes voisins (ex: 'informatique', 'logiciel', 'données') ?"
        )
        return msg

    if name == "download_and_analyze_resource":
        url = (arguments.get("url") or "").strip()
        sql_query = (arguments.get("sql_query") or "").strip()
        if not _is_safe_url(url):
            return "ERREUR: URL invalide, interdite ou non autorisée (protection SSRF)."
        try:
            path = await download_resource(url, settings.cache_dir, settings.max_download_mb)
            if sql_query:
                rows = await asyncio.wait_for(
                    asyncio.to_thread(
                        query_file,
                        Path(path),
                        sql_query,
                        settings.max_scan_rows,
                    ),
                    timeout=60,
                )
                # [SAUVEGARDE SQL] Stocker les résultats pour graphique/export futur
                if thread_ts and rows:
                    save_search_results(
                        settings.cache_dir,
                        f"analyse SQL: {sql_query[:80]}",
                        rows,
                        metadata={"thread_ts": thread_ts, "timestamp": int(time.time())}
                    )
                lines = [f"Résultats de la requête SQL ({len(rows)} lignes) :"]
                for i, row in enumerate(rows[:50], 1):
                    lines.append(f"{i}. {row}")
                if len(rows) > 50:
                    lines.append(f"... et {len(rows) - 50} lignes supplémentaires.")
                return "\n".join(lines)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        analyze_file,
                        Path(path),
                        settings.max_scan_rows,
                        settings.max_sample_rows,
                        url,
                    ),
                    timeout=60,
                )
                return summary_for_llm(result)
        except (DownloadError, AnalysisError) as exc:
            logger.warning("Analyse locale en échec pour %s: %s", url, exc)
            return f"ERREUR: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erreur inattendue d'analyse locale pour %s", url)
            return f"ERREUR inattendue pendant l'analyse locale: {exc}"

    if name == "search_enterprises":
        query = (arguments.get("query") or "").strip()
        if not query:
            return "ERREUR: terme de recherche manquant."

        requested = arguments.get("max_results", 25)
        per_page = min(requested, 25)
        max_total = min(requested, 75)  # [PAGINATION] Limite absolue pour rester fluide
        offset = max(0, arguments.get("offset", 0))
        all_results: list[dict] = []
        page = 1

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30), headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                while len(all_results) < max_total:
                    params = {
                        "q": query,
                        "per_page": per_page,
                        "page": page,
                    }
                    if arguments.get("code_postal"):
                        params["code_postal"] = arguments["code_postal"]
                    if arguments.get("departement"):
                        params["departement"] = arguments["departement"]
                    if arguments.get("commune"):
                        # L'API Recherche d'Entreprises attend un code INSEE, pas un nom
                        commune_val = str(arguments["commune"]).strip()
                        if commune_val.isdigit() and len(commune_val) == 5:
                            params["commune"] = commune_val
                        else:
                            try:
                                async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as geo_client:
                                    resp = await geo_client.get(
                                        "https://geo.api.gouv.fr/communes",
                                        params={"nom": commune_val, "fields": "code"},
                                    )
                                    resp.raise_for_status()
                                    comm_data = resp.json()
                                    if comm_data:
                                        params["commune"] = comm_data[0].get("code", "")
                            except Exception:
                                logger.warning("Géocodage commune échoué pour %s", commune_val, exc_info=False)
                    if arguments.get("section_naf"):
                        params["section_activite_principale"] = arguments["section_naf"]

                    async for attempt in AsyncRetrying(
                        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
                        stop=stop_after_attempt(3),
                        wait=wait_exponential(multiplier=1, min=1, max=8),
                    ):
                        with attempt:
                            response = await client.get(
                                "https://recherche-entreprises.api.gouv.fr/search",
                                params=params,
                            )
                            response.raise_for_status()
                    data = response.json()
                    results = data.get("results", [])
                    if not results:
                        break
                    all_results.extend(results)
                    if len(results) < per_page:
                        break
                    page += 1
                    if page > 5:  # Sécurité
                        break

                # [FILTRE DÉPARTEMENT] L'API Recherche Entreprises peut retourner des résultats hors zone
                if arguments.get("departement"):
                    dept = str(arguments["departement"]).strip()
                    all_results = [
                        r for r in all_results
                        if str(r.get("siege", {}).get("code_postal", "")).startswith(dept)
                    ]

                if not all_results:
                    return "Aucune entreprise trouvée pour cette recherche."

                # [EXTRACTION DIRIGEANTS] L'API Recherche d'Entreprises retourne déjà les dirigeants
                for r in all_results:
                    r["dirigeant"] = _format_dirigeant(r.get("dirigeants", []))

                # [PAGINATION] Sélectionner le slice à afficher à partir de l'offset
                display_slice = all_results[offset:offset + max_total]
                if not display_slice:
                    return f"Aucun résultat à afficher pour offset={offset} (total trouvé : {len(all_results)})."

                # [GÉOCODAGE] Enrichir TOUS les résultats avec lat/lon pour la cartographie complète
                geo_limit = len(all_results)
                to_geocode = all_results[:geo_limit]
                logger.info("[GÉOCODAGE] Début du géocodage de %d adresses (sur %d résultats)", len(to_geocode), len(all_results))
                sem = asyncio.Semaphore(10)
                async def _geo_one(addr: str):
                    async with sem:
                        try:
                            async for attempt in AsyncRetrying(
                                retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
                                stop=stop_after_attempt(3),
                                wait=wait_exponential(multiplier=1, min=1, max=8),
                            ):
                                with attempt:
                                    rgeo = await client.get(
                                        "https://api-adresse.data.gouv.fr/search/",
                                        params={"q": addr, "limit": 1},
                                        timeout=10,
                                    )
                                    rgeo.raise_for_status()
                                    feats = rgeo.json().get("features", [])
                                    if feats:
                                        coords = feats[0].get("geometry", {}).get("coordinates", [])
                                        if len(coords) >= 2:
                                            return float(coords[1]), float(coords[0])
                                    return None, None
                        except Exception as exc:
                            logger.warning("Géocodage échoué pour '%s': %s", addr, exc)
                        return None, None

                geo_tasks = []
                for r in to_geocode:
                    siege = r.get("siege", {})
                    addr_parts = [
                        siege.get("adresse") or "",
                        siege.get("code_postal") or "",
                        siege.get("libelle_commune") or "",
                    ]
                    addr = ", ".join(p for p in addr_parts if p)
                    geo_tasks.append(_geo_one(addr))
                geo_results = await asyncio.gather(*geo_tasks)
                geo_success = 0
                for i, r in enumerate(to_geocode):
                    lat, lon = geo_results[i]
                    if lat is not None:
                        r["_geo_lat"] = lat
                        r["_geo_lon"] = lon
                        geo_success += 1
                logger.info("[GÉOCODAGE] %d/%d adresses géocodées avec succès", geo_success, len(to_geocode))

                # [ENRICHISSEMENT DIRIGEANTS FALLBACK] Pour les rares cas où l'API initiale n'a pas le dirigeant
                enrich_sem = asyncio.Semaphore(10)
                async def _enrich_dirigeant(r: dict):
                    async with enrich_sem:
                        if r.get("dirigeant"):
                            return
                        siren = r.get("siren", "")
                        if siren and len(siren) == 9:
                            r["dirigeant"] = await _fetch_dirigeant(siren)
                await asyncio.gather(*[_enrich_dirigeant(r) for r in all_results])

                # [ENRICHISSEMENT TAVILY] Téléphone / email pour les 5 premières du slice (si clé configurée)
                if settings.tavily_api_key and len(display_slice) > 0:
                    try:
                        to_enrich = []
                        for r in display_slice[:min(5, len(display_slice))]:
                            siege = r.get("siege", {})
                            to_enrich.append({
                                "nom": r.get("nom_complet") or r.get("nom_raison_sociale", ""),
                                "ville": siege.get("libelle_commune", ""),
                                "adresse": siege.get("adresse", ""),
                                "siret": r.get("siret", ""),
                            })
                        enriched = await enrich_results_with_tavily(to_enrich, settings.tavily_api_key)
                        for i, r in enumerate(display_slice[:min(5, len(display_slice))]):
                            r["telephone"] = enriched[i].get("telephone", "")
                            r["email"] = enriched[i].get("email", "")
                    except Exception:
                        logger.exception("Échec enrichissement Tavily")

                # [SAUVEGARDE] Construire et persister TOUS les résultats (pas seulement le slice affiché)
                raw_results_all = []
                geo_points: list[dict] = []
                for r in all_results:
                    siege = r.get("siege", {})
                    nom = r.get("nom_complet") or r.get("nom_raison_sociale", "")
                    lat = r.get("_geo_lat")
                    lon = r.get("_geo_lon")
                    raw_results_all.append({
                        "nom": nom,
                        "siren": r.get("siren", ""),
                        "siret": r.get("siret", ""),
                        "adresse": siege.get("adresse", ""),
                        "code_postal": siege.get("code_postal", ""),
                        "ville": siege.get("libelle_commune", ""),
                        "activite_naf": r.get("activite_principale", ""),
                        "effectif": r.get("tranche_effectif_salarie", ""),
                        "latitude": lat or "",
                        "longitude": lon or "",
                        "dirigeant": r.get("dirigeant", ""),
                        "telephone": r.get("telephone", ""),
                        "email": r.get("email", ""),
                    })
                    if lat is not None and lon is not None:
                        geo_points.append({"lat": float(lat), "lon": float(lon), "label": nom})
                dept = arguments.get("departement", "")
                # [SAUVEGARDE GLOBALE UNIQUEMENT] search_enterprises est souvent appelé
                # comme enrichissement secondaire (ex: détail d'un acheteur d'AO).
                # Sauvegarder UNIQUEMENT en global évite d'écraser les données AO/dataset
                # du cache thread qui servent aux graphiques/exports.
                save_search_results(
                    settings.cache_dir, query, raw_results_all,
                    metadata={"timestamp": int(time.time()), "departement": dept}
                )
                logger.info("[SAUVEGARDE GLOBALE] %d résultats SIRENE sauvegardés", len(raw_results_all))
                # [ÉTAT ENRICHISSEMENT] Initialise l'état pour les lots d'enrichissement futurs
                enrich_key = thread_ts or "global_sirene"
                save_enrich_state(
                    settings.cache_dir, enrich_key, query,
                    next_offset=0, total=len(all_results)
                )

                total_msg = f"**{len(all_results)} entreprise(s) trouvée(s)**"
                start_idx = offset + 1
                end_idx = offset + len(display_slice)
                total_msg = f"**{len(all_results)} entreprise(s) trouvée(s)** — affichage des résultats **{start_idx} à {end_idx}**"
                if len(all_results) > max_total:
                    total_msg += f" (limité aux {max_total} premiers globalement)"

                # Nettoyage des caractères qui cassent le markdown
                def _md_cell(text: str) -> str:
                    return str(text).replace("|", "/").replace("\n", " ").replace("\r", " ").strip()

                # Format détaillé pour les petites listes (≤20), tableau compact pour les grandes
                # afin de ne pas saturer le contexte LLM
                results_slice = display_slice
                if len(results_slice) <= 20:
                    lines = []
                    for r in results_slice:
                        nom = _md_cell(r.get("nom_complet") or r.get("nom_raison_sociale", "Nom inconnu"))
                        siege = r.get("siege", {})
                        adresse = _md_cell(siege.get("adresse", "Adresse inconnue"))
                        cp = _md_cell(siege.get("code_postal", ""))
                        ville = _md_cell(siege.get("libelle_commune", ""))
                        siren = _md_cell(r.get("siren", "N/A"))
                        siret = _md_cell(r.get("siret") or r.get("siege", {}).get("siret", "N/A"))
                        if siren == "N/A" and len(siret) >= 9:
                            siren = siret[:9]
                        naf = _md_cell(r.get("activite_principale", "N/A"))
                        effectif = _md_cell(r.get("tranche_effectif_salarie", "Non précisé"))
                        lat = r.get("_geo_lat")
                        lon = r.get("_geo_lon")
                        coord_str = f"  📍 {lat:.5f}, {lon:.5f}\n" if lat and lon else ""
                        dirigeant = _md_cell(r.get("dirigeant", ""))
                        dirigeant_str = f"  👤 Dirigeant: {dirigeant}\n" if dirigeant else ""
                        telephone = _md_cell(r.get("telephone", ""))
                        tel_str = f"  📞 Tél: {telephone}\n" if telephone else ""
                        email = _md_cell(r.get("email", ""))
                        email_str = f"  ✉️ Email: {email}\n" if email else ""
                        lines.append(
                            f"• {nom} (SIREN: {siren} / SIRET: {siret})\n"
                            f"  Adresse: {adresse}, {cp} {ville}\n"
                            f"{coord_str}"
                            f"  Activité NAF: {naf}\n"
                            f"  Effectif: {effectif}\n"
                            f"{dirigeant_str}"
                            f"{tel_str}"
                            f"{email_str}"
                        )
                    geo_json_block = ""
                    if geo_points:
                        geo_json_block = "\n\n[POINTS_GEO_JSON]\n" + json.dumps(geo_points, ensure_ascii=False)
                    return "[RÉSULTAT BRUT — NE PAS MODIFIER]\n\n" + total_msg + " :\n\n" + "\n\n".join(lines) + geo_json_block
                else:
                    # Tableau compact avec coordonnées GPS pour permettre la cartographie
                    rows = ["| # | Nom | Adresse | SIRET | NAF | Latitude | Longitude |", "|---|---|---|---|---|---|---|"]
                    for idx, r in enumerate(results_slice, start=1):
                        nom = _md_cell((r.get("nom_complet") or r.get("nom_raison_sociale", "?"))[:35])
                        siege = r.get("siege", {})
                        adresse = _md_cell(f"{siege.get('adresse', '')}, {siege.get('code_postal', '')} {siege.get('libelle_commune', '')}"[:45])
                        siret = _md_cell(r.get("siret") or r.get("siege", {}).get("siret", "N/A"))
                        naf = _md_cell(r.get("activite_principale", "N/A"))
                        lat = r.get("_geo_lat")
                        lon = r.get("_geo_lon")
                        lat_str = f"{lat:.5f}" if lat else ""
                        lon_str = f"{lon:.5f}" if lon else ""
                        rows.append(f"| {idx} | {nom} | {adresse} | {siret} | {naf} | {lat_str} | {lon_str} |")
                    has_next = (offset + len(display_slice)) < len(all_results)
                    next_hint = ""
                    if has_next:
                        next_offset = offset + len(display_slice)
                        next_hint = f"\n*Résultats suivants : demande-moi `offset={next_offset}` pour voir la suite.*"
                    # NOTE : pas de question de confirmation (RÈGLE ZÉRO)
                    note = (
                        "\n\n📋 *Ces résultats peuvent être enrichis avec les dirigeants, téléphones et emails.* "
                        "Indique les numéros d'entreprises qui t'intéressent ou dis 'tous'."
                        f"{next_hint}"
                    )
                    geo_json_block = ""
                    if geo_points:
                        geo_json_block = "\n\n[POINTS_GEO_JSON]\n" + json.dumps(geo_points, ensure_ascii=False)
                    return "[RÉSULTAT BRUT — NE PAS MODIFIER]\n\n" + total_msg + " :\n\n" + "\n".join(rows) + note + geo_json_block
        except httpx.HTTPStatusError as exc:
            return f"ERREUR API: HTTP {exc.response.status_code}"
        except Exception as exc:
            logger.exception("Erreur recherche entreprises")
            return f"ERREUR: {exc}"

    if name == "get_company_leaders":
        siren_or_siret = (arguments.get("siren") or "").strip().replace(" ", "")
        siren = siren_or_siret[:9] if len(siren_or_siret) == 14 else siren_or_siret
        if not siren or len(siren) != 9:
            return "ERREUR: SIREN invalide (doit contenir exactement 9 chiffres)."

        fiche_url = f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"

        # Essayer l'API Annuaire des Entreprises d'abord
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10), headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                response = await client.get(
                    f"https://annuaire-entreprises.data.gouv.fr/api/dirigeants/{siren}"
                )
                response.raise_for_status()
                data = response.json()

            dirigeants = data.get("dirigeants", []) if isinstance(data, dict) else []
            if dirigeants:
                lines = [f"**Dirigeants de l'entreprise (SIREN: {siren})** :"]
                for d in dirigeants[:10]:
                    nom = d.get("nom", "Nom inconnu")
                    prenoms = d.get("prenoms", "")
                    role = d.get("role", "Dirigeant")
                    date_naissance = d.get("date_de_naissance", "")

                    identite = f"{prenoms} {nom}".strip()
                    info = f"• {identite} — {role}"
                    if date_naissance:
                        info += f" (né(e) {date_naissance})"
                    lines.append(info)

                lines.append(f"\nFiche complète : {fiche_url}")
                return "\n".join(lines)
        except Exception:
            logger.info("Fallback Tavily pour dirigeants SIREN %s (API officielle indisponible)", siren)

        # Fallback : recherche web via Tavily
        try:
            from tavily import TavilyClient

            api_key = getattr(settings, "tavily_api_key", "")
            if api_key:
                client = TavilyClient(api_key=api_key)
                query = f"dirigeant entreprise SIREN {siren}"
                tavily_response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.search, query, max_results=3, include_answer=True
                    ),
                    timeout=30,
                )

                results = tavily_response.get("results", [])
                answer = tavily_response.get("answer", "")

                lines = [
                    f"**Dirigeants SIREN {siren}** (via recherche web)",
                    f"Fiche officielle : {fiche_url}\n"
                ]

                if answer:
                    lines.append(f"**Réponse** : {answer}")

                if results:
                    lines.append("**Sources** :")
                    for i, r in enumerate(results, 1):
                        title = r.get("title", "Sans titre")
                        url = r.get("url", "")
                        content = r.get("content", "")[:400]
                        lines.append(f"{i}. {title}\n   {content}...\n   {url}")

                return "\n\n".join(lines)
        except Exception as exc:
            logger.warning("Tavily fallback échoué pour dirigeants SIREN %s: %s", siren, exc)

        # Si tout échoue, retourner au moins le lien
        return (
            f"Les dirigeants ne sont pas disponibles momentanément.\n\n"
            f"Tu peux consulter la fiche complète de l'entreprise ici :\n"
            f"{fiche_url}"
        )

    if name == "web_search":
        query = (arguments.get("query") or "").strip()
        if not query:
            return "ERREUR: requête de recherche manquante."

        max_results = min(arguments.get("max_results", 5), 10)

        # --- Tavily (premier choix) ---
        try:
            from tavily import TavilyClient

            api_key = getattr(settings, "tavily_api_key", "")
            if api_key:
                client = TavilyClient(api_key=api_key)
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.search, query, max_results=max_results, include_answer=True
                    ),
                    timeout=30,
                )

                results = response.get("results", [])
                answer = response.get("answer", "")

                lines = []
                if answer:
                    lines.append(f"**Réponse synthétique** : {answer}\n")

                if results:
                    lines.append("**Sources trouvées** (format Slack : <url|titre>) :")
                    for i, r in enumerate(results, 1):
                        title = r.get("title", "Sans titre")
                        url = r.get("url", "")
                        content = r.get("content", "")[:300]
                        lines.append(f"{i}. <{url}|{title}>\n   {content}...")
                else:
                    lines.append("Aucun résultat trouvé.")

                return "\n\n".join(lines)
        except Exception as exc:
            err_str = str(exc).lower()
            if "usage limit" in err_str or "quota" in err_str or "forbidden" in err_str:
                logger.warning("Tavily quota dépassé, fallback DuckDuckGo pour: %s", query)
            else:
                logger.exception("Erreur recherche Tavily")
                return f"ERREUR recherche web: {exc}"

        # --- Fallback DuckDuckGo (gratuit, sans clé) ---
        try:
            from ddgs import DDGS

            def _ddgs_search(q: str, limit: int):
                with DDGS() as ddgs:
                    return list(ddgs.text(q, max_results=limit))

            results = await asyncio.wait_for(
                asyncio.to_thread(_ddgs_search, query, max_results),
                timeout=30,
            )

            if not results:
                return "Aucun résultat trouvé (fallback DuckDuckGo)."

            lines = ["**Sources trouvées** (format Slack : <url|titre>) :"]
            for i, r in enumerate(results, 1):
                title = r.get("title", "Sans titre")
                url = r.get("href", "")
                content = r.get("body", "")[:300]
                lines.append(f"{i}. <{url}|{title}>\n   {content}...")
            return "\n\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur recherche DuckDuckGo")
            return f"ERREUR recherche web (fallback): {exc}"

    if name == "extract_web_tables":
        url = (arguments.get("url") or "").strip()
        table_index = arguments.get("table_index")
        max_rows = arguments.get("max_rows", 100)
        if not url:
            return "ERREUR: URL manquante."

        # Protection SSRF basique
        if not _is_safe_url(url):
            return "ERREUR: URL invalide (seuls http/https sont autorisés)."

        try:
            from io import StringIO

            import pandas as pd

            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; datagouv-agent/0.1)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text

            # pandas.read_html sur StringIO force le parsing HTML
            tables = pd.read_html(StringIO(html), flavor="lxml")
            if not tables:
                return "Aucun tableau HTML trouvé sur cette page."

            lines: list[str] = []
            for i, df in enumerate(tables):
                if table_index is not None and i != table_index:
                    continue
                df = df.head(max_rows)
                lines.append(f"**Tableau {i}** — {len(df)} lignes × {len(df.columns)} colonnes")
                lines.append(df.to_markdown(index=False))
                lines.append("")

            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur extraction tableaux web")
            return f"ERREUR: impossible d'extraire les tableaux: {exc}"

    if name == "query_dvf_stats":
        code_postal = (arguments.get("code_postal") or "").strip()
        departement = (arguments.get("departement") or "").strip()
        annee = (arguments.get("annee") or "").strip()
        voie = (arguments.get("voie") or "").strip()
        if not annee:
            annee = "2025"
        if not code_postal and not departement:
            return "ERREUR: code_postal ou departement obligatoire."
        try:
            result = await query_dvf_stats(
                code_postal=code_postal,
                departement=departement,
                annee=annee,
                voie=voie,
                cache_dir=settings.cache_dir,
            )
            return result
        except Exception as exc:
            logger.exception("Erreur DVF")
            return f"ERREUR DVF: {exc}"

    if name == "query_dvf_details":
        code_postal = (arguments.get("code_postal") or "").strip()
        departement = (arguments.get("departement") or "").strip()
        annee = (arguments.get("annee") or "").strip()
        voie = (arguments.get("voie") or "").strip()
        type_local = (arguments.get("type_local") or "Appartement").strip()
        limit = arguments.get("limit", 15)
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 15
        if not annee:
            annee = "2025"  # Année la plus récente par défaut pour les ventes récentes
        if not code_postal and not departement:
            return "ERREUR: code_postal ou departement obligatoire."
        try:
            result = await query_dvf_details(
                code_postal=code_postal,
                departement=departement,
                annee=annee,
                voie=voie,
                type_local=type_local,
                limit=limit,
                cache_dir=settings.cache_dir,
            )
            return result
        except Exception as exc:
            logger.exception("Erreur DVF détails")
            return f"ERREUR DVF détails: {exc}"

    if name == "query_dvf_trend":
        location = (arguments.get("location") or "").strip()
        annees_raw = arguments.get("annees", [])
        # Le LLM envoie parfois une string JSON au lieu d'une liste
        if isinstance(annees_raw, str):
            try:
                import json as _json
                annees = _json.loads(annees_raw)
            except Exception:
                annees = [a.strip().strip("'\"") for a in annees_raw.strip("[]").split(",") if a.strip()]
        else:
            annees = annees_raw
        type_local = (arguments.get("type_local") or "").strip()
        if not location or not annees:
            return "ERREUR: location et annees sont obligatoires."
        try:
            result = await query_dvf_trend(
                location=location,
                annees=annees,
                type_local=type_local,
                cache_dir=settings.cache_dir,
            )
            return result
        except Exception as exc:
            logger.exception("Erreur DVF trend")
            return f"ERREUR DVF trend: {exc}"

    if name == "query_sirene_api":
        return await _query_sirene_api(arguments, settings)

    # -----------------------------------------------------------------------
    # APIs externes gratuites (Phase 8)
    # -----------------------------------------------------------------------
    if name == "search_jobs":
        query = (arguments.get("query") or "").strip()
        commune = (arguments.get("commune") or "").strip()
        limit = min(arguments.get("limit", 5), 25)
        if not query:
            return "ERREUR: métier/query manquant."
        try:
            client = FranceTravailClient(settings.cache_dir)
            results = await client.search_jobs(query, code_commune=commune, limit=limit)
            if not results:
                return f"Aucune offre trouvée pour '{query}'" + (f" à {commune}." if commune else ".")
            lines = [f"💼 *Offres d'emploi France Travail — {query}* :\n"]
            for job in results:
                lines.append(format_job(job))
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur France Travail jobs")
            return f"ERREUR France Travail: {exc}"

    if name == "get_water_level":
        code_bss = (arguments.get("code_bss") or "").strip()
        bbox = (arguments.get("bbox") or "").strip()
        days = arguments.get("days", 30)
        try:
            client = HubEauClient(settings.cache_dir)
            results = await client.get_nappes_latest(bbox=bbox or None, code_bss=code_bss or None, days=days)
            if not results:
                return "Aucune mesure de nappe trouvée pour ces critères."
            lines = [f"💧 *Niveaux des nappes phréatiques* ({len(results)} mesures) :\n"]
            for m in results[:10]:
                lines.append(format_piezo(m))
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur Hub'Eau piézo")
            return f"ERREUR Hub'Eau: {exc}"

    if name == "search_legifrance":
        query = (arguments.get("query") or "").strip()
        code_name = (arguments.get("code_name") or "").strip()
        limit = min(arguments.get("limit", 5), 10)
        if not query:
            return "ERREUR: terme de recherche manquant."
        if not settings.legifrance_client_id or not settings.legifrance_client_secret:
            return (
                "ERREUR: API Legifrance non configurée. "
                "Crée un compte gratuit sur https://www.piste.gouv.fr et renseigne "
                "LEGIFRANCE_CLIENT_ID et LEGIFRANCE_CLIENT_SECRET dans le .env"
            )
        try:
            client = LegifranceClient(
                settings.legifrance_client_id,
                settings.legifrance_client_secret,
                settings.cache_dir,
            )
            results = await client.search_code(query, code_name=code_name, limit=limit)
            if not results:
                return f"Aucun article trouvé pour '{query}'" + (f" dans {code_name}." if code_name else ".")
            lines = [f"📜 *Résultats Legifrance — '{query}'* :\n"]
            for r in results:
                lines.append(format_legifrance_result(r))
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur Legifrance")
            return f"ERREUR Legifrance: {exc}"

    if name == "get_meteo":
        try:
            lat = float(arguments.get("lat", 0))
            lon = float(arguments.get("lon", 0))
        except (TypeError, ValueError):
            return "ERREUR: coordonnées GPS invalides."
        try:
            client = MeteoClient(settings.cache_dir)
            data = await client.get_forecast(lat, lon)
            return format_forecast(data)
        except Exception as exc:
            logger.exception("Erreur Météo France")
            return f"ERREUR Météo France: {exc}"

    if name == "get_building_rnb":
        address = (arguments.get("address") or "").strip()
        lat = arguments.get("lat")
        lon = arguments.get("lon")
        try:
            client = RNBClient(settings.cache_dir)
            results = await client.search_building(
                address=address,
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
            )
            if not results:
                return "Aucun bâtiment trouvé dans le RNB pour ces critères."
            lines = [f"🏗️ *Résultats RNB* ({len(results)} bâtiment(s)) :\n"]
            for b in results[:5]:
                lines.append(format_building(b))
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Erreur RNB")
            return f"ERREUR RNB: {exc}"

    if name == "electricity_consumption_by_region":
        year = (arguments.get("year") or "2023").strip()
        return await get_electricity_consumption_by_region(year, settings.cache_dir)

    if name == "traffic_summary":
        region = (arguments.get("region") or "").strip() or None
        year = (arguments.get("year") or "2024").strip()
        return await get_traffic_summary(region, year, settings.cache_dir)

    if name == "sncf_punctuality":
        depart = (arguments.get("depart") or "").strip() or None
        arrivee = (arguments.get("arrivee") or "").strip() or None
        return await get_sncf_punctuality(depart, arrivee, settings.cache_dir)

    if name == "sau_by_region":
        return await get_sau_by_region(settings.cache_dir)

    return f"ERREUR: tool local inconnu '{name}'."
