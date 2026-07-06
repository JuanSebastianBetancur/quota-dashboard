#!/usr/bin/env python3
"""
Dashboard web unificado de cuotas para multiples cuentas:
  - OpenAI (claves admin sk-admin-* / claves normales sk-*)
  - OpenCode Zen / OpenCode Go (claves opencode.ai/auth)

Sirve una pagina en http://127.0.0.1:8765 (configurable en config.json).
Sin dependencias externas: solo stdlib de Python.
"""

import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

OPENAI_BASE = "https://api.openai.com/v1"
OPENCODE_BASES = [
    ("zen", "https://opencode.ai/zen/v1"),
    ("go",  "https://opencode.ai/zen/go/v1"),
]

_state_lock = threading.Lock()
_state = {
    "updated_at": None,
    "openai": [],
    "opencode": [],
    "errors": [],
}


# ---------- helpers HTTP ----------

def _http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, method="GET")
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
        "tier": None,
        "endpoint_used": None,
        "models_count": None,
        "free_models": [],
        "balance_usd": None,
        "balance_note": "No expuesto via API key (solo dashboard web).",
        "error": None,
    }
    if not key:
        out["status"] = "missing-key"
        out["error"] = "Clave vacia en config.json"
        return out

    # Probar los dos bases (zen y go) y usar el primero que responda 200.
    last_err = None
    for tier, base in OPENCODE_BASES:
        st, body = _http_get(
            f"{base}/models",
            {"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        if st == 200:
            data = _json_safe(body)
            if isinstance(data, dict):
                models = data.get("data", [])
                out["status"] = "ok"
                out["tier"] = tier
                out["endpoint_used"] = f"{base}/models"
                out["models_count"] = len(models)
                free = [
                    m.get("id") for m in models
                    if isinstance(m, dict) and "free" in (m.get("id") or "").lower()
                ]
                out["free_models"] = sorted(set(free))
                return out
        elif st == 401:
            out["status"] = "invalid"
            out["tier"] = tier
            out["endpoint_used"] = f"{base}/models"
            out["error"] = "Clave invalida (401)."
            return out
        elif st == 404:
            last_err = f"{base}/models -> 404"
            continue
        else:
            last_err = f"{base}/models -> HTTP {st}"
            continue

    out["status"] = "error"
    out["error"] = last_err or "Sin respuesta de ningun endpoint."
    return out


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
    <h2>OpenCode Zen / Go</h2>
    <p class="note">OpenCode no expone el saldo via API key; solo validacion + lista de modelos. El saldo real se ve en <a style="color:var(--accent)" href="https://opencode.ai/auth" target="_blank">opencode.ai/auth</a> tras login.</p>
    <div class="grid" id="opencode"><div class="empty">cargando...</div></div>
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
  rows += row('Tier', d.tier?esc(d.tier.toUpperCase()):'—');
  rows += row('Endpoint', esc(d.endpoint_used||'—'));
  rows += row('Modelos disponibles', d.models_count==null?'—':d.models_count);
  rows += row('Modelos gratis', d.free_models && d.free_models.length? d.free_models.length : 0);
  let free = '';
  if(d.free_models && d.free_models.length){
    free = '<details><summary>Modelos gratis ('+d.free_models.length+')</summary><table><tbody>'+
      d.free_models.map(m=>'<tr><td>'+esc(m)+'</td></tr>').join('')+'</tbody></table></details>';
  }
  let err = d.error ? '<div class="err">'+esc(d.error)+'</div>' : '';
  let note = '<div class="note">'+esc(d.balance_note)+'</div>';
  return '<div class="card"><div class="name">'+esc(d.name)+' '+badge(d.status)+'</div>'+rows+free+err+note+'</div>';
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

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/data":
            with _state_lock:
                self._send(200, json.dumps(_state))
        elif self.path == "/api/config":
            cfg = load_config()
            # no devolver claves
            safe = {
                "port": cfg.get("port"),
                "host": cfg.get("host"),
                "refresh_seconds": cfg.get("refresh_seconds"),
                "openai_accounts": [a.get("name") for a in cfg.get("openai_accounts", [])],
                "opencode_accounts": [a.get("name") for a in cfg.get("opencode_accounts", [])],
            }
            self._send(200, json.dumps(safe))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/api/refresh":
            cfg = load_config()
            threading.Thread(target=refresh_all, args=(cfg,), daemon=True).start()
            self._send(202, '{"ok":true}')
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

    # refresco inicial + hilo en background
    threading.Thread(target=refresh_all, args=(config,), daemon=True).start()
    threading.Thread(target=background_loop, args=(config,), daemon=True).start()

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
