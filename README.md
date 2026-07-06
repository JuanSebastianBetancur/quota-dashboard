# quota-dashboard

Dashboard web unificado para consultar cuotas, uso y limites de multiples cuentas de **OpenAI** y **OpenCode Zen / Go** desde el navegador.

## Que muestra

### OpenAI (claves admin `sk-admin-*`)
- Organizacion
- Gasto acumulado del mes en curso (USD) + desglose por item (top 10)
- Numero de modelos disponibles
- Rate limits por modelo (req/min, tokens/min)

Las claves normales `sk-*` solo permiten ver validez y conteo de modelos (OpenAI no expone costos sin permisos de organizacion).

### OpenCode Zen / Go (claves de opencode.ai/auth)
- Validez de la clave
- Tier detectado (zen / go)
- Endpoint usado
- Numero de modelos disponibles
- Lista de modelos gratuitos

**Nota importante:** OpenCode **no expone el saldo/créditos via API key**. Esa informacion solo esta en el dashboard web tras login en https://opencode.ai/auth. El script valida la clave y lista modelos; el saldo real debe revisarse ahi.

## Configuracion

Edita `config.json` con tus cuentas:

```json
{
  "port": 8765,
  "host": "0.0.0.0",
  "refresh_seconds": 300,
  "openai_accounts": [
    { "name": "openai-admin-1", "key": "sk-admin-..." }
  ],
  "opencode_accounts": [
    { "name": "opencode-zen-1", "key": "oc_zen_..." },
    { "name": "opencode-go-1",  "key": "oc_go_..."  }
  ]
}
```

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
| `POST /api/refresh` | Forzar refresco |

## Seguridad

- El dashboard escucha en `0.0.0.0:8765` por defecto para funcionar en Docker. Para acceso solo local, cambia `host` a `127.0.0.1` en `config.json`.
- Las claves viven solo en `config.json` (montado read-only en el contenedor). No se exponen en `/api/config`.
- No habilites `host: 0.0.0.0` en produccion sin un proxy/auth enfrente.
