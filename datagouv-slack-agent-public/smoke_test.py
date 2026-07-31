import asyncio
import sys
from pathlib import Path

# --- 1. Test BOAMP (cybersécurité) ---
async def test_boamp():
    from app.agent.local_tools import fetch_boamp_tenders
    results, total, since = await fetch_boamp_tenders("cybersécurité")
    print(f"[BOAMP] cybersécurité : {len(results)} résultats (total annoncé {total})")
    if results:
        print(f"  Premier : {results[0].get('objet', 'N/A')[:60]}...")
    return len(results) > 0

# --- 2. Test DVF (Batignolles 2024) ---
async def test_dvf():
    from app.data.dvf import query_dvf_details, query_dvf_stats
    cache_dir = str(Path(__file__).parent / "cache")
    stats = await query_dvf_stats(departement="75", annee="2024", voie="batignolles", cache_dir=cache_dir)
    print(f"[DVF Stats] {stats[:200]}...")
    details = await query_dvf_details(departement="75", annee="2024", voie="batignolles", cache_dir=cache_dir, limit=5)
    print(f"[DVF Details] {details[:300]}...")
    return "transactions" in details.lower() or "€" in details

# --- 3. Test SIRENE (Astek) ---
async def test_sirene():
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get("https://recherche-entreprises.api.gouv.fr/search", params={"q": "Astek", "per_page": 5})
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
    print(f"[SIRENE] Astek : {len(results)} résultats")
    if results and isinstance(results[0], dict):
        r = results[0]
        nom = r.get('nom_complet') or r.get('nom_raison_sociale') or 'N/A'
        siren = r.get('siren', 'N/A')
        naf = (r.get('activite_principale') or {}).get('code', 'N/A') if isinstance(r.get('activite_principale'), dict) else 'N/A'
        print(f"  Premier : {nom} | SIREN {siren} | NAF {naf}")
    return len(results) > 0 and any(isinstance(r, dict) and r.get("siren") for r in results)

async def main():
    ok = True
    try:
        ok &= await test_boamp()
    except Exception as e:
        print(f"[BOAMP ERREUR] {e}")
        ok = False
    try:
        ok &= await test_dvf()
    except Exception as e:
        print(f"[DVF ERREUR] {e}")
        ok = False
    try:
        ok &= await test_sirene()
    except Exception as e:
        print(f"[SIRENE ERREUR] {e}")
        ok = False
    print("\n" + ("✅ SMOKE TEST OK" if ok else "❌ SMOKE TEST ÉCHOUÉ"))
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    asyncio.run(main())
