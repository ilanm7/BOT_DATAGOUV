"""Dashboard web minimal — stats par client et métriques globales."""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.web.api import create_api_router

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"

app = FastAPI(title="DATAGOUV-AGENT Dashboard")


def _get_db_path(cache_dir: str) -> Path:
    return Path(cache_dir) / DB_NAME


def _fetch_stats(cache_dir: str) -> dict[str, Any]:
    """Rassemble toutes les stats depuis SQLite."""
    db_path = _get_db_path(cache_dir)
    stats: dict[str, Any] = {
        "users": 0,
        "ao_alerts": 0,
        "tracked": [],
        "dce_indexed": 0,
        "pushes_total": 0,
        "avg_score": 0,
        "upcoming_deadlines": [],
        "profiles": [],
        "recent_pushes": [],
    }
    if not db_path.exists():
        return stats

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Profils
        rows = conn.execute("SELECT * FROM client_profiles ORDER BY updated_at DESC").fetchall()
        for row in rows:
            stats["profiles"].append({
                "user_id": row["user_id"],
                "activity": row["activity"] or "",
                "company_size": row["company_size"] or "",
                "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
                "sectors": json.loads(row["sectors"]) if row["sectors"] else [],
                "zones": json.loads(row["zones"]) if row["zones"] else [],
                "updated_at": row["updated_at"] or "",
            })
        stats["users"] = len(stats["profiles"])

        # Alertes AO
        row = conn.execute("SELECT COUNT(*) as c FROM ao_alerts").fetchone()
        stats["ao_alerts"] = row["c"] if row else 0

        # AO suivis
        rows = conn.execute(
            """
            SELECT id, user_id, ao_id, objet, deadline, reminders_sent
            FROM tracked_tenders
            ORDER BY deadline ASC
            """
        ).fetchall()
        today = datetime.now().date()
        for row in rows:
            dl_str = row["deadline"] or ""
            try:
                dl = datetime.strptime(dl_str, "%Y-%m-%d").date() if dl_str else None
            except Exception:
                dl = None
            days_left = (dl - today).days if dl else None
            stats["tracked"].append({
                "id": row["id"],
                "user_id": row["user_id"],
                "objet": row["objet"] or "",
                "deadline": dl_str,
                "days_left": days_left,
                "reminders": json.loads(row["reminders_sent"]) if row["reminders_sent"] else [],
            })

        # Prochaines deadlines (dans les 30 jours)
        stats["upcoming_deadlines"] = [
            t for t in stats["tracked"]
            if t["days_left"] is not None and 0 <= t["days_left"] <= 30
        ]

        # DCE indexés
        row = conn.execute(
            "SELECT COUNT(DISTINCT ao_id) as c FROM dce_chunks"
        ).fetchone()
        stats["dce_indexed"] = row["c"] if row else 0

        # Pushes historique
        row = conn.execute("SELECT COUNT(*) as c FROM ao_push_history").fetchone()
        stats["pushes_total"] = row["c"] if row else 0

        row = conn.execute(
            "SELECT AVG(score) as avg FROM ao_push_history"
        ).fetchone()
        stats["avg_score"] = round(row["avg"], 1) if row and row["avg"] else 0

        rows = conn.execute(
            """
            SELECT user_id, ao_id, objet, score, reason, pushed_at
            FROM ao_push_history
            ORDER BY pushed_at DESC
            LIMIT 20
            """
        ).fetchall()
        for row in rows:
            stats["recent_pushes"].append({
                "user_id": row["user_id"],
                "ao_id": row["ao_id"],
                "objet": (row["objet"] or "")[:60],
                "score": row["score"],
                "reason": (row["reason"] or "")[:80],
                "pushed_at": row["pushed_at"] or "",
            })

    return stats


