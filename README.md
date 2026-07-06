# quota-dashboard

Dashboard web para consultar cuotas, uso y limites de multiples cuentas de **OpenAI**, **OpenCode** y **ChatGPT Plus/Pro + Codex** desde el navegador.

## Que muestra

### ChatGPT Plus / Pro + Codex (device-code OAuth)
- Email de la cuenta
- Plan (Plus / Pro / Team / Enterprise)
- Medidores de uso Codex: rolling 5h y semanal (% usado y tiempo de reset)

Usa el OAuth device-code flow oficial de OpenAI (`auth.openai.com`). Login headless una sola vez por cuenta: el dashboard te da un codigo, lo ingresas en `auth.openai.com/codex/device`, y se guarda un **refresh token** permanente. Sin cookies, sin Cloudflare, sin re-login. Refresco automatico cada 5 min.

### OpenCode — saldo real (scraping via cookie de sesion)
- Email de la cuenta
- Plan (Black / Go / Pay-as-you-go)
- Saldo actual (USD)
- Uso del mes y limite mensual (con % de uso)
- Medidores de uso Go: rolling 5h, semanal, mensual (% y tiempo de reset)
- Auto-reload (monto y trigger)
- Workspace ID

Funciona pegando la cookie `auth` de opencode.ai. El server scrapea `/workspace/<id>/billing` y `/workspace/<id>/go` cada 5 min. La cookie dura ~1 año.

### OpenAI (claves admin `sk-admin-*`)
- Organizacion
- Gasto acumulado del mes en curso (USD) + desglose por item (top 10)
- Numero de modelos disponibles
- Rate limits por modelo (req/min, tokens/min)

Las claves normales `sk-*` solo permiten ver validez y conteo de modelos (OpenAI no expone costos sin permisos de organizacion).

## Configuracion

Edita `config.json` con tus cuentas OpenAI (opcional):

```json
{
  "port": 8765,
  "host": "0.0.0.0",
  "refresh_seconds": 300,
  "scrape_refresh_seconds": 300,
  "openai_accounts": [
    { "name": "openai-admin-1", "key": "sk-admin-..." }
  ]
}
```

Las cuentas de **ChatGPT/Codex** y de **OpenCode (scraping via cookie)** se añaden desde el formulario del dashboard (no en config.json); se guardan en `chatgpt_sessions/` y `sessions/` respectivamente (gitignored).

### Requisito previo para ChatGPT/Codex

Antes de iniciar el login de una cuenta de ChatGPT, activa **Device-code login** en:
`chatgpt.com` → Settings → Account → Security → "Allow device code login".

## Uso con Docker

```bash
docker compose up -d --build
```

Abre http://localhost:8765

Para detener:

```bash
docker compose down
```

## Uso sin Docker

```bash
python3 server.py
```

Abre http://localhost:8765 (requiere Python 3.10+, sin dependencias externas).

## Endpoints

| Ruta | Descripcion |
|------|-------------|
| `GET /` | Dashboard HTML |
| `GET /api/data` | Estado actual en JSON |
| `GET /api/config` | Config sin claves |
| `GET /api/sessions` | Sesiones OpenCode (sin cookies) |
| `GET /api/chatgpt/sessions` | Sesiones ChatGPT (sin tokens) |
| `GET /api/chatgpt/login/status?device_auth_id=...` | Estado de un login en curso |
| `POST /api/refresh` | Forzar refresco de todo |
| `POST /api/session` | Añadir cuenta OpenCode (body `{name,cookie}`) |
| `POST /api/session/scrape?name=...` | Re-scrape de una cuenta OpenCode |
| `DELETE /api/session?name=...` | Eliminar cuenta OpenCode |
| `POST /api/chatgpt/login` | Iniciar device-code flow de ChatGPT |
| `POST /api/chatgpt/login/poll?device_auth_id=...` | Poll del device-code |
| `POST /api/chatgpt/refresh?name=...` | Refrescar una cuenta ChatGPT |
| `DELETE /api/chatgpt/session?name=...` | Eliminar cuenta ChatGPT |

## Seguridad

- El dashboard escucha en `0.0.0.0:8765` por defecto para funcionar en Docker. Para acceso solo local, cambia `host` a `127.0.0.1` en `config.json`.
- Las claves viven solo en `config.json` (montado read-only en el contenedor). No se exponen en `/api/config`.
- No habilites `host: 0.0.0.0` en produccion sin un proxy/auth enfrente.
