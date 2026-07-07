#!/usr/bin/env python3
"""
Dashboard web unificado de cuotas para multiples cuentas:
  - OpenAI (claves admin sk-admin-* / claves normales sk-*)
  - OpenCode saldo real via scraping de cookie de sesion (billing/usage/go)
  - ChatGPT Plus/Pro + Codex via OAuth device-code (whoami + rate_limits)

Sirve una pagina en http://127.0.0.1:8765 (configurable en config.json).
Sin dependencias externas: solo stdlib de Python.
"""

import json
import os
import re
import sys
import time
import base64
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SESSIONS_DIR = os.path.join(HERE, "sessions")
CHATGPT_SESSIONS_DIR = os.path.join(HERE, "chatgpt_sessions")
ZAI_SESSIONS_DIR = os.path.join(HERE, "zai_sessions")
OLLAMA_SESSIONS_DIR = os.path.join(HERE, "ollama_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(CHATGPT_SESSIONS_DIR, exist_ok=True)
os.makedirs(ZAI_SESSIONS_DIR, exist_ok=True)
os.makedirs(OLLAMA_SESSIONS_DIR, exist_ok=True)

OPENAI_BASE = "https://api.openai.com/v1"
OPENCODE_WEB = "https://opencode.ai"
OPENAI_AUTH = "https://auth.openai.com"
OPENAI_CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_DEVICE_VERIFY_URL = "https://auth.openai.com/codex/device"
OPENAI_OAUTH_REDIRECT = "https://auth.openai.com/deviceauth/callback"
ZAI_API_BASE = "https://api.z.ai/api"
# 1 USD = 100,000,000 unidades internas (segun formatBalance del repo)
UNIT_DIVISOR = 100_000_000

_state_lock = threading.Lock()
_state = {
    "updated_at": None,
    "openai": [],
    "opencode_scraped": [],
    "chatgpt": [],
    "zai": [],
    "ollama": [],
    "errors": [],
}

# Estado de logins device-code en curso (no persistente)
_device_logins = {}
_device_logins_lock = threading.Lock()


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
    """Descubre el workspace ID. La pagina /go (con cookie) lo contiene."""
    headers = {"Cookie": cookie}
    for path in ["/go", "/", "/workspace"]:
        st, final_url, body = _http_get_full(f"{OPENCODE_WEB}{path}", headers, timeout=25)
        text = f"{final_url or ''} {body or ''}"
        m = re.search(r"(wrk_[A-Z0-9]+)", text)
        if m:
            return m.group(1)
    return None


def _usd(units):
    """Convierte unidades internas a USD (1 USD = 100,000,000 unidades)."""
    if units is None:
        return None
    try:
        return round(int(units) / UNIT_DIVISOR, 4)
    except (ValueError, TypeError):
        return None


def _extract_int(body, key):
    # SolidStart serializa como JS: key:value o "key":value
    m = re.search(r'(?:"' + re.escape(key) + r'"|' + re.escape(key) + r')\s*:\s*(-?\d+)', body)
    return int(m.group(1)) if m else None


def _extract_bool(body, key):
    m = re.search(r'(?:"' + re.escape(key) + r'"|' + re.escape(key) + r')\s*:\s*(true|false|null)', body)
    return m.group(1) if m else None


def _extract_str(body, key):
    m = re.search(r'(?:"' + re.escape(key) + r'"|' + re.escape(key) + r')\s*:\s*"([^"]*)"', body)
    return m.group(1) if m else None


def _extract_raw(body, key):
    # valor crudo hasta la siguiente coma o } (para detectar objetos/refs)
    m = re.search(r'(?:"' + re.escape(key) + r'"|' + re.escape(key) + r')\s*:\s*([^,}<]+)', body)
    return m.group(1).strip() if m else None


def scrape_opencode(cookie, workspace_id=None):
    """Scrapea billing/usage/go con la cookie de sesion."""
    out = {
        "status": "unknown",
        "workspace_id": workspace_id,
        "email": None,
        "balance_usd": None,
        "monthly_limit_usd": None,
        "monthly_usage_usd": None,
        "reload_enabled": None,
        "reload_amount_usd": None,
        "reload_trigger_usd": None,
        "subscription": None,
        "subscription_plan": None,
        "lite_subscription_id": None,
        "plan_label": None,
        "http_status_billing": None,
        "http_status_go": None,
        "go_rolling": None,
        "go_weekly": None,
        "go_monthly": None,
        "error": None,
        "raw_snippet": None,
    }
    if not cookie:
        out["status"] = "missing-cookie"
        out["error"] = "Cookie vacia."
        return out

    headers = {"Cookie": cookie}

    # Validar sesion y obtener email
    st, _, sbody = _http_get_full(f"{OPENCODE_WEB}/auth/status", headers, timeout=20)
    if st == 200:
        sjson = _json_safe(sbody)
        if isinstance(sjson, dict):
            acc = sjson.get("account", {})
            cur = sjson.get("current")
            if cur and cur in acc:
                out["email"] = acc[cur].get("email")
    if st in (401, 403):
        out["status"] = "expired"
        out["error"] = "Sesion expirada (auth/status 401/403). Re-login requerido."
        return out

    if not workspace_id:
        wsid = discover_workspace_id(cookie)
        out["workspace_id"] = wsid
        if not wsid:
            out["status"] = "no-workspace"
            out["error"] = "No se pudo descubrir el workspace ID (probar /go). Cookie invalida/expirada."
            return out
        workspace_id = wsid

    st, _, body = _http_get_full(
        f"{OPENCODE_WEB}/workspace/{workspace_id}/billing", headers, timeout=25
    )
    out["http_status_billing"] = st
    if st != 200:
        out["status"] = "error"
        out["error"] = f"billing HTTP {st}."
        if st in (401, 403):
            out["status"] = "expired"
            out["error"] = "Sesion expirada (401/403). Re-login requerido."
        return out

    # SolidStart serializa los resultados de query como JS (keys sin comillas).
    balance = _extract_int(body, "balance")             # unidades (÷100M)
    monthly_limit = _extract_int(body, "monthlyLimit")  # unidades
    monthly_usage = _extract_int(body, "monthlyUsage")  # unidades
    reload_amount = _extract_int(body, "reloadAmount")  # dolares directos
    reload_trigger = _extract_int(body, "reloadTrigger")  # dolares directos
    reload_enabled = _extract_bool(body, "reload")
    subscription = _extract_bool(body, "subscription")
    subscription_plan = _extract_str(body, "subscriptionPlan")
    lite_sub_id = _extract_str(body, "liteSubscriptionID")

    out["balance_usd"] = _usd(balance)
    out["monthly_limit_usd"] = _usd(monthly_limit)
    out["monthly_usage_usd"] = _usd(monthly_usage)
    out["reload_amount_usd"] = float(reload_amount) if reload_amount is not None else None
    out["reload_trigger_usd"] = float(reload_trigger) if reload_trigger is not None else None
    out["reload_enabled"] = reload_enabled
    out["subscription"] = subscription
    out["subscription_plan"] = subscription_plan
    out["lite_subscription_id"] = lite_sub_id

    # Etiqueta legible del plan
    if subscription == "true" or (subscription_plan and subscription_plan != "null"):
        out["plan_label"] = f"Black ({subscription_plan or 'plan'})"
    elif lite_sub_id and lite_sub_id != "null":
        out["plan_label"] = "Go (Lite)"
    else:
        out["plan_label"] = "Pay-as-you-go"

    # Scrapeo de /workspace/<id>/go para los medidores de uso (5h/semana/mes)
    stg, _, gbody = _http_get_full(
        f"{OPENCODE_WEB}/workspace/{workspace_id}/go", headers, timeout=25
    )
    out["http_status_go"] = stg
    if stg == 200:
        out["go_rolling"] = _extract_usage_meter(gbody, "rollingUsage")
        out["go_weekly"] = _extract_usage_meter(gbody, "weeklyUsage")
        out["go_monthly"] = _extract_usage_meter(gbody, "monthlyUsage")

    if balance is None and monthly_limit is None and monthly_usage is None:
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


def _extract_usage_meter(body, key):
    """Extrae {status, resetInSec, usagePercent} de un chunk tipo rollingUsage:$R[..]={...}"""
    m = re.search(
        re.escape(key) + r"\s*:\s*\$R\[\d+\]=\{([^}]*)\}",
        body,
    )
    if not m:
        return None
    inner = m.group(1)
    status = _extract_str(inner, "status") or _extract_raw(inner, "status")
    pct = _extract_int(inner, "usagePercent")
    reset = _extract_int(inner, "resetInSec")
    return {"status": status, "usagePercent": pct, "resetInSec": reset}


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


def refresh_one_session(name):
    """Re-scrapea una sola sesion por nombre y actualiza su entrada en el estado."""
    sessions = list_sessions()
    target = next((s for s in sessions if s.get("name") == name), None)
    if not target:
        return False
    try:
        cookie = target.get("cookie", "")
        wsid = target.get("workspace_id")
        scrape = scrape_opencode(cookie, wsid)
        save_session(target["name"], cookie, scrape.get("workspace_id"), scrape)
        scrape["name"] = target["name"]
    except Exception as e:
        scrape = {
            "name": name,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }
    with _state_lock:
        current = list(_state.get("opencode_scraped", []))
        # reemplazar la entrada existente o añadir
        found = False
        for i, s in enumerate(current):
            if s.get("name") == name:
                current[i] = scrape
                found = True
                break
        if not found:
            current.append(scrape)
        current.sort(key=lambda x: x.get("name", ""))
        _state["opencode_scraped"] = current
    return True


# ---------- ChatGPT/Codex via OAuth device-code ----------

def _http_post_json(url, payload, headers=None, timeout=20):
    """POST JSON y devuelve (status, json_dict_or_None, raw_body)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (quota-dashboard)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, _json_safe(body), body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, _json_safe(body), body
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def _http_get_json(url, headers=None, timeout=20):
    """GET con Bearer opcional y devuelve (status, json_dict_or_None, raw_body)."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0 (quota-dashboard)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, _json_safe(body), body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, _json_safe(body), body
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def chatgpt_start_device_login():
    """Inicia el device-code flow. Devuelve dict con device_auth_id, user_code, url, interval, expires_at."""
    st, data, raw = _http_post_json(
        f"{OPENAI_AUTH}/api/accounts/deviceauth/usercode",
        {"client_id": OPENAI_CHATGPT_CLIENT_ID},
        timeout=20,
    )
    if st != 200 or not isinstance(data, dict):
        return {"error": f"usercode HTTP {st}: {raw[:200]}"}
    device_auth_id = data.get("device_auth_id")
    user_code = data.get("user_code")
    interval = int(data.get("interval", 5) or 5)
    expires_at = data.get("expires_at")
    login = {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "url": f"{OPENAI_DEVICE_VERIFY_URL}",
        "interval": interval,
        "expires_at": expires_at,
        "status": "pending",
    }
    with _device_logins_lock:
        _device_logins[device_auth_id] = login
    return login


def chatgpt_poll_device_login(device_auth_id):
    """Poll del device-code. Devuelve el login actualizado (pending/success/error)."""
    with _device_logins_lock:
        login = _device_logins.get(device_auth_id)
    if not login:
        return {"error": "login no encontrado (expiro o nunca inicio)"}
    user_code = login.get("user_code")
    st, data, raw = _http_post_json(
        f"{OPENAI_AUTH}/api/accounts/deviceauth/token",
        {"device_auth_id": device_auth_id, "user_code": user_code},
        timeout=20,
    )
    # 403 con code=deviceauth_authorization_pending -> seguir esperando
    if st == 403 and isinstance(data, dict):
        code = (data.get("error") or {}).get("code", "")
        if code == "deviceauth_authorization_pending":
            login["status"] = "pending"
            return login
        if code == "deviceauth_authorization_slow_down":
            login["status"] = "pending"
            login["interval"] = login.get("interval", 5) + 5
            return login
        login["status"] = "error"
        login["error"] = f"poll: {code or raw[:120]}"
        return login
    if st == 200 and isinstance(data, dict):
        auth_code = data.get("authorization_code")
        code_verifier = data.get("code_verifier")
        if not auth_code or not code_verifier:
            login["status"] = "error"
            login["error"] = "respuesta sin authorization_code/code_verifier"
            return login
        # Intercambiar el authorization_code por tokens
        st2, data2, raw2 = _http_post_json(
            f"{OPENAI_AUTH}/oauth/token",
            {
                "grant_type": "authorization_code",
                "redirect_uri": OPENAI_OAUTH_REDIRECT,
                "code_verifier": code_verifier,
                "client_id": OPENAI_CHATGPT_CLIENT_ID,
                "code": auth_code,
            },
            timeout=20,
        )
        if st2 != 200 or not isinstance(data2, dict):
            login["status"] = "error"
            login["error"] = f"oauth/token HTTP {st2}: {raw2[:200]}"
            return login
        access_token = data2.get("access_token")
        refresh_token = data2.get("refresh_token")
        id_token = data2.get("id_token")
        if not access_token or not refresh_token:
            login["status"] = "error"
            login["error"] = "oauth/token sin access_token/refresh_token"
            return login
        # Extraer info de la cuenta de los claims del JWT (whoami suele fallar)
        info = chatgpt_extract_claims(access_token, id_token)
        name = info.get("email") or login.get("user_code", "chatgpt")
        chatgpt_save_session(name, {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "email": info.get("email"),
            "name": info.get("name"),
            "chatgpt_user_id": info.get("chatgpt_user_id"),
            "chatgpt_account_id": info.get("chatgpt_account_id"),
            "chatgpt_plan_type": info.get("chatgpt_plan_type"),
            "subscription_active_start": info.get("subscription_active_start"),
            "subscription_active_until": info.get("subscription_active_until"),
            "organizations": info.get("organizations"),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        login["status"] = "success"
        login["name"] = name
        # limpiar del mapa de logins en curso
        with _device_logins_lock:
            _device_logins.pop(device_auth_id, None)
        return login
    login["status"] = "error"
    login["error"] = f"poll HTTP {st}: {raw[:200]}"
    return login


def chatgpt_refresh_access_token(refresh_token):
    """Renueva el access_token usando el refresh_token. Devuelve (access_token, refresh_token, error)."""
    st, data, raw = _http_post_json(
        f"{OPENAI_AUTH}/oauth/token",
        {
            "grant_type": "refresh_token",
            "client_id": OPENAI_CHATGPT_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    if st != 200 or not isinstance(data, dict):
        return None, None, f"refresh HTTP {st}: {raw[:200]}"
    return data.get("access_token"), data.get("refresh_token"), None


def chatgpt_whoami(access_token):
    """GET whoami -> {email, chatgpt_user_id, chatgpt_account_id, chatgpt_plan_type}.
    Nota: este endpoint suele fallar con tokens de Codex OAuth; preferir _jwt_claims."""
    st, data, _ = _http_get_json(
        f"{OPENAI_AUTH}/api/accounts/v1/user-auth-credential/whoami",
        {"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if st == 200 and isinstance(data, dict):
        return {
            "email": data.get("email"),
            "chatgpt_user_id": data.get("chatgpt_user_id"),
            "chatgpt_account_id": data.get("chatgpt_account_id"),
            "chatgpt_plan_type": data.get("chatgpt_plan_type"),
        }
    return None


def chatgpt_fetch_usage(access_token):
    """GET chatgpt.com/backend-api/codex/usage con Bearer + OAI-Product-Sku: codex.
    Devuelve el dict de uso (rate_limit, credits, plan_type, email) o None."""
    st, data, raw = _http_get_json(
        "https://chatgpt.com/backend-api/codex/usage",
        {"Authorization": f"Bearer {access_token}", "OAI-Product-Sku": "codex"},
        timeout=20,
    )
    if st == 200 and isinstance(data, dict):
        return data
    return None


def _b64url_decode(s):
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _jwt_payload(tok):
    """Decodifica el payload de un JWT sin verificar firma."""
    if not tok or tok.count(".") < 2:
        return {}
    try:
        return json.loads(_b64url_decode(tok.split(".")[1]))
    except Exception:
        return {}


def _jwt_token_expired(tok):
    """Devuelve True si el JWT expiro (campo exp < ahora) o no se pudo leer."""
    p = _jwt_payload(tok)
    if not p:
        return True
    exp = p.get("exp")
    if not exp:
        return True
    return int(exp) < int(time.time())


def chatgpt_extract_claims(access_token, id_token):
    """Extrae email, name, plan, subscription y account IDs de los claims del JWT.
    Preferido sobre whoami (que suele fallar con tokens de Codex OAuth)."""
    idp = _jwt_payload(id_token) if id_token else {}
    atp = _jwt_payload(access_token) if access_token else {}
    auth_claim = idp.get("https://api.openai.com/auth") or atp.get("https://api.openai.com/auth") or {}
    profile_claim = atp.get("https://api.openai.com/profile") or {}
    out = {
        "email": idp.get("email") or profile_claim.get("email"),
        "name": idp.get("name"),
        "chatgpt_user_id": auth_claim.get("chatgpt_user_id"),
        "chatgpt_account_id": auth_claim.get("chatgpt_account_id"),
        "chatgpt_plan_type": auth_claim.get("chatgpt_plan_type"),
        "subscription_active_start": auth_claim.get("chatgpt_subscription_active_start"),
        "subscription_active_until": auth_claim.get("chatgpt_subscription_active_until"),
        "organizations": auth_claim.get("organizations"),
    }
    return out


def _chatgpt_session_path(name):
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", name)
    return os.path.join(CHATGPT_SESSIONS_DIR, f"{safe}.json")


def chatgpt_list_sessions():
    sessions = []
    if not os.path.isdir(CHATGPT_SESSIONS_DIR):
        return sessions
    for fn in sorted(os.listdir(CHATGPT_SESSIONS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CHATGPT_SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                sessions.append(json.load(f))
        except Exception:
            pass
    return sessions


def chatgpt_save_session(name, data):
    data["name"] = name
    with open(_chatgpt_session_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def chatgpt_delete_session(name):
    p = _chatgpt_session_path(name)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def chatgpt_fetch_one(sess):
    """Refresca una cuenta ChatGPT: renueva token + extrae claims del JWT +
    consulta /backend-api/codex/usage para rate_limits y credits.
    Devuelve dict para el estado."""
    name = sess.get("name", "?")
    out = {
        "name": name,
        "status": "unknown",
        "email": sess.get("email"),
        "plan_type": sess.get("chatgpt_plan_type"),
        "plan_label": None,
        "subscription_active_until": sess.get("subscription_active_until"),
        "rate_limits": None,
        "credits": None,
        "primary_used_percent": None,
        "secondary_used_percent": None,
        "error": None,
    }
    refresh_token = sess.get("refresh_token")
    if not refresh_token:
        out["status"] = "missing-token"
        out["error"] = "Sin refresh_token."
        return out

    access_token = sess.get("access_token")
    id_token = sess.get("id_token")

    # Si el access_token expiro, refrescarlo primero.
    if _jwt_token_expired(access_token):
        access_token, new_refresh, err = chatgpt_refresh_access_token(refresh_token)
        if err:
            out["status"] = "expired"
            out["error"] = f"No se pudo refrescar el token: {err}"
            return out
        sess["access_token"] = access_token
        if new_refresh:
            sess["refresh_token"] = new_refresh

    # Extraer claims del JWT (siempre; barato y no requiere red).
    info = chatgpt_extract_claims(access_token, id_token)
    if not info.get("email"):
        out["status"] = "error"
        out["error"] = "No se pudo extraer email del token."
        chatgpt_save_session(name, sess)
        return out

    # Actualizar la sesion persistida con la info extraida.
    sess["email"] = info.get("email")
    sess["name"] = info.get("name") or sess.get("name")
    sess["chatgpt_user_id"] = info.get("chatgpt_user_id")
    sess["chatgpt_account_id"] = info.get("chatgpt_account_id")
    sess["chatgpt_plan_type"] = info.get("chatgpt_plan_type")
    sess["subscription_active_start"] = info.get("subscription_active_start")
    sess["subscription_active_until"] = info.get("subscription_active_until")
    sess["organizations"] = info.get("organizations")

    # Si el nombre actual no es el email, renombrar el archivo de sesion al email.
    email = info.get("email")
    if email and name != email:
        chatgpt_delete_session(name)
        name = email
    sess["name"] = name
    out["name"] = name

    # Consultar uso de Codex (rate_limits + credits). No consume cuota.
    usage = chatgpt_fetch_usage(access_token)
    if usage:
        rl = usage.get("rate_limit") or {}
        primary = rl.get("primary_window") or {}
        secondary = rl.get("secondary_window") or {}
        rate_limits = {
            "primary": {
                "used_percent": primary.get("used_percent"),
                "limit_window_seconds": primary.get("limit_window_seconds"),
                "reset_after_seconds": primary.get("reset_after_seconds"),
                "reset_at": primary.get("reset_at"),
            },
            "secondary": {
                "used_percent": secondary.get("used_percent"),
                "limit_window_seconds": secondary.get("limit_window_seconds"),
                "reset_after_seconds": secondary.get("reset_after_seconds"),
                "reset_at": secondary.get("reset_at"),
            },
            "limit_reached": rl.get("limit_reached"),
            "allowed": rl.get("allowed"),
        }
        out["rate_limits"] = rate_limits
        out["primary_used_percent"] = primary.get("used_percent")
        out["secondary_used_percent"] = secondary.get("used_percent")
        # Credits
        credits = usage.get("credits") or {}
        out["credits"] = {
            "has_credits": credits.get("has_credits"),
            "unlimited": credits.get("unlimited"),
            "balance": credits.get("balance"),
            "approx_local_messages": credits.get("approx_local_messages"),
            "approx_cloud_messages": credits.get("approx_cloud_messages"),
        }
        # Persistir para no perderlo si la proxima llamada falla
        sess["last_rate_limits"] = rate_limits
        sess["last_credits"] = out["credits"]
        sess["last_usage_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    else:
        # Si falla, usar el ultimo guardado
        out["rate_limits"] = sess.get("last_rate_limits")
        out["credits"] = sess.get("last_credits")

    chatgpt_save_session(name, sess)

    out["email"] = info.get("email")
    out["plan_type"] = info.get("chatgpt_plan_type")
    out["subscription_active_until"] = info.get("subscription_active_until")
    pt = info.get("chatgpt_plan_type") or ""
    plan_labels = {
        "plus": "ChatGPT Plus",
        "pro": "ChatGPT Pro",
        "prolite": "ChatGPT Plus",
        "team": "ChatGPT Team",
        "enterprise": "ChatGPT Enterprise",
    }
    out["plan_label"] = plan_labels.get(pt, pt or "Desconocido")
    out["status"] = "ok"
    return out


def chatgpt_refresh_all():
    """Refresca todas las cuentas ChatGPT y actualiza el estado."""
    sessions = chatgpt_list_sessions()
    results = []

    def worker(s):
        try:
            results.append(chatgpt_fetch_one(s))
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
        t.join(timeout=30)
    results.sort(key=lambda x: x.get("name", ""))
    with _state_lock:
        _state["chatgpt"] = results


# ---------- z.ai (platform token) ----------

def _zai_api_get(path, token, timeout=20):
    """GET a api.z.ai/api/{path} con Bearer. Devuelve (status, json_dict_or_None)."""
    url = f"{ZAI_API_BASE}{path}"
    st, data, raw = _http_get_json(url, {"Authorization": f"Bearer {token}"}, timeout=timeout)
    return st, data


def _zai_session_path(name):
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", name)
    return os.path.join(ZAI_SESSIONS_DIR, f"{safe}.json")


def zai_list_sessions():
    sessions = []
    if not os.path.isdir(ZAI_SESSIONS_DIR):
        return sessions
    for fn in sorted(os.listdir(ZAI_SESSIONS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(ZAI_SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                sessions.append(json.load(f))
        except Exception:
            pass
    return sessions


def zai_save_session(name, data):
    data["name"] = name
    with open(_zai_session_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def zai_delete_session(name):
    p = _zai_session_path(name)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def zai_fetch_one(sess):
    """Consulta userInfo, subscription y quota de z.ai. Token permanente (sin exp)."""
    name = sess.get("name", "?")
    token = sess.get("token", "").strip()
    out = {
        "name": name,
        "status": "unknown",
        "email": sess.get("email"),
        "plan_label": None,
        "subscription": None,
        "limits": None,
        "level": None,
        "error": None,
    }
    if not token:
        out["status"] = "missing-token"
        out["error"] = "Sin token."
        return out

    # 1) UserInfo (email)
    st, data = _zai_api_get("/biz/customerService/zaiUserInfo", token)
    if st == 200 and isinstance(data, dict) and data.get("success"):
        u = data.get("data") or {}
        out["email"] = u.get("email")
        sess["email"] = u.get("email")
        sess["customer_number"] = u.get("customerNumber")
    elif st == 401:
        out["status"] = "expired"
        out["error"] = "Token invalido o expirado (401)."
        return out

    # 2) Subscription
    st, data = _zai_api_get("/biz/subscription/list", token)
    if st == 200 and isinstance(data, dict) and data.get("success"):
        subs = data.get("data") or []
        if subs:
            s = subs[0]
            out["subscription"] = {
                "product_name": s.get("productName"),
                "status": s.get("status"),
                "valid": s.get("valid"),
                "auto_renew": s.get("autoRenew"),
                "actual_price": s.get("actualPrice"),
                "next_renew_time": s.get("nextRenewTime"),
                "billing_cycle": s.get("billingCycle"),
                "current_period": s.get("currentPeriod"),
            }
            out["plan_label"] = s.get("productName") or "z.ai"
            sess["plan_label"] = out["plan_label"]

    # 3) Quota / Usage limits
    st, data = _zai_api_get("/monitor/usage/quota/limit", token)
    if st == 200 and isinstance(data, dict) and data.get("success"):
        qdata = data.get("data") or {}
        limits = qdata.get("limits") or []
        parsed_limits = []
        for lim in limits:
            ltype = lim.get("type")
            unit = lim.get("unit")
            pct = lim.get("percentage")
            # unit 3 = 5h rolling, unit 6 = weekly, unit 5 = monthly time-based
            unit_labels = {3: "Rolling 5h", 6: "Semanal", 5: "Mensual"}
            label = unit_labels.get(unit, f"unit {unit}")
            parsed_limits.append({
                "label": label,
                "type": ltype,
                "unit": unit,
                "number": lim.get("number"),
                "percentage": pct,
                "remaining": lim.get("remaining"),
                "current_value": lim.get("currentValue"),
                "usage": lim.get("usage"),
                "next_reset_time": lim.get("nextResetTime"),
                "usage_details": lim.get("usageDetails"),
            })
        out["limits"] = parsed_limits
        out["level"] = qdata.get("level")
        sess["level"] = out["level"]

    out["status"] = "ok"
    zai_save_session(name, sess)
    return out


def zai_refresh_all():
    """Refresca todas las cuentas z.ai y actualiza el estado."""
    sessions = zai_list_sessions()
    results = []

    def worker(s):
        try:
            results.append(zai_fetch_one(s))
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
        t.join(timeout=30)
    results.sort(key=lambda x: x.get("name", ""))
    with _state_lock:
        _state["zai"] = results


# ---------- ollama cloud (cookie scraping) ----------

OLLAMA_WEB = "https://ollama.com"

def _ollama_session_path(name):
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", name)
    return os.path.join(OLLAMA_SESSIONS_DIR, f"{safe}.json")


def ollama_list_sessions():
    sessions = []
    if not os.path.isdir(OLLAMA_SESSIONS_DIR):
        return sessions
    for fn in sorted(os.listdir(OLLAMA_SESSIONS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(OLLAMA_SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                sessions.append(json.load(f))
        except Exception:
            pass
    return sessions


def ollama_save_session(name, data):
    data["name"] = name
    with open(_ollama_session_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def ollama_delete_session(name):
    p = _ollama_session_path(name)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def _parse_reset_to_seconds(text):
    """Convierte 'Resets in X minutes/hours/days' a segundos."""
    m = re.search(r"(\d+)\s*(minutes?|hours?|days?)", text or "")
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("minute"):
        return n * 60
    if unit.startswith("hour"):
        return n * 3600
    if unit.startswith("day"):
        return n * 86400
    return None


def ollama_scrape(cookie, workspace_id=None):
    """Scrapea ollama.com/settings con la cookie __Secure-session."""
    out = {
        "status": "unknown",
        "email": None,
        "balance_usd": None,
        "meters": None,
        "auto_reload": None,
        "error": None,
        "raw_snippet": None,
    }
    if not cookie:
        out["status"] = "missing-cookie"
        out["error"] = "Cookie vacia."
        return out

    headers = {"Cookie": cookie}
    st, final_url, body = _http_get_full(f"{OLLAMA_WEB}/settings", headers, timeout=25)
    if st == 303 or (st == 200 and "signin" in (final_url or "").lower()):
        out["status"] = "expired"
        out["error"] = "Sesion expirada (redirige a signin). Re-login requerido."
        return out
    if st != 200:
        out["status"] = "error"
        out["error"] = f"/settings HTTP {st}."
        return out

    # Email
    m = re.search(r">([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})<", body)
    if m:
        out["email"] = m.group(1)

    # Balance
    m = re.search(r"Balance remaining.{0,200}?\$([0-9.]+)", body, re.DOTALL)
    if m:
        out["balance_usd"] = float(m.group(1))
    else:
        m = re.search(r'class="text-2xl[^"]*">\$([0-9.]+)<', body)
        if m:
            out["balance_usd"] = float(m.group(1))

    # Usage meters (data-usage-meter blocks con aria-label y Resets in)
    meters = []
    for block in re.finditer(r"data-usage-meter.*?(?=data-usage-meter|\Z)", body, re.DOTALL):
        blk = block.group(0)
        aria = re.search(r'aria-label="([^"]+)"', blk)
        reset = re.search(r"Resets in ([0-9]+ (?:minutes?|hours?|days?))", blk)
        if aria:
            label = aria.group(1)  # ej: "Session usage 8.2% used"
            pct_m = re.search(r"([\d.]+)%\s*used", label)
            pct = float(pct_m.group(1)) if pct_m else None
            name_m = re.match(r"(.+?)\s+\d", label)
            name = name_m.group(1).strip() if name_m else label
            meters.append({
                "label": name,
                "percentage": pct,
                "reset_text": reset.group(1) if reset else None,
                "reset_seconds": _parse_reset_to_seconds(reset.group(1) if reset else None),
            })
    out["meters"] = meters

    # Auto-reload
    if "Auto reload" in body:
        m_add = re.search(r"Add \$([0-9.]+)", body)
        m_trigger = re.search(r"hits \$([0-9.]+)", body)
        out["auto_reload"] = {
            "enabled": True,
            "add_amount": float(m_add.group(1)) if m_add else None,
            "trigger_amount": float(m_trigger.group(1)) if m_trigger else None,
        }

    if out["email"] is None and not meters:
        low = body.lower()
        if "signin" in low or "log in" in low:
            out["status"] = "expired"
            out["error"] = "Sesion expirada. Re-login requerido."
        else:
            out["status"] = "no-data"
            out["error"] = "No se encontraron datos en el HTML."
            out["raw_snippet"] = body[:3000]
        return out

    out["status"] = "ok"
    return out


def ollama_refresh_all():
    """Re-scrapea todas las sesiones de ollama."""
    sessions = ollama_list_sessions()
    results = []

    def worker(s):
        try:
            cookie = s.get("cookie", "")
            scrape = ollama_scrape(cookie)
            scrape["name"] = s.get("name", "?")
            ollama_save_session(s["name"], {"cookie": cookie, **scrape})
            results.append(scrape)
        except Exception as e:
            results.append({"name": s.get("name", "?"), "status": "error",
                            "error": f"{type(e).__name__}: {e}"})

    threads = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    results.sort(key=lambda x: x.get("name", ""))
    with _state_lock:
        _state["ollama"] = results


# ---------- refresco ----------

def refresh_all(config):
    errors = []
    openai_results = []

    def worker_openai(acc):
        try:
            openai_results.append(fetch_openai(acc))
        except Exception as e:
            errors.append(f"openai[{acc.get('name')}]: {e}")

    threads = []
    for acc in config.get("openai_accounts", []):
        t = threading.Thread(target=worker_openai, args=(acc,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=60)

    # scrapeo de cuentas con cookie (saldo real)
    try:
        refresh_scraped()
    except Exception as e:
        errors.append(f"scraped: {e}")

    # refresco de cuentas ChatGPT/Codex
    try:
        chatgpt_refresh_all()
    except Exception as e:
        errors.append(f"chatgpt: {e}")

    # refresco de cuentas z.ai
    try:
        zai_refresh_all()
    except Exception as e:
        errors.append(f"zai: {e}")

    # refresco de cuentas ollama cloud
    try:
        ollama_refresh_all()
    except Exception as e:
        errors.append(f"ollama: {e}")

    openai_results.sort(key=lambda x: x.get("name", ""))

    with _state_lock:
        _state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _state["openai"] = openai_results
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
    """Refresco mas frecuente para el saldo scrapeado, ChatGPT y z.ai (cada 5 min)."""
    interval = max(60, int(config.get("scrape_refresh_seconds", 300)))
    while True:
        time.sleep(interval)
        try:
            refresh_scraped()
            chatgpt_refresh_all()
            zai_refresh_all()
            ollama_refresh_all()
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
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:16px; align-items:stretch; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; display:flex; flex-direction:column; height:100%; }
  .card .prov-title { font-size:11px; font-weight:700; color:var(--accent); text-transform:uppercase; letter-spacing:.5px; margin-bottom:2px; }
  .card .name { font-weight:600; font-size:15px; margin-bottom:6px; }
  .card .actions { margin-top:auto; padding-top:10px; display:flex; align-items:center; gap:8px; }
  .card .upd, .card .del { height:32px; border-radius:6px; cursor:pointer; font:12px/1 ui-sans-serif,system-ui,sans-serif; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
  .card .upd { background:transparent; color:var(--accent); border:1px solid var(--border); padding:0 14px; }
  .card .del { background:transparent; color:var(--err); border:1px solid var(--border); width:32px; padding:0; }
  .card .upd:hover { background:rgba(88,166,255,.1); }
  .card .del:hover { background:rgba(248,81,73,.1); }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
  .badge.ok { background:rgba(63,185,80,.15); color:var(--ok); }
  .badge.err { background:rgba(248,81,73,.15); color:var(--err); }
  .badge.warn { background:rgba(210,153,34,.15); color:var(--warn); }
  .badge.muted { background:rgba(139,148,159,.15); color:var(--muted); }
  .row { display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--border); font-size:13px; }
  .row:last-child { border-bottom:0; }
  .row .k { color:var(--muted); }
  .row .v { font-variant-numeric:tabular-nums; }
  .big { font-size:16px; font-weight:700; font-variant-numeric:tabular-nums; }
  .dash { color:var(--muted); }
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
  .add-form { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; margin-top:16px; }
  .add-form label { display:block; color:var(--muted); font-size:12px; margin:8px 0 4px; }
  .add-form input, .add-form textarea { width:100%; background:#0d1117; color:var(--fg); border:1px solid var(--border); border-radius:6px; padding:8px; font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
  .add-form textarea { min-height:60px; resize:vertical; }
  .add-form button { margin-top:10px; background:var(--ok); color:#000; border:0; padding:8px 14px; border-radius:6px; cursor:pointer; font-weight:600; }
  .add-form button:hover { filter:brightness(1.1); }
  .add-form .hint { color:var(--muted); font-size:11px; margin-top:8px; }
  .add-form details { margin-top:8px; }
  .raw { white-space:pre-wrap; word-break:break-all; max-height:200px; overflow:auto; background:#0d1117; padding:8px; border-radius:6px; font:11px/1.4 ui-monospace,monospace; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>Cuotas: OpenAI + OpenCode</h1>
  <span class="meta" id="updated"></span>
  <span id="loading" class="show">cargando...</span>
</header>
<main>
  <div class="grid" id="allcards"><div class="empty">cargando...</div></div>
  <div style="margin-top:16px;text-align:center">
    <button id="add-btn" onclick="openModal()" style="background:var(--accent);color:#000;border:0;padding:10px 24px;border-radius:8px;cursor:pointer;font-weight:600;font-size:14px">+ Agregar cuenta</button>
  </div>
  <section id="errors" style="margin-top:24px"></section>
</main>

<div id="modal-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center">
  <div id="modal" style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;max-width:520px;width:90%;max-height:85vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="margin:0;font-size:16px;color:var(--accent)">Agregar cuenta</h2>
      <button onclick="closeModal()" style="background:none;border:0;color:var(--muted);font-size:20px;cursor:pointer">x</button>
    </div>
    <div id="modal-step1">
      <p style="color:var(--muted);font-size:13px;margin-bottom:12px">Selecciona el proveedor:</p>
      <div id="provider-list" style="display:grid;gap:10px"></div>
    </div>
    <div id="modal-step2" style="display:none">
      <button onclick="modalBack()" style="background:none;border:1px solid var(--border);color:var(--muted);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;margin-bottom:16px">&larr; Volver</button>
      <div id="modal-form"></div>
    </div>
  </div>
</div>

<script>
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function badge(status){
  if(status==='ok') return '<span class="badge ok">OK</span>';
  if(status==='invalid') return '<span class="badge err">INVALIDA</span>';
  if(status==='missing-key') return '<span class="badge warn">SIN CLAVE</span>';
  if(status==='expired') return '<span class="badge err">EXPIRADA</span>';
  if(status==='error') return '<span class="badge err">ERROR</span>';
  return '<span class="badge muted">'+esc(status||'?')+'</span>';
}
function money(v){ if(v==null) return null; return '$'+Number(v).toFixed(2); }
function fmtSec(s){
  if(s==null||s<=0) return null;
  if(s<3600) return Math.round(s/60)+' min';
  if(s<86400) return (s/3600).toFixed(1)+' h';
  return (s/86400).toFixed(1)+' d';
}
function pctBadge(pct){
  if(pct==null) return null;
  let cls = pct>=90?'err':pct>=70?'warn':'ok';
  return '<span class="badge '+cls+'">'+pct+'%</span>';
}
function fmtDate(s){
  if(!s) return null;
  try { let d=new Date(s); return d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}); }
  catch(e){ return s; }
}
function row(k,v){
  if(v==null||v==='') v = '<span class="dash">--</span>';
  return '<div class="row"><span class="k">'+esc(k)+'</span><span class="v">'+v+'</span></div>';
}
function meterRow(label, pct, resetSec){
  let reset = fmtSec(resetSec);
  let val = pctBadge(pct) || '<span class="dash">--</span>';
  if(reset) val += ' <span class="note">reset '+reset+'</span>';
  return row(label, val);
}
function autoReloadStr(ar){
  if(!ar) return null;
  if(ar.enabled==='true' || ar.enabled===true || ar.enabled===1){
    return 'ON ('+money(ar.add_amount||ar.reload_amount_usd)+' < '+money(ar.trigger_amount||ar.reload_trigger_usd)+')';
  }
  if(ar.enabled==='false' || ar.enabled===false || ar.enabled===0) return 'OFF';
  return null;
}

// --- Render unificado de tarjetas ---
const PROVIDER_LABELS = {opencode:'OpenCode Go', chatgpt:'ChatGPT / Codex', zai:'z.ai', ollama:'Ollama Cloud', openai:'OpenAI'};
const TRASH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>';
function renderCard(d, provider){
  // --- campos estandarizados (siempre presentes; "--" si no aplica) ---
  let plan = d.plan_label || d.level || (d.is_admin ? 'Admin' : null);
  if(d.subscription && d.subscription.product_name) plan = d.subscription.product_name;
  let email = d.email || null;

  // Uso: Rolling 5h / Semanal / Mensual
  let u5h = null, u5hReset = null;
  let uSem = null, uSemReset = null;
  let uMen = null, uMenReset = null;
  if(d.rate_limits){
    if(d.rate_limits.primary){ u5h=d.rate_limits.primary.used_percent; u5hReset=d.rate_limits.primary.reset_after_seconds; }
    if(d.rate_limits.secondary){ uSem=d.rate_limits.secondary.used_percent; uSemReset=d.rate_limits.secondary.reset_after_seconds; }
  }
  if(d.go_rolling){ u5h=d.go_rolling.usagePercent; u5hReset=d.go_rolling.resetInSec; }
  if(d.go_weekly){ uSem=d.go_weekly.usagePercent; uSemReset=d.go_weekly.resetInSec; }
  if(d.go_monthly){ uMen=d.go_monthly.usagePercent; uMenReset=d.go_monthly.resetInSec; }
  if(d.limits){
    for(let l of d.limits){
      let pct = l.percentage!=null?l.percentage:l.usage;
      let resetSec = l.next_reset_time?Math.round((l.next_reset_time-Date.now())/1000):null;
      if(l.label && l.label.toLowerCase().includes('5h')){ u5h=pct; u5hReset=resetSec; }
      else if(l.label && l.label.toLowerCase().includes('sem')){ uSem=pct; uSemReset=resetSec; }
      else if(l.label && l.label.toLowerCase().includes('men')){ uMen=pct; uMenReset=resetSec; }
    }
  }
  if(d.meters){
    for(let m of d.meters){
      let lbl=(m.label||'').toLowerCase();
      if(lbl.includes('session')||lbl.includes('5h')){ u5h=m.percentage; u5hReset=m.reset_seconds; }
      else if(lbl.includes('week')||lbl.includes('sem')){ uSem=m.percentage; uSemReset=m.reset_seconds; }
      else if(lbl.includes('month')||lbl.includes('men')){ uMen=m.percentage; uMenReset=m.reset_seconds; }
    }
  }

  // Saldo
  let saldo = null;
  if(d.balance_usd!=null) saldo = money(d.balance_usd);
  else if(d.credits && (d.credits.has_credits || d.credits.balance)) saldo = esc(d.credits.balance||'0');

  // Uso del mes (billing)
  let usoMes = d.monthly_usage_usd!=null ? money(d.monthly_usage_usd) : null;

  // Limite mensual
  let limMen = null;
  if(d.monthly_limit_usd!=null){
    limMen = money(d.monthly_limit_usd);
    if(d.monthly_usage_usd!=null && d.monthly_limit_usd>0){
      limMen += ' '+pctBadge(Math.round(d.monthly_usage_usd/d.monthly_limit_usd*100));
    }
  }

  // Auto-reload
  let ar = d.auto_reload || (d.reload_enabled && d.reload_enabled!=='null' ? {enabled:d.reload_enabled, add_amount:d.reload_amount_usd, trigger_amount:d.reload_trigger_usd} : null);
  let autoReload = autoReloadStr(ar);

  // Suscripcion hasta
  let subUntil = d.subscription_active_until ? fmtDate(d.subscription_active_until) : null;
  if(d.subscription && d.subscription.valid){
    let m = String(d.subscription.valid).match(/([0-9]{4}-[0-9]{2}-[0-9]{2})[ ]+.*?([0-9]{4}-[0-9]{2}-[0-9]{2})/);
    subUntil = m ? fmtDate(m[2]) : (subUntil||esc(d.subscription.valid));
  }

  let rows = '';
  rows += row('Plan', plan?esc(plan):null);
  rows += row('Email', email?esc(email):null);
  rows += meterRow('Rolling 5h', u5h, u5hReset);
  rows += meterRow('Semanal', uSem, uSemReset);
  rows += meterRow('Mensual', uMen, uMenReset);
  rows += row('Saldo', saldo!=null?'<span class="big">'+saldo+'</span>':null);
  rows += row('Uso del mes', usoMes);
  rows += row('Limite mensual', limMen);
  rows += row('Auto-reload', autoReload);
  rows += row('Suscripcion hasta', subUntil);

  let err = d.error ? '<div class="err">'+esc(d.error)+'</div>' : '';
  let raw = d.raw_snippet ? '<details><summary>HTML bruto (debug)</summary><div class="raw">'+esc(d.raw_snippet)+'</div></details>' : '';
  let provTitle = PROVIDER_LABELS[provider] || provider;
  let display = d.email || d.name;
  let actions = '<div class="actions">'
    + '<button class="del" data-type="'+provider+'" data-name="'+esc(d.name)+'" title="Eliminar">'+TRASH+'</button>'
    + '<span style="flex:1"></span>'
    + '<button class="upd" data-type="'+provider+'" data-name="'+esc(d.name)+'">Actualizar</button>'
    + '</div>';
  return '<div class="card"><div class="prov-title">'+esc(provTitle)+'</div><div class="name">'+esc(display)+' '+badge(d.status)+'</div>'+rows+err+raw+actions+'</div>';
}

function render(data){
  document.getElementById('updated').textContent = data.updated_at ? 'Actualizado: '+data.updated_at+' UTC' : '';
  let cards = [];
  (data.opencode_scraped||[]).forEach(d => cards.push(renderCard(d,'opencode')));
  (data.chatgpt||[]).forEach(d => cards.push(renderCard(d,'chatgpt')));
  (data.zai||[]).forEach(d => cards.push(renderCard(d,'zai')));
  (data.ollama||[]).forEach(d => cards.push(renderCard(d,'ollama')));
  (data.openai||[]).forEach(d => cards.push(renderCard(d,'openai')));
  let grid = document.getElementById('allcards');
  if(cards.length) grid.innerHTML = cards.join('');
  else grid.innerHTML = '<div class="empty">sin cuentas — pulsa "Agregar cuenta"</div>';
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

// --- Modal: agregar cuenta ---
const PROVIDERS = [
  {id:'opencode', label:'OpenCode (cookie auth)', field:'cookie', fieldLabel:'Cookie auth de opencode.ai', placeholder:'auth=Fe26.2...', hint:'DevTools &rarr; Application &rarr; Cookies &rarr; opencode.ai &rarr; copia el valor de <code>auth</code>'},
  {id:'chatgpt', label:'ChatGPT / Codex (device-code)', field:'device', hint:'Activa "Device-code login" en ChatGPT Settings &rarr; Security. Luego pulsa el boton.'},
  {id:'zai', label:'z.ai (platform token)', field:'token', fieldLabel:'Platform token (localStorage)', placeholder:'eyJhbGci...', hint:'F12 Console: localStorage.getItem(z-ai-open-platform-token-production)'},
  {id:'ollama', label:'Ollama Cloud (cookie)', field:'cookie', fieldLabel:'Cookie __Secure-session', placeholder:'__Secure-session=YWdl...', hint:'DevTools &rarr; Application &rarr; Cookies &rarr; ollama.com &rarr; copia el valor de <code>__Secure-session</code>'},
];
let _cgPollTimer = null;
let _modalProvider = null;

function openModal(){
  document.getElementById('modal-overlay').style.display = 'flex';
  modalShowStep1();
}
function closeModal(){
  document.getElementById('modal-overlay').style.display = 'none';
  if(_cgPollTimer){ clearTimeout(_cgPollTimer); _cgPollTimer=null; }
}
function modalShowStep1(){
  document.getElementById('modal-step1').style.display = '';
  document.getElementById('modal-step2').style.display = 'none';
  let list = document.getElementById('provider-list');
  list.innerHTML = PROVIDERS.map(p =>
    '<button onclick=\"modalSelect(\\''+p.id+'\\')\" style=\"background:var(--bg);border:1px solid var(--border);color:var(--fg);padding:14px;border-radius:8px;cursor:pointer;text-align:left;font-size:14px\">'+esc(p.label)+'</button>'
  ).join('');
}
function modalSelect(id){
  _modalProvider = id;
  let p = PROVIDERS.find(x => x.id === id);
  document.getElementById('modal-step1').style.display = 'none';
  document.getElementById('modal-step2').style.display = '';
  let form = document.getElementById('modal-form');
  let nameInput = '<label style="display:block;color:var(--muted);font-size:12px;margin:8px 0 4px">Nombre</label><input id="m-name" type="text" placeholder="email@ejemplo.com" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:8px;font:13px ui-monospace,monospace">';
  let body = '<p style="color:var(--accent);font-weight:600;margin-bottom:8px">'+esc(p.label)+'</p>';
  if(p.field === 'device'){
    // ChatGPT device-code flow
    body += '<p style="color:var(--muted);font-size:12px;margin-bottom:10px">'+p.hint+'</p>';
    body += '<button onclick="modalChatGPTLogin()" style="background:var(--accent);color:#000;border:0;padding:10px 20px;border-radius:8px;cursor:pointer;font-weight:600">Iniciar login</button>';
    body += '<span id="m-msg" class="err" style="margin-left:10px"></span>';
    body += '<div id="m-login-box" style="display:none;margin-top:12px;padding:12px;background:var(--bg);border-radius:6px;border:1px solid var(--border)">';
    body += '<div style="font-size:12px;color:var(--muted);margin-bottom:4px">Abre esta URL e ingresa el codigo:</div>';
    body += '<div id="m-login-url" style="font-family:monospace;color:var(--accent);word-break:break-all;font-size:12px;margin-bottom:8px"></div>';
    body += '<div style="font-size:12px;color:var(--muted);margin-bottom:4px">Codigo:</div>';
    body += '<div id="m-login-code" style="font-size:22px;font-weight:700;letter-spacing:2px;font-family:monospace;margin-bottom:8px"></div>';
    body += '<div id="m-login-status" style="color:var(--warn);font-size:12px">esperando...</div>';
    body += '</div>';
  } else {
    body += nameInput;
    body += '<label style="display:block;color:var(--muted);font-size:12px;margin:8px 0 4px">'+esc(p.fieldLabel||'')+'</label>';
    body += '<textarea id="m-credential" placeholder="'+esc(p.placeholder||'')+'" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:8px;min-height:70px;font:12px ui-monospace,monospace;resize:vertical"></textarea>';
    body += '<details style="margin-top:8px"><summary style="cursor:pointer;color:var(--muted);font-size:12px">Como obtenerlo</summary><div style="color:var(--muted);font-size:12px;margin-top:6px">'+p.hint+'</div></details>';
    body += '<button onclick="modalSubmit()" style="margin-top:14px;background:var(--ok);color:#000;border:0;padding:10px 24px;border-radius:8px;cursor:pointer;font-weight:600">Anadir</button>';
    body += '<span id="m-msg" class="err" style="margin-left:10px"></span>';
  }
  form.innerHTML = body;
}
function modalBack(){ modalShowStep1(); }
function modalSubmit(){
  let p = _modalProvider;
  let name = (document.getElementById('m-name')||{}).value;
  name = name ? name.trim() : '';
  let cred = (document.getElementById('m-credential')||{}).value;
  cred = cred ? cred.trim() : '';
  let msg = document.getElementById('m-msg');
  if(msg) msg.textContent = '';
  let body = {};
  if(p === 'opencode' || p === 'ollama'){
    if(!name || !cred){ if(msg) msg.textContent='Falta nombre o valor.'; return; }
    body = {name, cookie: cred};
  } else if(p === 'zai'){
    if(!name || !cred){ if(msg) msg.textContent='Falta nombre o token.'; return; }
    body = {name, token: cred};
  }
  let path = p==='opencode' ? '/api/session' : p==='ollama' ? '/api/ollama/session' : p==='zai' ? '/api/zai/session' : '';
  if(msg) msg.textContent = 'guardando...';
  fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(r=>r.json()).then(d=>{
      if(d.ok){ if(msg) msg.textContent='anadida'; closeModal(); setTimeout(poll,2000); }
      else { if(msg) msg.textContent = d.error||'error'; }
    }).catch(e=>{ if(msg) msg.textContent='error: '+e; });
}
function modalChatGPTLogin(){
  let msg = document.getElementById('m-msg');
  msg.textContent = 'iniciando...';
  fetch('/api/chatgpt/login',{method:'POST'})
    .then(r=>r.json()).then(d=>{
      if(d.error){ msg.textContent=d.error; return; }
      msg.textContent='';
      document.getElementById('m-login-box').style.display='';
      document.getElementById('m-login-url').textContent=d.url;
      document.getElementById('m-login-code').textContent=d.user_code;
      let statusEl=document.getElementById('m-login-status');
      let poll2 = () => {
        fetch('/api/chatgpt/login/poll?device_auth_id='+encodeURIComponent(d.device_auth_id),{method:'POST'})
          .then(r=>r.json()).then(x=>{
            if(x.status==='success'){ statusEl.style.color='var(--ok)'; statusEl.textContent='anadida: '+(x.name||'ok'); setTimeout(()=>{closeModal(); poll();},1500); }
            else if(x.status==='pending'){ _cgPollTimer=setTimeout(poll2,(d.interval||5)*1000); }
            else { statusEl.style.color='var(--err)'; statusEl.textContent='error: '+(x.error||'?'); }
          }).catch(e=>{ statusEl.style.color='var(--err)'; statusEl.textContent='red: '+e; });
      };
      _cgPollTimer=setTimeout(poll2,(d.interval||5)*1000);
    }).catch(e=>{ msg.textContent='error: '+e; });
}

// --- Acciones por card (Actualizar / Eliminar) ---
const REFRESH_PATHS = {opencode:'/api/session/scrape', chatgpt:'/api/chatgpt/refresh', zai:'/api/zai/refresh', ollama:'/api/ollama/refresh'};
const DELETE_PATHS = {opencode:'/api/session', chatgpt:'/api/chatgpt/session', zai:'/api/zai/session', ollama:'/api/ollama/session'};
function refreshCard(type, name, btn){
  if(btn){ btn.disabled=true; btn.textContent='Actualizando...'; }
  fetch(REFRESH_PATHS[type]+'?name='+encodeURIComponent(name),{method:'POST'})
    .then(()=>setTimeout(()=>{ poll(); if(btn){btn.disabled=false; btn.textContent='Actualizar';} },3000))
    .catch(()=>{ if(btn){btn.disabled=false; btn.textContent='Actualizar';} });
}
function deleteCard(type, name){
  if(!confirm('Eliminar "'+name+'"?')) return;
  fetch(DELETE_PATHS[type]+'?name='+encodeURIComponent(name),{method:'DELETE'})
    .then(r=>r.json()).then(d=>{ if(d.ok) poll(); }).catch(()=>{});
}
document.addEventListener('click', e=>{
  if(!e.target || !e.target.classList) return;
  let type=e.target.getAttribute('data-type');
  if(e.target.classList.contains('del')) deleteCard(type, e.target.getAttribute('data-name'));
  else if(e.target.classList.contains('upd')) refreshCard(type, e.target.getAttribute('data-name'), e.target);
});
document.getElementById('modal-overlay').addEventListener('click', e=>{ if(e.target.id==='modal-overlay') closeModal(); });
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
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif parsed.path == "/api/data":
            with _state_lock:
                self._send(200, json.dumps(_state))
        elif parsed.path == "/api/config":
            cfg = load_config()
            safe = {
                "port": cfg.get("port"),
                "host": cfg.get("host"),
                "refresh_seconds": cfg.get("refresh_seconds"),
                "scrape_refresh_seconds": cfg.get("scrape_refresh_seconds", 300),
                "openai_accounts": [a.get("name") for a in cfg.get("openai_accounts", [])],
                "scraped_accounts": [s.get("name") for s in list_sessions()],
                "chatgpt_accounts": [s.get("name") for s in chatgpt_list_sessions()],
                "zai_accounts": [s.get("name") for s in zai_list_sessions()],
                "ollama_accounts": [s.get("name") for s in ollama_list_sessions()],
            }
            self._send(200, json.dumps(safe))
        elif parsed.path == "/api/sessions":
            sessions = [{"name": s.get("name"), "workspace_id": s.get("workspace_id"),
                         "updated_at": s.get("updated_at")} for s in list_sessions()]
            self._send(200, json.dumps(sessions))
        elif parsed.path == "/api/chatgpt/sessions":
            sessions = [{"name": s.get("name"), "email": s.get("email"),
                         "plan_type": s.get("chatgpt_plan_type")} for s in chatgpt_list_sessions()]
            self._send(200, json.dumps(sessions))
        elif parsed.path == "/api/zai/sessions":
            sessions = [{"name": s.get("name"), "email": s.get("email"),
                         "plan_label": s.get("plan_label")} for s in zai_list_sessions()]
            self._send(200, json.dumps(sessions))
        elif parsed.path == "/api/ollama/sessions":
            sessions = [{"name": s.get("name"), "email": s.get("email")} for s in ollama_list_sessions()]
            self._send(200, json.dumps(sessions))
        elif parsed.path == "/api/chatgpt/login/status":
            q = parse_qs(parsed.query)
            did = q.get("device_auth_id", [None])[0]
            if not did:
                self._send(400, '{"error":"se requiere device_auth_id"}')
                return
            with _device_logins_lock:
                login = _device_logins.get(did, {"status": "unknown", "error": "login expirado o no encontrado"})
            self._send(200, json.dumps(login))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            cfg = load_config()
            threading.Thread(target=refresh_all, args=(cfg,), daemon=True).start()
            self._send(202, '{"ok":true}')
        elif parsed.path == "/api/session":
            data = self._read_json()
            if not isinstance(data, dict) or not data.get("name") or not data.get("cookie"):
                self._send(400, '{"error":"se requiere name y cookie"}')
                return
            name = data["name"].strip()
            cookie = data["cookie"].strip()
            save_session(name, cookie)
            threading.Thread(target=refresh_one_session, args=(name,), daemon=True).start()
            self._send(201, json.dumps({"ok": True, "name": name}))
        elif parsed.path == "/api/session/scrape":
            q = parse_qs(parsed.query)
            if "name" in q:
                name = q["name"][0]
                threading.Thread(target=refresh_one_session, args=(name,), daemon=True).start()
                self._send(202, json.dumps({"ok": True, "name": name}))
            else:
                threading.Thread(target=refresh_scraped, daemon=True).start()
                self._send(202, '{"ok":true}')
        elif parsed.path == "/api/chatgpt/login":
            # Iniciar device-code flow
            login = chatgpt_start_device_login()
            self._send(200, json.dumps(login))
        elif parsed.path == "/api/chatgpt/login/poll":
            q = parse_qs(parsed.query)
            did = q.get("device_auth_id", [None])[0]
            if not did:
                self._send(400, '{"error":"se requiere device_auth_id"}')
                return
            result = chatgpt_poll_device_login(did)
            self._send(200, json.dumps(result))
        elif parsed.path == "/api/chatgpt/refresh":
            q = parse_qs(parsed.query)
            if "name" in q:
                name = q["name"][0]
                sess = next((s for s in chatgpt_list_sessions() if s.get("name") == name), None)
                if not sess:
                    self._send(404, '{"error":"no encontrada"}')
                    return
                def _do():
                    res = chatgpt_fetch_one(sess)
                    with _state_lock:
                        current = list(_state.get("chatgpt", []))
                        for i, s in enumerate(current):
                            if s.get("name") == name:
                                current[i] = res
                                break
                        else:
                            current.append(res)
                        current.sort(key=lambda x: x.get("name", ""))
                        _state["chatgpt"] = current
                threading.Thread(target=_do, daemon=True).start()
                self._send(202, json.dumps({"ok": True, "name": name}))
            else:
                threading.Thread(target=chatgpt_refresh_all, daemon=True).start()
                self._send(202, '{"ok":true}')
        elif parsed.path == "/api/zai/session":
            data = self._read_json()
            if not isinstance(data, dict) or not data.get("name") or not data.get("token"):
                self._send(400, '{"error":"se requiere name y token"}')
                return
            name = data["name"].strip()
            token = data["token"].strip()
            zai_save_session(name, {"token": token})
            threading.Thread(target=zai_refresh_all, daemon=True).start()
            self._send(201, json.dumps({"ok": True, "name": name}))
        elif parsed.path == "/api/zai/refresh":
            q = parse_qs(parsed.query)
            if "name" in q:
                name = q["name"][0]
                sess = next((s for s in zai_list_sessions() if s.get("name") == name), None)
                if not sess:
                    self._send(404, '{"error":"no encontrada"}')
                    return
                def _do_zai():
                    res = zai_fetch_one(sess)
                    with _state_lock:
                        current = list(_state.get("zai", []))
                        for i, s in enumerate(current):
                            if s.get("name") == name:
                                current[i] = res
                                break
                        else:
                            current.append(res)
                        current.sort(key=lambda x: x.get("name", ""))
                        _state["zai"] = current
                threading.Thread(target=_do_zai, daemon=True).start()
                self._send(202, json.dumps({"ok": True, "name": name}))
            else:
                threading.Thread(target=zai_refresh_all, daemon=True).start()
                self._send(202, '{"ok":true}')
        elif parsed.path == "/api/ollama/session":
            data = self._read_json()
            if not isinstance(data, dict) or not data.get("name") or not data.get("cookie"):
                self._send(400, '{"error":"se requiere name y cookie"}')
                return
            name = data["name"].strip()
            cookie = data["cookie"].strip()
            ollama_save_session(name, {"cookie": cookie})
            threading.Thread(target=ollama_refresh_all, daemon=True).start()
            self._send(201, json.dumps({"ok": True, "name": name}))
        elif parsed.path == "/api/ollama/refresh":
            q = parse_qs(parsed.query)
            if "name" in q:
                name = q["name"][0]
                sess = next((s for s in ollama_list_sessions() if s.get("name") == name), None)
                if not sess:
                    self._send(404, '{"error":"no encontrada"}')
                    return
                def _do_ollama():
                    res = ollama_scrape(sess.get("cookie", ""))
                    res["name"] = name
                    ollama_save_session(name, {"cookie": sess.get("cookie", ""), **res})
                    with _state_lock:
                        current = list(_state.get("ollama", []))
                        for i, s in enumerate(current):
                            if s.get("name") == name:
                                current[i] = res
                                break
                        else:
                            current.append(res)
                        current.sort(key=lambda x: x.get("name", ""))
                        _state["ollama"] = current
                threading.Thread(target=_do_ollama, daemon=True).start()
                self._send(202, json.dumps({"ok": True, "name": name}))
            else:
                threading.Thread(target=ollama_refresh_all, daemon=True).start()
                self._send(202, '{"ok":true}')
        else:
            self._send(404, '{"error":"not found"}')

    def do_DELETE(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path == "/api/session" and "name" in q:
            name = q["name"][0]
            ok = delete_session(name)
            if ok:
                threading.Thread(target=refresh_scraped, daemon=True).start()
                self._send(200, json.dumps({"ok": True, "name": name}))
            else:
                self._send(404, json.dumps({"error": "no encontrada"}))
        elif parsed.path == "/api/chatgpt/session" and "name" in q:
            name = q["name"][0]
            ok = chatgpt_delete_session(name)
            if ok:
                threading.Thread(target=chatgpt_refresh_all, daemon=True).start()
                self._send(200, json.dumps({"ok": True, "name": name}))
            else:
                self._send(404, json.dumps({"error": "no encontrada"}))
        elif parsed.path == "/api/zai/session" and "name" in q:
            name = q["name"][0]
            ok = zai_delete_session(name)
            if ok:
                threading.Thread(target=zai_refresh_all, daemon=True).start()
                self._send(200, json.dumps({"ok": True, "name": name}))
            else:
                self._send(404, json.dumps({"error": "no encontrada"}))
        elif parsed.path == "/api/ollama/session" and "name" in q:
            name = q["name"][0]
            ok = ollama_delete_session(name)
            if ok:
                threading.Thread(target=ollama_refresh_all, daemon=True).start()
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
