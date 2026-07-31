"""Génération de graphiques à partir de données structurées."""

import logging
import math
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # mode non-interactif
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def _unique_path(cache_dir: str, prefix: str = "chart") -> Path:
    """Génère un chemin unique pour éviter l'écrasement parallèle."""
    path = Path(cache_dir) / f"{prefix}_{uuid.uuid4().hex[:8]}_{int(time.time())}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def generate_bar_chart(
    data: list[dict],
    label_key: str,
    value_key: str,
    title: str = "Graphique",
    output_path: Path | None = None,
    cache_dir: str = "./cache",
) -> Path:
    """Génère un histogramme horizontal et sauvegarde en PNG."""
    if output_path is None:
        output_path = _unique_path(cache_dir, "bar")

    labels = [str(d.get(label_key, "?"))[:30] for d in data]
    values = []
    for d in data:
        v = d.get(value_key, 0)
        try:
            # Nettoyage des valeurs formatées (ex: "67,8 %" → 67.8)
            if isinstance(v, str):
                v_clean = v.replace("%", "").replace(" ", "").replace(",", ".")
                values.append(float(v_clean))
            else:
                values.append(float(v))
        except (ValueError, TypeError):
            values.append(0)

    if not values or max(values) == 0:
        raise ValueError("Aucune donnée numérique à afficher.")

    fig, ax = plt.subplots(figsize=(10, min(max(6, len(labels) * 0.5), 30)))
    try:
        bars = ax.barh(labels, values, color="steelblue")
        ax.set_xlabel(value_key)
        ax.set_title(title)
        ax.invert_yaxis()

        for bar, val in zip(bars, values, strict=False):
            width = bar.get_width()
            ax.text(width + max(values) * 0.01, bar.get_y() + bar.get_height() / 2,
                    f"{val:.0f}", ha="left", va="center", fontsize=9)

        plt.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    finally:
        if fig is not None:
            plt.close(fig)

    logger.info("Graphique généré: %s", output_path)
    return output_path


def generate_pie_chart(
    data: list[dict],
    label_key: str,
    value_key: str,
    title: str = "Graphique",
    output_path: Path | None = None,
    cache_dir: str = "./cache",
) -> Path:
    """Génère un camembert et sauvegarde en PNG."""
    if output_path is None:
        output_path = _unique_path(cache_dir, "pie")

    labels = [str(d.get(label_key, "?"))[:20] for d in data]
    values = []
    for d in data:
        v = d.get(value_key, 0)
        try:
            if isinstance(v, str):
                v_clean = v.replace("%", "").replace(" ", "").replace(",", ".")
                values.append(float(v_clean))
            else:
                values.append(float(v))
        except (ValueError, TypeError):
            values.append(0)

    if not values or sum(values) == 0:
        raise ValueError("Aucune donnée numérique à afficher.")

    # Limiter à 20 parts pour lisibilité
    if len(labels) > 20:
        other_value = sum(values[20:])
        labels = labels[:20] + ["Autres"]
        values = values[:20] + [other_value]

    fig, ax = plt.subplots(figsize=(8, 8))
    try:
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
        ax.set_title(title)

        plt.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    finally:
        if fig is not None:
            plt.close(fig)

    logger.info("Camembert généré: %s", output_path)
    return output_path


def generate_categorical_chart(
    data: list[dict],
    category_key: str,
    title: str = "Répartition",
    output_path: Path | None = None,
    cache_dir: str = "./cache",
) -> Path:
    """Génère un histogramme horizontal des occurrences par catégorie."""
    if output_path is None:
        output_path = _unique_path(cache_dir, "cat")

    categories = [str(d.get(category_key, "Non renseigné")) for d in data]
    counts = Counter(categories)

    if not counts:
        raise ValueError("Aucune catégorie à afficher.")

    items = counts.most_common(15)
    labels = [item[0][:35] for item in items]
    values = [item[1] for item in items]

    fig, ax = plt.subplots(figsize=(10, min(max(6, len(labels) * 0.5), 30)))
    try:
        bars = ax.barh(labels, values, color="steelblue")
        ax.set_xlabel("Nombre d'entités")
        ax.set_title(title)
        ax.invert_yaxis()

        for bar, val in zip(bars, values, strict=False):
            width = bar.get_width()
            ax.text(width + 0.1, bar.get_y() + bar.get_height() / 2,
                    str(int(val)), ha="left", va="center", fontsize=10, fontweight="bold")

        plt.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    finally:
        if fig is not None:
            plt.close(fig)

    logger.info("Graphique catégoriel généré: %s", output_path)
    return output_path


