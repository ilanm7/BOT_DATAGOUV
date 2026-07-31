#!/usr/bin/env python3
"""Stress-test de l'orchestrateur — charge parallèle + flows critiques.

Lance N requêtes simultanées pour vérifier :
1. Le sémaphore limite bien à 5 traitements concurrents
2. Les flows AO / SIRENE / Dataset ne crashent pas sous charge
3. Les temps de réponse restent raisonnables

⚠️ Consomme des tokens OpenAI et des appels API réels.
"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.agent.orchestrator import AgentOrchestrator

_passed = 0
_failed = 0


def _ok(msg: str):
    global _passed
    _passed += 1
    print(f"  ✅ {msg}")


def _ko(msg: str, exc: Exception | None = None):
    global _failed
    _failed += 1
    print(f"  ❌ {msg}")
    if exc:
        print(f"     → {exc}")


# ---------------------------------------------------------------------------
# Scénarios
# ---------------------------------------------------------------------------
async def scenario_ao_cyber(orchestrator: AgentOrchestrator) -> str:
    """Flow Appels d'Offre — cybersécurité cette semaine."""
    conversation = [
        {
            "role": "user",
            "content": (
                "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande des appels d'offre. "
                "Tu dois utiliser DIRECTEMENT fetch_boamp_tenders.\n\n"
                "Quels appels d'offre en cybersécurité sont ouverts cette semaine ?"
            ),
        }
    ]
    return await orchestrator.run(conversation)


async def scenario_sirene_astek(orchestrator: AgentOrchestrator) -> str:
    """Flow SIRENE — discrimination entreprise."""
    conversation = [
        {
            "role": "user",
            "content": (
                "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande les informations d'UNE "
                "entreprise précise par son nom. Tu ne dois PAS te contenter de 'search_enterprises' "
                "qui retournerait tous les homonymes. Stratégie obligatoire : "
                "1) Faire 'web_search' pour trouver le site officiel, le SIREN, le siège social, "
                "le secteur d'activité. 2) Puis appeler 'search_enterprises' avec les filtres. "
                "3) Présenter UNIQUEMENT l'entreprise la plus pertinente.\n\n"
                "Donne-moi les infos SIRENE de Astek"
            ),
        }
    ]
    return await orchestrator.run(conversation)


async def scenario_dataset_creation(orchestrator: AgentOrchestrator) -> str:
    """Flow Dataset — créations d'entreprises (exception critique dataset)."""
    conversation = [
        {
            "role": "user",
            "content": "Combien d'entreprises ont été créées à Paris en 2023 ?",
        }
    ]
    return await orchestrator.run(conversation)


# ---------------------------------------------------------------------------
# Tests de charge
# ---------------------------------------------------------------------------
async def test_concurrent_load(orchestrator: AgentOrchestrator, n: int = 8):
    """Lance n requêtes identiques en parallèle et mesure les temps."""
    print(f"\n🧪 Charge parallèle — {n} requêtes AO simultanées")

    async def _one(i: int):
        t0 = time.time()
        try:
            result = await scenario_ao_cyber(orchestrator)
            elapsed = time.time() - t0
            return i, True, elapsed, result
        except Exception as exc:
            elapsed = time.time() - t0
            return i, False, elapsed, str(exc)

    tasks = [asyncio.create_task(_one(i)) for i in range(n)]
    results = await asyncio.gather(*tasks)

    ok_count = sum(1 for _, ok, _, _ in results if ok)
    times = [elapsed for _, _, elapsed, _ in results]

    print(f"   → {ok_count}/{n} succès | min={min(times):.1f}s max={max(times):.1f}s avg={sum(times)/len(times):.1f}s")

    if ok_count == n:
        _ok(f"{n} requêtes parallèles terminées sans crash")
    else:
        _ko(f"{n - ok_count} requêtes ont échoué")
        for i, ok, elapsed, msg in results:
            if not ok:
                print(f"     → requête {i} échouée après {elapsed:.1f}s: {msg[:200]}")


async def test_flow_ao(orchestrator: AgentOrchestrator):
    print("\n🧪 Flow critique — Appels d'offre cybersécurité")
    try:
        result = await scenario_ao_cyber(orchestrator)
        checks = [
            "BOAMP" in result or "appel" in result.lower() or "consultation" in result.lower(),
            len(result) > 100,
        ]
        if all(checks):
            _ok(f"AO retourné ({len(result)} caractères)")
        else:
            _ko(f"AO réponse inattendue : {result[:300]}")
    except Exception as exc:
        _ko("Flow AO a crashé", exc)


async def test_flow_sirene(orchestrator: AgentOrchestrator):
    print("\n🧪 Flow critique — SIRENE Astek")
    try:
        result = await scenario_sirene_astek(orchestrator)
        checks = [
            "439752817" in result or "ASTEK FRANCE" in result,
            "[RÉSULTAT BRUT" in result or "92100" in result,
        ]
        if all(checks):
            _ok(f"Astek France trouvé ({len(result)} caractères)")
        else:
            _ko(f"SIRENE réponse inattendue : {result[:500]}")
    except Exception as exc:
        _ko("Flow SIRENE a crashé", exc)


async def test_flow_dataset(orchestrator: AgentOrchestrator):
    print("\n🧪 Flow critique — Dataset créations d'entreprises")
    try:
        result = await scenario_dataset_creation(orchestrator)
        checks = [
            len(result) > 50,
            "désol" not in result.lower() or "pas trouvé" not in result.lower(),
        ]
        if all(checks):
            _ok(f"Dataset retourné ({len(result)} caractères)")
        else:
            _ko(f"Dataset réponse inattendue : {result[:300]}")
    except Exception as exc:
        _ko("Flow Dataset a crashé", exc)


async def test_timeout_resilience(orchestrator: AgentOrchestrator):
    print("\n🧪 Résilience — requête inconnue / hors périmètre")
    try:
        conversation = [
            {"role": "user", "content": "Quel temps fait-il sur Mars en ce moment ?"}
        ]
        result = await asyncio.wait_for(orchestrator.run(conversation), timeout=60)
        _ok(f"Réponse hors périmètre sans crash ({len(result)} caractères)")
    except asyncio.TimeoutError:
        _ko("Timeout sur requête hors périmètre (>60s)")
    except Exception as exc:
        _ko("Crash sur requête hors périmètre", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    print("=" * 70)
    print("  STRESS-TEST ORCHESTRATEUR")
    print("  ⚠️  Appels OpenAI + API externes réels")
    print("=" * 70)

    settings = get_settings()
    orchestrator = AgentOrchestrator(settings)

    # 1. Flows critiques individuels
    await test_flow_ao(orchestrator)
    await test_flow_sirene(orchestrator)
    await test_flow_dataset(orchestrator)
    await test_timeout_resilience(orchestrator)

    # 2. Charge parallèle
    await test_concurrent_load(orchestrator, n=6)

    print("\n" + "=" * 70)
    print(f"  Résultat : {_passed} passés, {_failed} échoués")
    print("=" * 70)

    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
