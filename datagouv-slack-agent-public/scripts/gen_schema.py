import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import os

fig, ax = plt.subplots(1, 1, figsize=(14, 10))
ax.set_xlim(0, 14)
ax.set_ylim(0, 10)
ax.axis('off')
fig.patch.set_facecolor('white')

# Title
ax.text(7, 9.5, 'Comment fonctionne le bot DATAGOUV ?', fontsize=20, fontweight='bold',
        ha='center', va='center', color='#1a1a2e')


def draw_box(ax, x, y, w, h, text, color, text_color='white', fontsize=11):
    box = FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0.03,rounding_size=0.2',
                         facecolor=color, edgecolor='white', linewidth=2)
    ax.add_patch(box)
    ax.text(x + w / 2, y + h / 2, text, fontsize=fontsize, color=text_color,
            ha='center', va='center', fontweight='bold')


def arrow(ax, x1, y1, x2, y2, color='#555'):
    ax.annotate('', xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle='->', color=color, lw=2.5,
                                connectionstyle='arc3,rad=0'))


# === ROW 1: User ===
draw_box(ax, 5.5, 8.2, 3, 0.9, 'Tu demandes dans Slack\n"Plombiers du 16eme"', '#e94560')
arrow(ax, 7, 8.2, 7, 7.6)

# === ROW 2: Bot Brain ===
draw_box(ax, 4.5, 6.5, 5, 1.0, 'Le bot comprend ta question\net choisit les bons outils', '#0f3460')
arrow(ax, 7, 6.5, 7, 5.9)

# === ROW 3: APIs ===
draw_box(ax, 1.2, 4.8, 3.5, 1.0, 'data.gouv.fr\n(cherche datasets)', '#533483')
arrow(ax, 4.7, 6.5, 3.0, 5.5)

draw_box(ax, 5.2, 4.8, 3.6, 1.0, 'API Entreprises\n(SIRET, NAF, adresse)', '#16213e')
arrow(ax, 7, 6.5, 7, 5.9)

draw_box(ax, 9.3, 4.8, 3.5, 1.0, 'Recherche web\n(telephones, dirigeants)', '#1a1a2e')
arrow(ax, 9.3, 6.5, 10.8, 5.8)

arrow(ax, 2.95, 4.8, 5.5, 3.6)
arrow(ax, 7, 4.8, 7, 3.6)
arrow(ax, 11.05, 4.8, 8.5, 3.6)

# === ROW 4: Result ===
draw_box(ax, 4.0, 2.5, 6, 1.0,
         'Reponse en 5 secondes\n15 plombiers trouves  •  Taux moyen : 4 employes\nExport Excel dispo  •  Sources citees',
         '#16c79a')
arrow(ax, 7, 2.5, 7, 1.9)

# === ROW 5: Memory ===
draw_box(ax, 4.5, 1.0, 5, 0.7, 'Memoire : le bot se souvient\nde cette conversation pour plus tard', '#f4a261')

# === Side: Avant / Apres ===
ax.text(0.3, 3.5, 'AVANT', fontsize=13, fontweight='bold', color='#e94560', va='center')
ax.text(0.3, 3.0, '• 3 sites ouverts', fontsize=9, color='#555', va='center')
ax.text(0.3, 2.6, '• 30 min de recherche', fontsize=9, color='#555', va='center')
ax.text(0.3, 2.2, '• Excel qui rame', fontsize=9, color='#555', va='center')

ax.text(0.3, 1.5, 'APRES', fontsize=13, fontweight='bold', color='#16c79a', va='center')
ax.text(0.3, 1.0, '• 1 message Slack', fontsize=9, color='#555', va='center')
ax.text(0.3, 0.6, '• 5 secondes', fontsize=9, color='#555', va='center')
ax.text(0.3, 0.2, '• Fichier recu', fontsize=9, color='#555', va='center')

plt.tight_layout()
out = os.path.expanduser('~/dev/datagouv-slack-agent/schema_bot.png')
plt.savefig(out, dpi=200, bbox_inches='tight', facecolor='white', edgecolor='none')
print(f'Schema genere : {out}')
