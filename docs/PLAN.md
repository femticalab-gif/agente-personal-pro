# Plan de construcción de marc-agent

Documento de trabajo: decisiones técnicas tomadas tras leer la documentación vigente del
Claude Agent SDK (Python 0.2.167, CLI empaquetado) y cómo se mapea cada requisito.

## Decisiones clave (y por qué)

| Tema | Decisión | Motivo |
|---|---|---|
| Núcleo del agente | `ClaudeSDKClient` con `tools=["Agent"]`, `setting_sources=[]`, `strict_mcp_config=True`, `permission_mode="dontAsk"` y todas las herramientas propias en un servidor MCP *in-process* (`create_sdk_mcp_server`, nombre `marc`) | `tools` sin built-ins = sin shell, sin ficheros, sin WebFetch. Solo queda `Agent` para subagentes. Nada de settings, CLAUDE.md, skills ni plugins de disco. |
| Permisos | Hook `PreToolUse` (se ejecuta antes que cualquier otra regla y un `deny` gana siempre) + guardia en el ejecutor de cada tool | Defensa en profundidad: si el hook no se ejecutase, el ejecutor exige el ticket de aprobación y vuelve a comprobar FORBIDDEN. |
| Aprobaciones | El hook espera la respuesta de Telegram (botones) con timeout 10 min ⇒ rechazo. Ticket de un solo uso ligado a `sha256(tool+args)` | El ejecutor solo ejecuta una acción SENSITIVE si hay ticket válido para exactamente esos parámetros. |
| Subagentes | `AgentDefinition` solo con tools READ; el hook fuerza `run_in_background=false` y rechaza cualquier SENSITIVE/FORBIDDEN pedida desde un subagente; profundidad 1, máx. 4 concurrentes | Paralelismo sin ampliar superficie de ataque. |
| Sesiones | Una sesión por chat; `SessionStore` propio cifrado (Fernet) en SQLite; `CLAUDE_CONFIG_DIR` en tmpfs (RAM) | Los transcripts nunca quedan en claro en disco. Reanudación tras reinicio desde el store cifrado. |
| Compactación | Auto-compactación del CLI con ventana reducida (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=150000`) + cierre por inactividad con resumen (modelo económico) que se inyecta en la siguiente sesión | Coste acotado y continuidad. |
| Modelos | Principal `claude-opus-5-5` (razonamiento y acciones), económico `claude-haiku-5-5` (clasificación, resúmenes, recordatorios), visión con el principal. Configurable | Enrutado por tarea. Prompt caching automático del CLI para system+tools y `cache_control` en llamadas directas. |
| Cifrado en reposo | Cifrado de campos con Fernet/MultiFernet (rotación) en SQLite; vault en ficheros cifrados; FTS5 en una base **en memoria** reconstruida al arrancar | FTS5 en disco guardaría tokens en claro. SQLCipher descartado por portabilidad de binarios. |
| Backups | Tar del snapshot SQLite + vault, cifrado AES-256-GCM por bloques (clave propia `MARC_BACKUP_KEY`) | Streaming, detecta truncado/reordenación. Restore verificado con `integrity_check`. |
| Egress | Red Docker `internal` sin salida + proxy Squid con allowlist de dominios | Cero puertos de entrada (long polling) y salida solo a los dominios permitidos. |
| STT | Google Cloud Speech-to-Text v1 (`speech.googleapis.com`) | Anthropic no ofrece transcripción; Google ya es proveedor (mismo proyecto de Cloud). Español con catalán como idioma alternativo. |
| Búsqueda | Brave Search API, solo resultados (sin fetch de páginas) | Requisito. Resultados envueltos como no fiables. |
| Clima | Open-Meteo (sin clave) | Necesario para el briefing; dominio añadido a la allowlist. |

## Estructura

```
src/marc_agent/
  config.py          configuración (config.yaml) + secretos (.env), nunca se loguean
  crypto.py          Fernet/MultiFernet, HKDF, AES-GCM por bloques
  db/                SQLite (esquema, migraciones), repos cifrados, índice FTS5 en memoria
  audit/             log JSON estructurado con redacción y rotación
  budget/            contador de gasto API, avisos 80 % / bloqueo 100 %
  llm/               llamadas directas a la API (modelo económico y visión)
  security/          contenido no fiable, clasificador económico/salud, fraude, inyección, taint
  permissions/       categorías, motor de decisión, aprobaciones, hooks del SDK, estado (/stop, dry-run)
  integrations/      Google (OAuth, Gmail, Calendar, Drive), Brave, STT, clima, parseo de documentos
  domain/            servicios de finanzas, salud, tareas, notas, recordatorios, contactos, memoria, vault
  tools/             catálogo de tools propias (registro + servidor MCP in-process)
  agent/             prompts, runtime del Agent SDK, session store cifrado, onboarding
  gateway/           bot de Telegram (allowlist, rate limit, streaming, comandos, botones)
  scheduler/         briefing, vigilancia de correo, recordatorios, resumen semanal, backups, heartbeat
  ops/               backup/restore, export, borrado total, watchdog, selftest
  cli.py             punto de entrada (run, watchdog, healthcheck, google-auth, backup, restore, ...)
tests/               pytest con mocks de Telegram, Google, Anthropic, búsqueda y STT
```

## Orden de ejecución

1. Documentación del SDK ✔
2. Estructura y plan ✔ (este documento)
3. Código + tests hasta verde (ruff, mypy estricto, cobertura ≥ 85 %)
4. Configuración externa por Chrome (Google Cloud, Anthropic, Hetzner, Tailscale, BotFather, Brave)
5. Hardening (`scripts/harden_server.sh`) y despliegue guiado
6. Onboarding conversacional
