"""Message de bienvenue interactif pour les nouveaux utilisateurs."""

from pathlib import Path

WELCOME_TEXT = """👋 *Bienvenue sur DATAGOUV--AGENT !*

Je suis ton *intelligence augmentée* pour les marchés publics et les données ouvertes.

*Ce que je fais pour toi :*

🔍 *Appels d'offre* — Je surveille le BOAMP et te pousse uniquement les AO qui matchent ton profil (scoring IA 0-100).

📊 *Analyse acheteur* — Pour chaque AO, je croise l'historique DECP + SIRENE pour te dire : "Ce marché est souvent gagné par des PME locales de 20 salariés".

⏰ *Rappels automatiques* — Réagis avec ✅ ou 🔥 sur un push AO → je te rappelle à J-15, J-7 et J-2 pour ne jamais rater une deadline.

📄 *Lecture du DCE* — Envoie-moi le PDF du cahier des charges, je te dis en 10 secondes quels sont les critères de notation et les clauses pièges.

*3 commandes pour démarrer :*

1️⃣ `/datagouv demo` → Je te montre en 2 min comment je fonctionne sur un vrai marché public
2️⃣ `/datagouv profil activité=IT taille=PME zones=75,92` → Configure ton scoring
3️⃣ `/datagouv ao alerte cybersécurité` → Alerte proactive chaque matin

_Besoin d'aide :_ `/datagouv help`
_Dashboard :_ <http://localhost:8080|Voir tes stats en temps réel>
"""


def has_been_welcomed(cache_dir: str, user_id: str) -> bool:
    """Vérifie si l'utilisateur a déjà reçu le message de bienvenue."""
    return (Path(cache_dir) / "welcomed" / f"{user_id}.txt").exists()


def mark_welcomed(cache_dir: str, user_id: str) -> None:
    """Marque l'utilisateur comme ayant reçu le message de bienvenue."""
    path = Path(cache_dir) / "welcomed"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{user_id}.txt").write_text("")


async def send_welcome(client, channel: str, user_id: str | None = None) -> None:
    """Envoie le message de bienvenue dans un canal ou DM."""
    text = WELCOME_TEXT
    if user_id:
        text = f"<@{user_id}> " + text
    await client.chat_postMessage(channel=channel, text=text)
