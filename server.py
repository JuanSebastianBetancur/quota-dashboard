#!/usr/bin/env python3
"""
Dashboard web unificado de cuotas para multiples cuentas:
  - OpenAI (claves admin sk-admin-* / claves normales sk-*)
  - OpenCode Zen / OpenCode Go (claves opencode.ai/auth)
  - OpenCode saldo real via scraping de cookie de sesion (billing/usage/go)

Sirve una pagina en http://127.0.0.1:8765 (configurable en config.json).
Sin dependencias externas: solo stdlib de Python.
"""

import json
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SESSIONS_DIR = os.path.join(HERE, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

OPENAI_BASE = "https://api.openai.com/v1"
OPENCODE_BASES = [
    ("zen", "https://opencode.ai/zen/v1"),
    ("go",  "https://opencode.ai/zen/go/v1"),
]
OPENCODE_WEB = "https://opencode.ai"
# 1 USD = 100,000,000 unidades internas (segun formatBalance del repo)
UNIT_DIVISOR = 100_000_000

_state_lock = threading.Lock()
_state = {
    "updated_at": None,
    "openai": [],
    "opencode": [],
    "opencode_scraped": [],
    "errors": [],
}


# ---------- helpers HTTP ----------

def _http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0 (quota-dashboard)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _json_safe(body):
    try:
        return json.loads(body)
    except Exception:
        return None


# ---------- OpenAI ----------

def _month_range_unix():
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _openai_get(key, path):
    url = f"{OPENAI_BASE}{path}"
    status, body = _http_get(url, {"Authorization": f"Bearer {key}"})
    return status, _json_safe(body), body


def fetch_openai(account):
    name = account.get("name", "?")
    key = account.get("key", "").strip()
    out = {
        "name": name,
        "status": "unknown",
        "is_admin": False,
        "org": None,
        "this_month_usd": None,
        "this_month_breakdown": [],
        "rate_limits": [],
        "models_count": None,
        "error": None,
    }
    if not key:
        out["status"] = "missing-key"
        out["error"] = "Clave vacia en config.json"
        return out

    # 1) Info de organizacion (requiere admin)
    st, org, raw = _openai_get(key, "/organization")
    if st == 200 and isinstance(org, dict):
        out["is_admin"] = True
        out["org"] = {
            "id": org.get("id"),
            "title": org.get("title") or org.get("name"),
        }
    elif st == 403:
        out["is_admin"] = False
        out["error"] = "Clave sin permisos de organization (no es admin)."
    elif st == 401:
        out["status"] = "invalid"
        out["error"] = "Clave invalida (401)."
        return out

    # 2) Costos del mes actual (solo admin)
    start_ts, end_ts = _month_range_unix()
    if out["is_admin"]:
        cost_path = f"/organization/costs?start_time={start_ts}&end_time={end_ts}&limit=100&group_by=line_item"
        st, costs, _ = _openai_get(key, cost_path)
        if st == 200 and isinstance(costs, dict):
            rows = []
            total = 0.0
            for page in costs.get("data", []):
                for r in page.get("results", []):
                    c = float(r.get("cost", 0) or 0)
                    rows.append({"name": r.get("name", "?"), "cost_usd": c})
                    total += c
            rows.sort(key=lambda x: x["cost_usd"], reverse=True)
            out["this_month_usd"] = round(total, 4)
            out["this_month_breakdown"] = rows[:10]

    # 3) Rate limits (solo admin)
    if out["is_admin"]:
        st, rl, _ = _openai_get(key, "/organization/rate_limits?per_page=100")
        if st == 200 and isinstance(rl, dict):
            limits = []
            for item in rl.get("data", []):
                limits.append({
                    "model": item.get("model"),
                    "max_requests_per_minute": item.get("max_requests_per_minute"),
                    "max_tokens_per_minute": item.get("max_tokens_per_minute"),
                    "max_images_per_minute": item.get("max_images_per_minute"),
                })
            limits = [l for l in limits if l["model"]]
            limits.sort(key=lambda x: x["model"])
            out["rate_limits"] = limits

    # 4) Validacion + conteo de modelos (clave normal tambien)
    st, models, _ = _openai_get(key, "/models")
    if st == 200 and isinstance(models, dict):
        out["models_count"] = len(models.get("data", []))
        if out["status"] == "unknown":
            out["status"] = "ok"
    elif st == 401:
        out["status"] = "invalid"
        out["error"] = "Clave invalida (401)."
        return out
    elif st == 403:
        out["status"] = "ok"  # clave valida pero sin /models
    else:
        out["error"] = f"/models -> HTTP {st}"

    out["status"] = out.get("status") or "ok"
    if out["status"] == "ok" and not out["is_admin"] and not out.get("error"):
        out["error"] = "Clave valida (sin acceso a organization/costos)."
    return out


# ---------- OpenCode ----------

def fetch_opencode(account):
    name = account.get("name", "?")
    key = account.get("key", "").strip()
    out = {
        "name": name,
        "status": "unknown",
        "tiers": {},
        "balance_usd": None,
        "balance_note": "OpenCode no expone el saldo via API key. Vealo en opencode.ai/auth tras login.",
        "error": None,
    }
    if not key:
        out["status"] = "missing-key"
        out["error"] = "Clave vacia en config.json"
        return out

    any_ok = False
    any_401 = False
    for tier, base in OPENCODE_BASES:
        st, body = _http_get(
            f"{base}/models",
            {"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        tier_info = {
            "endpoint": f"{base}/models",
            "http_status": st,
            "models_count": None,
            "models": [],
            "free_models": [],
            "error": None,
        }
        if st == 200:
            data = _json_safe(body)
            if isinstance(data, dict):
                models = data.get("data", [])
                ids = sorted([m.get("id") for m in models if isinstance(m, dict) and m.get("id")])
                tier_info["models_count"] = len(ids)
                tier_info["models"] = ids
                tier_info["free_models"] = [m for m in ids if "free" in m.lower()]
                any_ok = True
        elif st == 401:
            tier_info["error"] = "Clave invalida (401)."
            any_401 = True
        else:
            tier_info["error"] = f"HTTP {st}"
        out["tiers"][tier] = tier_info

    if any_ok:
        out["status"] = "ok"
    elif any_401:
        out["status"] = "invalid"
        out["error"] = "Clave invalida (401) en todos los endpoints."
    else:
        out["status"] = "error"
        out["error"] = "Sin respuesta 200 de ningun endpoint."
    return out


# ---------- OpenCode scraping via cookie de sesion ----------

def _http_get_full(url, headers=None, timeout=25):
    """GET que devuelve (status, final_url, body). Sigue redirects."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0 (quota-dashboard)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, r.geturl(), body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, url, body
    except Exception as e:
        return 0, url, f"{type(e).__name__}: {e}"


def normalize_cookie(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Acepta "auth=..." o el valor suelto; si viene solo el valor, prefix auth=
    if "=" in raw.split(";")[0]:
        return raw
    return f"auth={raw}"


def discover_workspace_id(cookie):
    """Descubre el workspace ID siguiendo el redirect de opencode.ai/."""
    headers = {"Cookie": cookie}
    for path in ["/", "/workspace"]:
        st, final_url, body = _http_get_full(f"{OPENCODE_WEB}{path}", headers, timeout=25)
        text = f"{final_url or ''} {body or ''}"
        m = re.search(r"workspace/(wrk_[A-Z0-9]+)", text)
        if m:
            return m.group(1)
    return None


def _usd(units):
    if units is None:
        return None
    try:
        return round(int(units) / UNIT_DIVISOR, 4)
    except (ValueError, TypeError):
        return None


def _extract_int(body, key):
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*(-?\d+)', body)
    return int(m.group(1)) if m else None


def _extract_bool(body, key):
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*(true|false|null)', body)
    return m.group(1) if m else None


def _extract_str(body, key):
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"', body)
    return m.group(1) if m else None


def scrape_opencode(cookie, workspace_id=None):
    """Scrapea billing/usage/go con la cookie de sesion."""
    out = {
        "status": "unknown",
        "workspace_id": workspace_id,
        "balance_usd": None,
        "monthly_limit_usd": None,
        "monthly_usage_usd": None,
        "reload_enabled": None,
        "reload_amount_usd": None,
        "reload_trigger_usd": None,
        "subscription": None,
        "subscription_plan": None,
        "lite": None,
        "plan_label": None,
        "http_status": None,
        "error": None,
        "raw_snippet": None,
    }
    if not cookie:
        out["status"] = "missing-cookie"
        out["error"] = "Cookie vacia."
        return out

    headers = {"Cookie": cookie}

    if not workspace_id:
        wsid = discover_workspace_id(cookie)
        out["workspace_id"] = wsid
        if not wsid:
            out["status"] = "no-workspace"
            out["error"] = "No se pudo descubrir el workspace ID. Cookie invalida/expirada."
            return out
        workspace_id = wsid

    st, _, body = _http_get_full(
        f"{OPENCODE_WEB}/workspace/{workspace_id}/billing", headers, timeout=25
    )
    out["http_status"] = st
    if st != 200:
        out["status"] = "error"
        out["error"] = f"billing HTTP {st}."
        if st in (401, 403):
            out["status"] = "expired"
            out["error"] = "Sesion expirada (401/403). Re-login requerido."
        return out

    # SolidStart serializa los resultados de query en el HTML; extraer campos.
    balance = _extract_int(body, "balance")
    monthly_limit = _extract_int(body, "monthlyLimit")
    monthly_usage = _extract_int(body, "monthlyUsage")
    reload_amount = _extract_int(body, "reloadAmount")
    reload_trigger = _extract_int(body, "reloadTrigger")
    reload_enabled = _extract_bool(body, "reload")
    subscription = _extract_bool(body, "subscription")
    subscription_plan = _extract_str(body, "subscriptionPlan")
    lite = _extract_bool(body, "lite")

    out["balance_usd"] = _usd(balance)
    out["monthly_limit_usd"] = _usd(monthly_limit)
    out["monthly_usage_usd"] = _usd(monthly_usage)
    out["reload_amount_usd"] = _usd(reload_amount)
    out["reload_trigger_usd"] = _usd(reload_trigger)
    out["reload_enabled"] = reload_enabled
    out["subscription"] = subscription
    out["subscription_plan"] = subscription_plan
    out["lite"] = lite

    # Etiqueta legible del plan
    if subscription == "true":
        out["plan_label"] = f"Black ({subscription_plan or 'plan'})"
    elif lite == "true":
        out["plan_label"] = "Go (Lite)"
    else:
        out["plan_label"] = "Pay-as-you-go"

    if balance is None and monthly_limit is None and monthly_usage is None:
        # Posible sesion expirada (pagina de login) o formato cambiado
        low = body.lower()
        if "sign in" in low or "/auth" in low or "log in" in low:
            out["status"] = "expired"
            out["error"] = "Sesion expirada. Re-login requerido."
        else:
            out["status"] = "no-data"
            out["error"] = "No se encontraron datos de billing en el HTML (formato desconocido)."
            out["raw_snippet"] = body[:3000]
        return out

    out["status"] = "ok"
    return out


# ---------- gestion de sesiones (cookies) ----------

def _session_path(name):
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", name)
    return os.path.join(SESSIONS_DIR, f"{safe}.json")


def list_sessions():
    sessions = []
    if not os.path.isdir(SESSIONS_DIR):
        return sessions
    for fn in sorted(os.listdir(SESSIONS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                sessions.append(json.load(f))
        except Exception:
            pass
    return sessions


def save_session(name, cookie, workspace_id=None, last_scrape=None):
    data = {
        "name": name,
        "cookie": normalize_cookie(cookie),
        "workspace_id": workspace_id,
        "last_scrape": last_scrape or {},
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(_session_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def delete_session(name):
    p = _session_path(name)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def refresh_scraped():
    """Re-scrapea todas las sesiones guardadas. No levanta excepciones."""
    sessions = list_sessions()
    results = []

    def worker(s):
        try:
            cookie = s.get("cookie", "")
            wsid = s.get("workspace_id")
            scrape = scrape_opencode(cookie, wsid)
            # persistir workspace_id descubierto y ultimo scrape
            save_session(s["name"], cookie, scrape.get("workspace_id"), scrape)
            scrape["name"] = s["name"]
            results.append(scrape)
        except Exception as e:
            results.append({
                "name": s.get("name", "?"),
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })

    threads = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    results.sort(key=lambda x: x.get("name", ""))
    with _state_lock:
        _state["opencode_scraped"] = results


# ---------- refresco ----------

def refresh_all(config):
    errors = []
    openai_results = []
    opencode_results = []

    def worker_openai(acc):
        try:
            openai_results.append(fetch_openai(acc))
        except Exception as e:
            errors.append(f"openai[{acc.get('name')}]: {e}")

    def worker_opencode(acc):
        try:
            opencode_results.append(fetch_opencode(acc))
        except Exception as e:
            errors.append(f"opencode[{acc.get('name')}]: {e}")

    threads = []
    for acc in config.get("openai_accounts", []):
        t = threading.Thread(target=worker_openai, args=(acc,))
        t.start()
        threads.append(t)
    for acc in config.get("opencode_accounts", []):
        t = threading.Thread(target=worker_opencode, args=(acc,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=60)

    # scrapeo de cuentas con cookie (saldo real)
    try:
        refresh_scraped()
    except Exception as e:
        errors.append(f"scraped: {e}")

    # ordenar por nombre
    openai_results.sort(key=lambda x: x.get("name", ""))
    opencode_results.sort(key=lambda x: x.get("name", ""))

    with _state_lock:
        _state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _state["openai"] = openai_results
        _state["opencode"] = opencode_results
        _state["errors"] = errors


def background_loop(config):
    interval = max(60, int(config.get("refresh_seconds", 300)))
    while True:
        try:
            refresh_all(config)
        except Exception as e:
            with _state_lock:
                _state["errors"].append(f"background: {e}")
        time.sleep(interval)


def background_scrape_loop(config):
    """Refresco mas frecuente para el saldo scrapeado (cada 5 min)."""
    interval = max(60, int(config.get("scrape_refresh_seconds", 300)))
    while True:
        time.sleep(interval)
        try:
            refresh_scraped()
        except Exception as e:
            with _state_lock:
                _state["errors"].append(f"scrape_loop: {e}")


# ---------- servidor HTTP ----------

HTML_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Cuotas - OpenAI + OpenCode</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --fg:#e6edf3; --muted:#8b949e; --ok:#3fb950; --warn:#d29922; --err:#f85149; --accent:#58a6ff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; }
  header { padding:16px 24px; border-bottom:1px solid var(--border); display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  header h1 { margin:0; font-size:18px; }
  header .meta { color:var(--muted); font-size:12px; }
  header button { margin-left:auto; background:var(--accent); color:#000; border:0; padding:8px 14px; border-radius:6px; cursor:pointer; font-weight:600; }
  header button:hover { filter:brightness(1.1); }
  main { padding:24px; max-width:1200px; margin:0 auto; }
  section { margin-bottom:32px; }
  section h2 { font-size:15px; margin:0 0 12px; color:var(--accent); border-bottom:1px solid var(--border); padding-bottom:6px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; }
  .card .name { font-weight:600; font-size:15px; margin-bottom:6px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
  .badge.ok { background:rgba(63,185,80,.15); color:var(--ok); }
  .badge.err { background:rgba(248,81,73,.15); color:var(--err); }
  .badge.warn { background:rgba(210,153,34,.15); color:var(--warn); }
  .badge.muted { background:rgba(139,148,159,.15); color:var(--muted); }
  .row { display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--border); font-size:13px; }
  .row:last-child { border-bottom:0; }
  .row .k { color:var(--muted); }
  .row .v { font-variant-numeric:tabular-nums; }
  .big { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }
  .err { color:var(--err); font-size:12px; margin-top:6px; }
  .note { color:var(--muted); font-size:12px; margin-top:6px; }
  details { margin-top:8px; }
  summary { cursor:pointer; color:var(--muted); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:6px; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:600; }
  .empty { color:var(--muted); font-style:italic; }
  #loading { display:none; color:var(--muted); font-size:12px; }
  #loading.show { display:inline; }
  .add-form { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; margin-bottom:16px; }
  .add-form label { display:block; color:var(--muted); font-size:12px; margin:8px 0 4px; }
  .add-form input, .add-form textarea { width:100%; background:#0d1117; color:var(--fg); border:1px solid var(--border); border-radius:6px; padding:8px; font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
  .add-form textarea { min-height:60px; resize:vertical; }
  .add-form button { margin-top:10px; background:var(--ok); color:#000; border:0; padding:8px 14px; border-radius:6px; cursor:pointer; font-weight:600; }
  .add-form button:hover { filter:brightness(1.1); }
  .add-form .hint { color:var(--muted); font-size:11px; margin-top:8px; }
  .add-form details { margin-top:8px; }
  .card .del { margin-top:8px; background:transparent; color:var(--err); border:1px solid var(--border); padding:4px 10px; border-radius:6px; cursor:pointer; font-size:12px; }
  .card .del:hover { background:rgba(248,81,73,.1); }
  .raw { white-space:pre-wrap; word-break:break-all; max-height:200px; overflow:auto; background:#0d1117; padding:8px; border-radius:6px; font:11px/1.4 ui-monospace,monospace; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>Cuotas: OpenAI + OpenCode</h1>
  <span class="meta" id="updated"></span>
  <span id="loading" class="show">cargando...</span>
  <button onclick="refresh()">Refrescar</button>
</header>
<main>
  <section>
    <h2>OpenAI (claves admin)</h2>
    <div class="grid" id="openai"><div class="empty">cargando...</div></div>
  </section>
  <section>
    <h2>OpenCode Zen / Go (via API key)</h2>
    <p class="note">OpenCode no expone el saldo via API key; solo validacion + lista de modelos. El saldo real se ve en <a style="color:var(--accent)" href="https://opencode.ai/auth" target="_blank">opencode.ai/auth</a> tras login.</p>
    <div class="grid" id="opencode"><div class="empty">cargando...</div></div>
  </section>
  <section>
    <h2>OpenCode — saldo real (scraping via cookie)</h2>
    <div class="add-form">
      <label for="s-name">Nombre de la cuenta (ej: juan@gmail.com)</label>
      <input id="s-name" type="text" placeholder="juan@gmail.com">
      <label for="s-cookie">Cookie de sesion <code>auth</code> de opencode.ai</label>
      <textarea id="s-cookie" placeholder="auth=s%3A...  (pega aqui el valor de la cookie auth)"></textarea>
      <button onclick="addSession()">Anadir cuenta</button>
      <span id="s-msg" class="err" style="margin-left:10px"></span>
      <details>
        <summary>Como obtener la cookie (una sola vez)</summary>
        <div class="hint">
          1. Abre <a style="color:var(--accent)" href="https://opencode.ai/auth" target="_blank">https://opencode.ai/auth</a> en tu navegador.<br>
          2. Inicia sesion con Google (o GitHub).<br>
          3. Abre DevTools (F12) &rarr; pestana <b>Application</b> (o Storage) &rarr; <b>Cookies</b> &rarr; <b>https://opencode.ai</b>.<br>
          4. Busca la cookie llamada <code>auth</code> y copia su <b>Value</b>.<br>
          5. Pegalo arriba. Dura ~1 ano; el dashboard refrescara el saldo cada 5 min sin re-login.
        </div>
      </details>
    </div>
    <div class="grid" id="scraped"><div class="empty">sin cuentas scrapeadas — anade una con el formulario de arriba</div></div>
  </section>
  <section id="errors"></section>
</main>
<script>
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function badge(status){
  if(status==='ok') return '<span class="badge ok">OK</span>';
  if(status==='invalid') return '<span class="badge err">INVALIDA</span>';
  if(status==='missing-key') return '<span class="badge warn">SIN CLAVE</span>';
  if(status==='error') return '<span class="badge err">ERROR</span>';
  return '<span class="badge muted">'+esc(status||'?')+'</span>';
}
function money(v){ if(v==null) return '—'; return '$'+Number(v).toFixed(2); }
function renderOpenAI(d){
  let rows = '';
  rows += row('Plan', d.is_admin ? 'Admin (acceso completo)' : 'Estandar (sin costos)');
  if(d.org) rows += row('Organizacion', esc(d.org.title||d.org.id) + ' ('+esc(d.org.id||'')+')');
  rows += row('Gasto este mes', '<span class="big">'+money(d.this_month_usd)+'</span>');
  rows += row('Modelos disponibles', d.models_count==null?'—':d.models_count);
  rows += row('Rate limits', d.rate_limits.length+' modelos');
  let breakdown = '';
  if(d.this_month_breakdown && d.this_month_breakdown.length){
    breakdown = '<details><summary>Desglose de costos (top 10)</summary><table><thead><tr><th>Item</th><th style="text-align:right">USD</th></tr></thead><tbody>'+
      d.this_month_breakdown.map(r=>'<tr><td>'+esc(r.name)+'</td><td style="text-align:right">'+money(r.cost_usd)+'</td></tr>').join('')+'</tbody></table></details>';
  }
  let rl = '';
  if(d.rate_limits && d.rate_limits.length){
    rl = '<details><summary>Rate limits</summary><table><thead><tr><th>Modelo</th><th>req/min</th><th>tok/min</th></tr></thead><tbody>'+
      d.rate_limits.map(r=>'<tr><td>'+esc(r.model)+'</td><td>'+(r.max_requests_per_minute||'—')+'</td><td>'+(r.max_tokens_per_minute||'—')+'</td></tr>').join('')+'</tbody></table></details>';
  }
  let err = d.error ? '<div class="err">'+esc(d.error)+'</div>' : '';
  return '<div class="card"><div class="name">'+esc(d.name)+' '+badge(d.status)+'</div>'+rows+breakdown+rl+err+'</div>';
}
function renderOpenCode(d){
  let rows = '';
  let tierDetails = '';
  if(d.tiers){
    for(const [tier, info] of Object.entries(d.tiers)){
      const ok = info.http_status === 200;
      const label = tier.toUpperCase();
      const b = ok ? '<span class="badge ok">'+info.models_count+' modelos</span>' : '<span class="badge err">'+esc(info.error||'HTTP '+info.http_status)+'</span>';
      rows += row(label+' ('+info.http_status+')', b);
      if(ok && info.models && info.models.length){
        tierDetails += '<details><summary>'+label+' — '+info.models_count+' modelos ('+info.free_models.length+' gratis)</summary><table><tbody>'+
          info.models.map(m=>'<tr><td>'+esc(m)+'</td>'+(info.free_models.includes(m)?'<td><span class="badge ok">free</span></td>':'<td></td>')+'</tr>').join('')+'</tbody></table></details>';
      }
    }
  }
  let err = d.error ? '<div class="err">'+esc(d.error)+'</div>' : '';
  let note = '<div class="note">'+esc(d.balance_note)+'</div>';
  return '<div class="card"><div class="name">'+esc(d.name)+' '+badge(d.status)+'</div>'+rows+tierDetails+err+note+'</div>';
}
function renderScraped(d){
  let rows = '';
  rows += row('Plan', esc(d.plan_label||'—'));
  rows += row('Saldo', '<span class="big">'+money(d.balance_usd)+'</span>');
  rows += row('Uso del mes', money(d.monthly_usage_usd));
  let lim = d.monthly_limit_usd;
  let limPct = (d.monthly_usage_usd!=null && lim>0) ? Math.round(d.monthly_usage_usd/lim*100) : null;
  rows += row('Limite mensual', money(lim) + (limPct!=null? ' <span class="badge '+(limPct>=90?'err':limPct>=70?'warn':'ok')+'">'+limPct+'%</span>':''));
  rows += row('Auto-reload', d.reload_enabled==='true' ? 'ON ('+money(d.reload_amount_usd)+' cuando < '+money(d.reload_trigger_usd)+')' : (d.reload_enabled==='false'?'OFF':'—'));
  rows += row('Workspace', esc(d.workspace_id||'—'));
  if(d.http_status) rows += row('HTTP billing', d.http_status);
  let err = d.error ? '<div class="err">'+esc(d.error)+'</div>' : '';
  let raw = '';
  if(d.raw_snippet){ raw = '<details><summary>HTML bruto (debug)</summary><div class="raw">'+esc(d.raw_snippet)+'</div></details>'; }
  let del = '<button class="del" data-name="'+esc(d.name)+'">Eliminar</button>';
  return '<div class="card"><div class="name">'+esc(d.name)+' '+badge(d.status)+'</div>'+rows+err+raw+del+'</div>';
}
function row(k,v){ return '<div class="row"><span class="k">'+esc(k)+'</span><span class="v">'+v+'</span></div>'; }
function render(data){
  document.getElementById('updated').textContent = data.updated_at ? 'Actualizado: '+data.updated_at+' UTC' : '';
  let oa = document.getElementById('openai');
  if(data.openai && data.openai.length) oa.innerHTML = data.openai.map(renderOpenAI).join('');
  else oa.innerHTML = '<div class="empty">Sin cuentas OpenAI en config.json</div>';
  let oc = document.getElementById('opencode');
  if(data.opencode && data.opencode.length) oc.innerHTML = data.opencode.map(renderOpenCode).join('');
  else oc.innerHTML = '<div class="empty">Sin cuentas OpenCode en config.json</div>';
  let sc = document.getElementById('scraped');
  if(data.opencode_scraped && data.opencode_scraped.length) sc.innerHTML = data.opencode_scraped.map(renderScraped).join('');
  else sc.innerHTML = '<div class="empty">sin cuentas scrapeadas — anade una con el formulario de arriba</div>';
  let er = document.getElementById('errors');
  if(data.errors && data.errors.length){
    er.innerHTML = '<h2>Errores</h2><ul>'+data.errors.map(e=>'<li class="err">'+esc(e)+'</li>').join('')+'</ul>';
  } else { er.innerHTML=''; }
  document.getElementById('loading').classList.remove('show');
}
function poll(){
  fetch('/api/data').then(r=>r.json()).then(d=>{
    if(d.updated_at){ render(d); }
    else { document.getElementById('loading').classList.add('show'); setTimeout(poll,1500); }
  }).catch(()=>setTimeout(poll,2000));
}
function refresh(){
  document.getElementById('loading').classList.add('show');
  fetch('/api/refresh',{method:'POST'}).then(()=>poll());
}
function addSession(){
  let name = document.getElementById('s-name').value.trim();
  let cookie = document.getElementById('s-cookie').value.trim();
  let msg = document.getElementById('s-msg');
  msg.textContent = '';
  if(!name || !cookie){ msg.textContent = 'Falta nombre o cookie.'; return; }
  msg.textContent = 'guardando y scrapeando...';
  fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,cookie})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){ document.getElementById('s-name').value=''; document.getElementById('s-cookie').value=''; msg.textContent='anadida. scrapeando...'; setTimeout(poll,2000); }
      else { msg.textContent = d.error || 'error'; }
    }).catch(e=>{ msg.textContent = 'error: '+e; });
}
function delSession(name){
  if(!confirm('Eliminar la cuenta "'+name+'"?')) return;
  fetch('/api/session?name='+encodeURIComponent(name),{method:'DELETE'})
    .then(r=>r.json()).then(d=>{ if(d.ok) poll(); }).catch(()=>{});
}
document.addEventListener('click', e=>{
  if(e.target && e.target.classList && e.target.classList.contains('del')){
    delSession(e.target.getAttribute('data-name'));
  }
});
poll();
setInterval(poll, 60000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return None

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/data":
            with _state_lock:
                self._send(200, json.dumps(_state))
        elif self.path == "/api/config":
            cfg = load_config()
            safe = {
                "port": cfg.get("port"),
                "host": cfg.get("host"),
                "refresh_seconds": cfg.get("refresh_seconds"),
                "scrape_refresh_seconds": cfg.get("scrape_refresh_seconds", 300),
                "openai_accounts": [a.get("name") for a in cfg.get("openai_accounts", [])],
                "opencode_accounts": [a.get("name") for a in cfg.get("opencode_accounts", [])],
                "scraped_accounts": [s.get("name") for s in list_sessions()],
            }
            self._send(200, json.dumps(safe))
        elif self.path == "/api/sessions":
            sessions = [{"name": s.get("name"), "workspace_id": s.get("workspace_id"),
                         "updated_at": s.get("updated_at")} for s in list_sessions()]
            self._send(200, json.dumps(sessions))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/api/refresh":
            cfg = load_config()
            threading.Thread(target=refresh_all, args=(cfg,), daemon=True).start()
            self._send(202, '{"ok":true}')
        elif self.path == "/api/session":
            data = self._read_json()
            if not isinstance(data, dict) or not data.get("name") or not data.get("cookie"):
                self._send(400, '{"error":"se requiere name y cookie"}')
                return
            name = data["name"].strip()
            cookie = data["cookie"].strip()
            save_session(name, cookie)
            # scrape inmediato en background
            threading.Thread(target=refresh_scraped, daemon=True).start()
            self._send(201, json.dumps({"ok": True, "name": name}))
        elif self.path == "/api/session/scrape":
            threading.Thread(target=refresh_scraped, daemon=True).start()
            self._send(202, '{"ok":true}')
        else:
            self._send(404, '{"error":"not found"}')

    def do_DELETE(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        if self.path.startswith("/api/session") and "name" in q:
            name = q["name"][0]
            ok = delete_session(name)
            if ok:
                # re-scrape para quitar la cuenta del estado
                threading.Thread(target=refresh_scraped, daemon=True).start()
                self._send(200, json.dumps({"ok": True, "name": name}))
            else:
                self._send(404, json.dumps({"error": "no encontrada"}))
        else:
            self._send(404, '{"error":"not found"}')


# ---------- config ----------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    config = load_config()
    host = config.get("host", "0.0.0.0")
    port = int(config.get("port", 8765))

    # refresco inicial + hilos en background
    threading.Thread(target=refresh_all, args=(config,), daemon=True).start()
    threading.Thread(target=background_loop, args=(config,), daemon=True).start()
    threading.Thread(target=background_scrape_loop, args=(config,), daemon=True).start()

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard en http://{host}:{port}", flush=True)
    print(f"Config: {CONFIG_PATH}", flush=True)
    print(f"PID: {os.getpid()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