def _to_web_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Convertit WGS84 (lon, lat) en Web Mercator (EPSG:3857) en mètres."""
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    y = y * 20037508.34 / 180
    return x, y


def generate_scatter_map(
    data: list[dict],
    lat_key: str = "latitude",
    lon_key: str = "longitude",
    label_key: str | None = None,
    title: str = "Carte",
    output_path: Path | None = None,
    cache_dir: str = "./cache",
) -> Path:
    """Génère une carte de points (scatter plot) et sauvegarde en PNG.

    Si *contextily* est installé, un fond OpenStreetMap est ajouté et les
    coordonnées sont reprojetées en Web Mercator pour un rendu géométriquement
    correct. Sinon, fallback sur un scatter matplotlib simple.
    """
    if output_path is None:
        output_path = _unique_path(cache_dir, "map")

    points = []
    for d in data:
        lat = d.get(lat_key)
        lon = d.get(lon_key)
        if lat is None or lon is None:
            continue
        try:
            points.append(
                (
                    float(lon),
                    float(lat),
                    str(d.get(label_key, ""))[:35] if label_key else "",
                )
            )
        except (ValueError, TypeError):
            continue

    if len(points) < 1:
        raise ValueError("Aucun point géolocalisé trouvé dans les données.")

    try:
        import contextily as ctx
        HAS_CTX = True
    except ImportError:
        HAS_CTX = False

    if HAS_CTX:
        # Mode haute précision : Web Mercator + tuiles OSM
        wm_points = [(_to_web_mercator(lon, lat), lbl) for lon, lat, lbl in points]
        xs = [p[0][0] for p in wm_points]
        ys = [p[0][1] for p in wm_points]

        fig, ax = plt.subplots(figsize=(12, 12))
        try:
            # Marge d'environ 300 mètres autour des points
            margin = 300
            ax.set_xlim(min(xs) - margin, max(xs) + margin)
            ax.set_ylim(min(ys) - margin, max(ys) + margin)

            ctx.add_basemap(
                ax,
                crs="EPSG:3857",
                source=ctx.providers.OpenStreetMap.Mapnik,
                alpha=0.85,
            )

            ax.scatter(
                xs, ys,
                s=150, alpha=0.9, c="darkred",
                edgecolors="white", linewidths=1.5,
                zorder=5,
            )

            # Éviter le chevauchement : max ~15 labels
            step = max(1, len(wm_points) // 15)
            for i, ((x, y), lbl) in enumerate(wm_points):
                if lbl and i % step == 0:
                    ax.text(
                        x, y, f"  {lbl}",
                        fontsize=8, va="bottom", ha="left",
                        bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.75},
                        zorder=6,
                    )

            ax.set_aspect("equal")
            ax.axis("off")
            ax.set_title(title, fontsize=14, fontweight="bold", pad=10)
            plt.tight_layout(pad=0.5)
            fig.savefig(output_path, dpi=200, bbox_inches="tight", pad_inches=0.2)
        finally:
            if fig is not None:
                plt.close(fig)
    else:
        # Fallback fond blanc
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        fig, ax = plt.subplots(figsize=(10, 10))
        try:
            ax.scatter(lons, lats, s=80, alpha=0.7, c="darkred", edgecolors="black")
            for lon, lat, lbl in points:
                if lbl:
                    ax.text(lon, lat, f"  {lbl}", fontsize=8, va="bottom")
            ax.set_xlabel("Longitude")
            ax.set_ylabel("Latitude")
            ax.set_title(title)
            if lats:
                mid_lat = sum(lats) / len(lats)
                ax.set_aspect(1 / math.cos(math.radians(mid_lat)))
            plt.tight_layout()
            fig.savefig(output_path, dpi=150, bbox_inches="tight")
        finally:
            if fig is not None:
                plt.close(fig)

    logger.info("Carte générée: %s", output_path)
    return output_path


_FOLIUM_COLORS = [
    "red", "blue", "green", "purple", "orange", "darkred", "darkblue",
    "darkgreen", "cadetblue", "darkpurple", "pink", "lightblue",
    "lightgreen", "gray", "black",
]


def _build_popup_html(d: dict, label_key: str | None = None, category_key: str | None = None) -> str:
    """Construit un popup HTML riche avec toutes les infos disponibles."""
    nom = str(d.get(label_key, ""))[:60] if label_key else "Entreprise"
    if not nom:
        nom = "Entreprise"

    # Ordre et labels prioritaires
    priority = [
        ("adresse", "Adresse"),
        ("code_postal", "CP"),
        ("ville", "Ville"),
        ("siret", "SIRET"),
        ("siren", "SIREN"),
        ("activite_naf", "NAF"),
        ("effectif", "Effectif"),
        ("dirigeant", "Dirigeant"),
        ("telephone", "Téléphone"),
        ("email", "Email"),
        ("données_complémentaires", "Infos complémentaires"),
    ]
    rows = []
    seen = set()
    for key, display in priority:
        val = d.get(key)
        if val and str(val).strip():
            rows.append(f"<tr><td style='padding:2px 8px 2px 0;color:#555'><b>{display}</b></td><td style='padding:2px 0'>{val}</td></tr>")
            seen.add(key)

    # Ajouter toutes les autres clés non-géo et non vides
    skip = {"latitude", "longitude", "lat", "lon", "_geo_lat", "_geo_lon", label_key, category_key}
    skip.update(seen)
    for key, val in d.items():
        if key in skip or val is None or str(val).strip() == "":
            continue
        rows.append(f"<tr><td style='padding:2px 8px 2px 0;color:#555'><b>{key}</b></td><td style='padding:2px 0'>{val}</td></tr>")

    if rows:
        return f"<h4 style='margin:0 0 6px 0'>{nom}</h4><table style='font-size:13px;border-collapse:collapse'>{''.join(rows)}</table>"
    return f"<h4>{nom}</h4>"


def generate_interactive_map(
    data: list[dict],
    lat_key: str = "latitude",
    lon_key: str = "longitude",
    label_key: str | None = None,
    category_key: str | None = None,
    title: str = "Carte interactive",
    output_path: Path | None = None,
    cache_dir: str = "./cache",
) -> Path:
    """Génère une carte interactive Leaflet (HTML) via Folium.

    Les marqueurs sont cliquables et affichent un popup HTML riche avec toutes
    les informations disponibles (adresse, SIRET, dirigeant, téléphone, email…).
    Si plus de 50 points, un MarkerCluster est utilisé pour éviter la surcharge.
    Si *category_key* est fourni, chaque catégorie a sa propre couleur de marqueur
    et apparaît dans le LayerControl pour pouvoir les activer/désactiver.
    """
    if output_path is None:
        output_path = _unique_path(cache_dir, "interactive_map")
        output_path = output_path.with_suffix(".html")

    valid_data = []
    for d in data:
        lat = d.get(lat_key)
        lon = d.get(lon_key)
        if lat is None or lon is None:
            continue
        try:
            float(lat)
            float(lon)
            valid_data.append(d)
        except (ValueError, TypeError):
            continue

    if not valid_data:
        raise ValueError("Aucun point géolocalisé trouvé dans les données.")

    import folium
    from folium.plugins import MarkerCluster

    lats = [float(d[lat_key]) for d in valid_data]
    lons = [float(d[lon_key]) for d in valid_data]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    lat_span = max(lats) - min(lats) if len(lats) > 1 else 0.01
    lon_span = max(lons) - min(lons) if len(lons) > 1 else 0.01
    span = max(lat_span, lon_span)
    zoom = max(3, min(16, int(8 - math.log2(span + 0.001))))

    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom, tiles="OpenStreetMap")
    folium.TileLayer("CartoDB positron", name="Clair", control=True).add_to(m)
    folium.TileLayer("CartoDB dark_matter", name="Sombre", control=True).add_to(m)

    if category_key:
        categories = sorted({str(d.get(category_key, "")) for d in valid_data if d.get(category_key)})
        cat_to_color = {
            cat: _FOLIUM_COLORS[i % len(_FOLIUM_COLORS)]
            for i, cat in enumerate(categories)
        }
        groups: dict[str, Any] = {}
        for cat in categories:
            groups[cat] = folium.FeatureGroup(name=cat or "Autre", show=True)
            groups[cat].add_to(m)

        for d in valid_data:
            lat = float(d[lat_key])
            lon = float(d[lon_key])
            cat = str(d.get(category_key, ""))
            color = cat_to_color.get(cat, "blue")
            popup_html = _build_popup_html(d, label_key, category_key)
            folium.Marker(
                location=[lat, lon],
                popup=folium.Popup(popup_html, max_width=320),
                tooltip=str(d.get(label_key, cat or "Point"))[:50],
                icon=folium.Icon(color=color, icon="info-sign"),
            ).add_to(groups.get(cat, m))
    else:
        if len(valid_data) > 50:
            cluster = MarkerCluster(name="Points").add_to(m)
            for d in valid_data:
                lat = float(d[lat_key])
                lon = float(d[lon_key])
                popup_html = _build_popup_html(d, label_key, category_key)
                folium.Marker(
                    location=[lat, lon],
                    popup=folium.Popup(popup_html, max_width=320),
                    tooltip="Cliquez pour voir le détail",
                ).add_to(cluster)
        else:
            for d in valid_data:
                lat = float(d[lat_key])
                lon = float(d[lon_key])
                popup_html = _build_popup_html(d, label_key, category_key)
                folium.Marker(
                    location=[lat, lon],
                    popup=folium.Popup(popup_html, max_width=320),
                    tooltip=str(d.get(label_key, "Point"))[:50] if len(valid_data) <= 20 else None,
                ).add_to(m)

    folium.LayerControl().add_to(m)
    m.save(str(output_path))

    logger.info("Carte interactive générée: %s", output_path)
    return output_path
