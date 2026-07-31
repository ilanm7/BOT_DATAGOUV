"""Orchestrateur de l'agent.

Boucle agentique maison : le LLM reçoit les tools MCP data.gouv.fr ET les
tools locaux (analyse de fichiers), demande des appels, l'orchestrateur les
route vers le bon exécuteur, réinjecte les résultats, et boucle jusqu'à une
réponse finale (ou la limite d'itérations, avec conclusion forcée).
"""

import asyncio
import json
import logging
from typing import Any

from app.agent.local_tools import LOCAL_TOOL_NAMES, LOCAL_TOOLS, execute_local_tool
from app.agent.prompts import SYSTEM_PROMPT
from app.config import Settings
from app.llm.client import LLMClient
from app.mcp.datagouv_mcp_client import DataGouvMCPClient, MCPToolError

logger = logging.getLogger(__name__)

# [SÉMAPHORE] Limite à 5 traitements simultanés pour maîtriser les coûts OpenAI
_MAX_CONCURRENT_RUNS = 5
_orchestrator_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)


class AgentOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = LLMClient(settings)
        self.mcp = DataGouvMCPClient(settings.datagouv_mcp_url)

    async def run(
        self,
        conversation: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        async with _orchestrator_semaphore:
            return await self._run(conversation, context)

    async def _run(
        self,
        conversation: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        """[BOUCLE AGENTIQUE] Boucle agentique : LLM → tool call → exécution → réinjection → reboucle"""
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

        has_principal = any(
            "[DATASET_PRINCIPAL:" in (msg.get("content") or "")
            for msg in conversation
        )
        # [PRIORITÉ AO / DVF] Si la conversation contient une consigne absolue pour les appels
        # d'offre ou pour les données DVF, le dataset principal ne doit pas distraire le LLM.
        has_ao_priority = any(
            "[CONSIGNE SYSTÈME ABSOLUE" in (msg.get("content") or "")
            and ("appels d'offre" in (msg.get("content") or "").lower() or "marchés publics" in (msg.get("content") or "").lower())
            for msg in conversation
        )
        has_dvf_priority = any(
            "[CONSIGNE SYSTÈME ABSOLUE" in (msg.get("content") or "")
            and ("prix au m²" in (msg.get("content") or "").lower() or "transactions" in (msg.get("content") or "").lower() or "dvf" in (msg.get("content") or "").lower() or "valeur foncière" in (msg.get("content") or "").lower())
            for msg in conversation
        )
        if has_principal and not has_ao_priority and not has_dvf_priority:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[CONSIGNE SYSTÈME] Un DATASET_PRINCIPAL est défini dans cette conversation. "
                        "Tu dois d'abord l'analyser avec get_dataset_info ou list_dataset_resources. "
                        "Ensuite, et SEULEMENT ensuite, si ce dataset ne convient pas, tu fais UNE "
                        "recherche ciblée (search_datasets) pour trouver 1-3 alternatives que tu "
                        "présenteras numérotées #2, #3…"
                    ),
                }
            )

        messages.extend(self._truncate(conversation))

        # [FALLBACK MCP] Ouvrir la session MCP pour toute la durée de la boucle.
        # Si le MCP est down, on continue avec session=None (tools locaux uniquement).
        tools = list(LOCAL_TOOLS)
        try:
            async with self.mcp.session() as session:
                mcp_tools = await self.mcp.list_tools_openai_format(session)
                # [DÉDUPLICATION] Les outils locaux priment sur les outils MCP en cas de collision de noms
                local_names = LOCAL_TOOL_NAMES
                mcp_tools_filtered = [t for t in mcp_tools if t.get("function", {}).get("name") not in local_names]
                tools = mcp_tools_filtered + LOCAL_TOOLS
                return await self._agent_loop(messages, tools, conversation, context, session, has_principal)
        except Exception:
            logger.exception("MCP data.gouv indisponible, fallback sur tools locaux")
            local_tool_names = ", ".join(t["function"]["name"] for t in LOCAL_TOOLS)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[CONSIGNE SYSTÈME] ERREUR: Connexion au serveur MCP data.gouv.fr impossible. "
                        "Les tools MCP ne sont pas accessibles pour cette requête. "
                        f"Tu ne peux utiliser que les tools locaux ({local_tool_names}). "
                        "Explique brièvement à l'utilisateur que le catalogue data.gouv est "
                        "momentanément inaccessible, mais que tu peux l'aider avec les outils locaux."
                    ),
                }
            )
            return await self._agent_loop(messages, tools, conversation, context, None, has_principal)

    async def _agent_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        _conversation: list[dict[str, str]],
        context: dict[str, Any] | None,
        session: Any | None,
        has_principal: bool,
    ) -> str:
        """Boucle agentique proprement dite, exécutée dans une session MCP ouverte."""
        # [ANTI-BOUCLE] Compteurs séparés pour forcer l'engagement sur le premier
        # dataset trouvé, tout en permettant un fallback web limité.
        dataset_search_count = 0
        max_dataset_search = 3 if has_principal else 1
        web_search_count = 0
        max_web_search = 2
        for iteration in range(self.settings.agent_max_iterations):
            assistant_msg = await self.llm.chat(messages, tools=tools)

            if not assistant_msg.tool_calls:
                return (assistant_msg.content or "").strip() or "(réponse vide du modèle)"

            dataset_search_count += sum(
                1 for tc in assistant_msg.tool_calls if tc.function.name == "search_datasets"
            )
            web_search_count += sum(
                1 for tc in assistant_msg.tool_calls if tc.function.name in ("web_search", "extract_web_tables")
            )
            if dataset_search_count > max_dataset_search or web_search_count > max_web_search:
                logger.warning(
                    "Trop de recherches — datasets=%d (max %d), web=%d (max %d), conclusion forcée",
                    dataset_search_count, max_dataset_search, web_search_count, max_web_search
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[CONSIGNE SYSTÈME] Tu as déjà effectué plusieurs recherches. "
                            "Il est temps de synthétiser et de répondre à l'utilisateur avec "
                            "les résultats déjà obtenus, sans lancer de nouvelle recherche."
                        ),
                    }
                )
                final_msg = await self.llm.chat(messages)
                return final_msg.content or (
                    "J'ai exploré plusieurs pistes. Peux-tu préciser ta demande ?"
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in assistant_msg.tool_calls
                    ],
                }
            )

            search_datasets_used_this_iteration = False
            for tool_call in assistant_msg.tool_calls:
                name = (tool_call.function.name or "").strip()
                if not name:
                    logger.warning("Tool call sans nom ignoré")
                    continue
                tc_id = tool_call.id or f"missing_{iteration}"
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                logger.info("Itération %d, appel tool %s(%s)", iteration, name, self._safe_args(arguments))

                result = await self._dispatch(session, name, arguments, context, messages)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": self._cap(result),
                    }
                )

                if name == "search_datasets":
                    search_datasets_used_this_iteration = True

                # [MODE RÉFLEXION] Court-circuits désactivés pour forcer le passage par le LLM
                # sur TOUTES les réponses, y compris les petites listes et les tableaux DVF.
                # Cela évite les réponses génériques codées en dur et permet au LLM d'adapter
                # le ton, le format et les détails au contexte de la conversation.
                #
                # if single_tool and name == "search_enterprises" and "[RÉSULTAT BRUT" in result:
                #     match = re.search(r"(\d+)\s+entreprise\(s\)\s+trouvée", result)
                #     if match and int(match.group(1)) > 5:
                #         logger.info("search_enterprises retourne %s résultats — pas de court-circuit", match.group(1))
                #     else:
                #         logger.info("Court-circuit search_enterprises — résultat retourné brut")
                #         return result
                #
                # if single_tool and name == "query_dvf_details" and "| Date |" in result:
                #     logger.info("Court-circuit query_dvf_details — résultat retourné brut")
                #     return result

            # [ANTI-WINDOW-SHOPPING] Après un search_datasets, retirer le tool pour forcer l'engagement
            if search_datasets_used_this_iteration:
                tools = [t for t in tools if t.get("function", {}).get("name") != "search_datasets"]
                logger.info("Tool search_datasets retiré des outils disponibles — engagement forcé")

        logger.warning("Limite d'itérations atteinte, conclusion forcée")
        messages.append(
            {
                "role": "user",
                "content": (
                    "[CONSIGNE SYSTÈME] Limite d'étapes atteinte. Rédige maintenant "
                    "la meilleure réponse possible avec les informations déjà "
                    "collectées ci-dessus. Indique clairement ce que tu n'as pas pu "
                    "vérifier et propose une étape suivante à l'utilisateur."
                ),
            }
        )
        final_msg = await self.llm.chat(messages)
        return final_msg.content or (
            "Je n'ai pas pu aboutir. Peux-tu reformuler ou préciser ta demande ?"
        )

    @staticmethod
    def _safe_args(arguments: dict[str, Any]) -> str:
        """[MASQUAGE PII] Masque les valeurs sensibles pour les logs (PII, tokens, SIREN...)"""
        if not arguments:
            return "{}"
        safe: dict[str, str] = {}
        for k, v in arguments.items():
            s = str(v)
            lower_k = k.lower()
            if any(sk in lower_k for sk in ("token", "key", "password", "secret", "email", "mail", "tel", "phone", "siren", "siret", "api_key")):
                safe[k] = "***"
            elif len(s) > 120:
                safe[k] = s[:120] + "..."
            else:
                safe[k] = s
        return str(safe)

    async def _dispatch(
        self,
        session,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """Route un appel de tool vers l'exécuteur local ou MCP."""
        # Blocage dataset DECP interdit via MCP
        if name in ("get_dataset_info", "list_dataset_resources"):
            dataset_id = arguments.get("dataset_id") or arguments.get("dataset") or ""
            if "donnees-essentielles-de-la-commande-publique" in str(dataset_id).lower():
                return (
                    "ERREUR: Ce dataset est géré exclusivement par le tool local "
                    "'query_notified_contracts'. N'utilise PAS 'get_dataset_info' ou "
                    "'list_dataset_resources' sur ce dataset. Appelle DIRECTEMENT "
                    "'query_notified_contracts' pour les marchés déjà notifiés."
                )
        # [BLOCAGE ENTREPRISE SPÉCIFIQUE] Forcer web_search avant search_enterprises
        # quand la consigne entreprise est active et qu'il n'y a pas de filtres discriminants
        if name == "search_enterprises" and messages:
            has_company_directive = any(
                "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande les informations d'UNE entreprise" in (msg.get("content") or "")
                for msg in messages
            )
            if has_company_directive:
                query = (arguments.get("query") or "").strip()
                has_dept = bool(arguments.get("departement"))
                has_cp = bool(arguments.get("code_postal"))
                has_naf = bool(arguments.get("section_naf"))
                # Si c'est un nom simple (≤2 mots) sans filtres → bloquer
                if len(query.split()) <= 2 and not has_dept and not has_cp and not has_naf:
                    return (
                        "ERREUR STRATÉGIQUE: Tu as appelé 'search_enterprises' avec un nom "
                        "d'entreprise trop générique et sans filtres discriminants. "
                        "Cela retournerait une liste inutilisable d'homonymes. "
                        "Tu DOIS d'abord appeler 'web_search' pour trouver le siège social, "
                        "le secteur d'activité, le SIREN ou tout lien avec Iguane/IG1. "
                        "Une fois les indices collectés, rappelle 'search_enterprises' "
                        "avec les filtres (departement, code_postal, NAF)."
                    )
        if name in LOCAL_TOOL_NAMES:
            # [TIMEOUT ADAPTATIF] extract_web_tables peut bloquer sur des pages lourdes
            timeout = 15 if name == "extract_web_tables" else 30
            return await asyncio.wait_for(
                execute_local_tool(name, arguments, self.settings, context),
                timeout=timeout,
            )
        if session is None:
            return (
                "ERREUR: Le serveur MCP data.gouv.fr est temporairement indisponible. "
                "Ce tool n'est pas accessible pour le moment."
            )
        try:
            return await asyncio.wait_for(
                DataGouvMCPClient.call_tool(session, name, arguments),
                timeout=10,
            )
        except TimeoutError:
            logger.warning("Timeout MCP sur %s(%s)", name, arguments)
            return (
                f"ERREUR: Le tool MCP '{name}' a dépassé 10 secondes de réponse. "
                "Le serveur MCP est lent ou le dataset est très volumineux. "
                "Passe à une autre approche."
            )
        except MCPToolError as exc:
            return f"ERREUR: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erreur inattendue sur tool MCP %s", name)
            return f"ERREUR: {exc}"

    def _truncate(self, conversation: list[dict[str, str]]) -> list[dict[str, str]]:
        """[TRONCATURE] Tronque l'historique en gardant les messages les plus récents."""
        budget = self.settings.llm_context_max_chars
        kept: list[dict[str, str]] = []
        total = 0
        for msg in reversed(conversation):
            content = msg["content"]
            if len(content) > budget:
                content = content[:budget] + "\n... [tronqué par l'orchestrateur] ..."
            total += len(content)
            if total > budget and kept:
                break
            kept.append({"role": msg["role"], "content": content})
        return list(reversed(kept))

    def _cap(self, text: str, limit: int = 15_000) -> str:
        """Limite la taille d'un résultat de tool injecté dans le contexte."""
        # Les résultats bruts (search_enterprises) méritent un cap plus élevé
        # pour préserver le header [RÉSULTAT BRUT] et le court-circuit.
        if "[RÉSULTAT BRUT" in text[:120]:
            limit = 25_000
        if len(text) <= limit:
            return text
        return text[:limit] + "\n\n[... résultat tronqué par l'orchestrateur ...]"