_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DATAGOUV-AGENT Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f5f5f5; color: #333; line-height: 1.5; }
  .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
  h1 { font-size: 1.8rem; margin-bottom: 1.5rem; color: #1a1a1a; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
  .card { background: #fff; border-radius: 12px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .card h3 { font-size: 0.875rem; text-transform: uppercase; color: #666; margin-bottom: 0.5rem; }
  .card .value { font-size: 2rem; font-weight: 700; color: #111; }
  .card .sub { font-size: 0.875rem; color: #888; margin-top: 0.25rem; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px;
          overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  th, td { padding: 0.75rem 1rem; text-align: left; font-size: 0.875rem; }
  th { background: #fafafa; font-weight: 600; color: #555; border-bottom: 1px solid #eee; }
  td { border-bottom: 1px solid #f0f0f0; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.75rem;
           font-weight: 600; background: #e8f5e9; color: #2e7d32; }
  .badge-warn { background: #fff3e0; color: #ef6c00; }
  .badge-urgent { background: #ffebee; color: #c62828; }
  .section { margin-bottom: 2rem; }
  .section h2 { font-size: 1.2rem; margin-bottom: 1rem; color: #1a1a1a; }
  .empty { color: #999; font-style: italic; padding: 1rem; }
</style>
</head>
<body>
<div class="container">
  <h1>📊 DATAGOUV-AGENT Dashboard</h1>

  <div class="grid">
    <div class="card">
      <h3>Utilisateurs actifs</h3>
      <div class="value">{{users}}</div>
    </div>
    <div class="card">
      <h3>Alertes AO actives</h3>
      <div class="value">{{ao_alerts}}</div>
    </div>
    <div class="card">
      <h3>AO suivis</h3>
      <div class="value">{{tracked_count}}</div>
    </div>
    <div class="card">
      <h3>DCE indexés</h3>
      <div class="value">{{dce_indexed}}</div>
    </div>
    <div class="card">
      <h3>Pushes AO total</h3>
      <div class="value">{{pushes_total}}</div>
      <div class="sub">Score moyen : {{avg_score}}/100</div>
    </div>
    <div class="card">
      <h3>Deadlines ≤ 30j</h3>
      <div class="value">{{deadlines_count}}</div>
    </div>
  </div>

  <div class="section">
    <h2>⏰ Deadlines à venir (30 jours)</h2>
    {% if upcoming_deadlines %}
    <table>
      <tr><th>AO</th><th>Utilisateur</th><th>Deadline</th><th>Jours restants</th><th>Rappels</th></tr>
      {% for d in upcoming_deadlines %}
      <tr>
        <td>{{d.objet}}</td>
        <td>{{d.user_id}}</td>
        <td>{{d.deadline}}</td>
        <td>
          {% if d.days_left <= 2 %}<span class="badge badge-urgent">{{d.days_left}}j</span>
          {% elif d.days_left <= 7 %}<span class="badge badge-warn">{{d.days_left}}j</span>
          {% else %}<span class="badge">{{d.days_left}}j</span>{% endif %}
        </td>
        <td>{{ ", ".join(d.reminders) or "—" }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">Aucune deadline dans les 30 prochains jours.</div>
    {% endif %}
  </div>

  <div class="section">
    <h2>👤 Profils clients</h2>
    {% if profiles %}
    <table>
      <tr><th>Utilisateur</th><th>Activité</th><th>Taille</th><th>Secteurs</th><th>Zones</th><th>MàJ</th></tr>
      {% for p in profiles %}
      <tr>
        <td>{{p.user_id}}</td>
        <td>{{p.activity or "—"}}</td>
        <td>{{p.company_size or "—"}}</td>
        <td>{{ ", ".join(p.sectors) or "—" }}</td>
        <td>{{ ", ".join(p.zones) or "—" }}</td>
        <td>{{p.updated_at}}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">Aucun profil enregistré.</div>
    {% endif %}
  </div>

  <div class="section">
    <h2>🔔 Derniers pushes AO (20 derniers)</h2>
    {% if recent_pushes %}
    <table>
      <tr><th>Date</th><th>Utilisateur</th><th>AO</th><th>Score</th><th>Raison</th></tr>
      {% for push in recent_pushes %}
      <tr>
        <td>{{push.pushed_at}}</td>
        <td>{{push.user_id}}</td>
        <td>{{push.objet}}</td>
        <td><span class="badge">{{push.score}}</span></td>
        <td>{{push.reason}}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">Aucun push AO enregistré.</div>
    {% endif %}
  </div>
</div>
</body>
</html>
"""


def _render_template(template: str, data: dict[str, Any]) -> str:
    """Moteur de template minimal (pas de Jinja2, on reste léger)."""
    result = template

    # Boucles simples : {% for x in list %}...{% endfor %}
    import re
    loop_pattern = r"{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%}(.*?){%\s*endfor\s*%}"

    def _render_loop(match: re.Match) -> str:
        var_name = match.group(1)
        list_name = match.group(2)
        block = match.group(3)
        items = data.get(list_name, [])
        if not items:
            # Chercher le {% else %} dans le bloc
            else_split = re.split(r"{%\s*else\s*%}", block)
            if len(else_split) > 1:
                return else_split[1].strip()
            return ""
        out = []
        for item in items:
            rendered = block
            # Supprimer la partie {% else %}...{% endif %} si elle existe
            rendered = re.sub(r"{%\s*else\s*%}.*?{%\s*endif\s*%}", "", rendered, flags=re.DOTALL)
            # Variables simples {{var.attr}}
            for key, val in item.items():
                placeholder = f"{{{{{var_name}.{key}}}}}"
                rendered = rendered.replace(placeholder, str(val) if val is not None else "")
            # Expressions conditionnelles simples
            rendered = _render_conditions(rendered, item, var_name)
            out.append(rendered)
        return "\n".join(out)

    def _render_conditions(text: str, ctx: dict, prefix: str = "") -> str:
        # {% if cond %}...{% elif cond %}...{% else %}...{% endif %}
        def _eval_cond(cond: str, ctx: dict, prefix: str) -> bool:
            cond = cond.strip()
            # Supporte : d.days_left <= 2, d.reminders, not list
            if "not " in cond:
                inner = cond.replace("not ", "").strip()
                return not _eval_cond(inner, ctx, prefix)
            if " <= " in cond:
                left, right = cond.split(" <= ", 1)
                left_val = _resolve_var(left.strip(), ctx, prefix)
                right_val = int(right.strip())
                try:
                    return float(left_val) <= right_val
                except Exception:
                    return False
            val = _resolve_var(cond, ctx, prefix)
            return bool(val)

        def _resolve_var(expr: str, ctx: dict, prefix: str) -> Any:
            expr = expr.strip()
            if prefix and expr.startswith(prefix + "."):
                key = expr[len(prefix) + 1 :]
                return ctx.get(key, "")
            return data.get(expr, "")

        pattern = r"{%\s*if\s+(.*?)\s*%}(.*?){%\s*endif\s*%}"

        def _replace_if(match: re.Match) -> str:
            cond_raw = match.group(1)
            block = match.group(2)
            # Gérer elif/else
            parts = re.split(r"{%\s*elif\s+(.*?)\s*%}", block)
            first_cond = parts[0]
            rest = parts[1:]
            candidates = [(cond_raw, first_cond)]
            for i in range(0, len(rest), 2):
                if i + 1 < len(rest):
                    candidates.append((rest[i], rest[i + 1]))

            for cond, blk in candidates:
                if _eval_cond(cond, ctx, prefix):
                    # Enlever la partie else si présente dans ce bloc
                    blk = re.split(r"{%\s*else\s*%}", blk)[0]
                    return blk

            # Aucune condition ne matche, chercher le else global
            else_match = re.search(r"{%\s*else\s*%}(.*)", block, re.DOTALL)
            if else_match:
                return else_match.group(1)
            return ""

        return re.sub(pattern, _replace_if, text, flags=re.DOTALL)

    while True:
        new_result = re.sub(loop_pattern, _render_loop, result, flags=re.DOTALL)
        if new_result == result:
            break
        result = new_result

    # Variables globales simples {{key}}
    for key, val in data.items():
        if isinstance(val, (int, float, str)):
            result = result.replace(f"{{{{{key}}}}}", str(val))

    return result


def create_dashboard_app(cache_dir: str) -> FastAPI:
    """Factory qui retourne l'app FastAPI configurée avec le cache_dir."""

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        stats = _fetch_stats(cache_dir)
        data = {
            "users": stats["users"],
            "ao_alerts": stats["ao_alerts"],
            "tracked_count": len(stats["tracked"]),
            "dce_indexed": stats["dce_indexed"],
            "pushes_total": stats["pushes_total"],
            "avg_score": stats["avg_score"],
            "deadlines_count": len(stats["upcoming_deadlines"]),
            "upcoming_deadlines": stats["upcoming_deadlines"],
            "profiles": stats["profiles"],
            "recent_pushes": stats["recent_pushes"],
        }
        return _render_template(_DASHBOARD_HTML, data)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "datagouv-agent-dashboard"}

    @app.get("/api/stats")
    def api_stats() -> dict[str, Any]:
        return _fetch_stats(cache_dir)

    # Router API publique v1
    app.include_router(create_api_router(cache_dir))

    return app
