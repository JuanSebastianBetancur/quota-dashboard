# quota-dashboard

Dashboard web para consultar cuotas, uso y limites de multiples cuentas de **OpenAI** y **OpenCode** desde el navegador.

## Que muestra

### OpenAI (claves admin `sk-admin-*`)
- Organizacion
- Gasto acumulado del mes en curso (USD) + desglose por item (top 10)
- Numero de modelos disponibles
- Rate limits por modelo (req/min, tokens/min)

Las claves normales `sk-*` solo permiten ver validez y conteo de modelos (OpenAI no expone costos sin permisos de organizacion).

### OpenCode — saldo real (scraping via cookie de sesion)
- Email de la cuenta
- Plan (Black / Go / Pay-as-you-go)
- Saldo actual (USD)
- Uso del mes y limite mensual (con % de uso)
- Medidores de uso Go: rolling 5h, semanal, mensual (% y tiempo de reset)
- Auto-reload (monto y trigger)
- Workspace ID

Funciona pegando la cookie `auth` de opencode.ai (ver instrucciones en el propio dashboard). El server scrapea `/workspace/<id>/billing` y `/workspace/<id>/go` cada 5 min (~configurable). La cookie dura ~1 año, así que no requiere re-login.

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

Las cuentas de **OpenCode (scraping via cookie)** se añaden desde el formulario del dashboard (no en config.json); se guardan en `sessions/` (gitignored).

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
| `GET /api/sessions` | Sesiones scrapeadas (sin cookies) |
| `POST /api/refresh` | Forzar refresco de todo |
| `POST /api/session` | Añadir cuenta de scraping (body `{name,cookie}`) |
| `POST /api/session/scrape` | Forzar re-scrape |
| `DELETE /api/session?name=...` | Eliminar cuenta de scraping |

## Seguridad

- El dashboard escucha en `0.0.0.0:8765` por defecto para funcionar en Docker. Para acceso solo local, cambia `host` a `127.0.0.1` en `config.json`.
- Las claves viven solo en `config.json` (montado read-only en el contenedor). No se exponen en `/api/config`.
- No habilites `host: 0.0.0.0` en produccion sin un proxy/auth enfrente.
