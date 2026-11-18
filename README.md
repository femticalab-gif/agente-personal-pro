# marc-agent — agente personal privado (Telegram + Claude Agent SDK)

Agente mono-usuario, accesible **solo por Telegram**, que gestiona correo, calendario,
documentos, finanzas personales, salud, tareas, notas y recordatorios. Siempre encendido,
proactivo y con memoria. **Prioridad absoluta: seguridad sobre funcionalidades.**

> **Si este agente es para ti:** pon tu nombre en `profile.name` de `config/config.yaml`.
> Es obligatorio: con el nombre vacío el agente no arranca. Ajusta también ciudad, zona
> horaria, coordenadas, idiomas, `own_email` y familia. Los nombres internos (`marc-agent`,
> `MARC_*_KEY`, `mcp__marc__*`) son solo identificadores técnicos: no los cambies.

---

## 1. Arquitectura y modelo de seguridad (una página)

```
Telegram (solo TELEGRAM_USER_ID, chat privado)
   │  long polling (salida), cero puertos de entrada
   ▼
┌──────────────────────── red Docker "internal" (sin gateway) ─────────────────────────┐
│ agent (non-root, FS read-only, cap_drop ALL, no-new-privileges, límites CPU/RAM/PIDs) │
│  gateway ─► cola ─► runtime (Claude Agent SDK, CLI en modo --restricted)              │
│     │                  │  tools: solo `Agent` (subagentes propios) + MCP in-process   │
│     │                  │  `mcp__marc__*` (68 tools propias). Sin Bash/ficheros/fetch. │
│     │                  ▼                                                               │
│     │        hook PreToolUse ─► PolicyEngine ─► aprobación Telegram (10 min = no)      │
│     │                  ▼                                                               │
│     │        ToolExecutor (re-verifica FORBIDDEN, kill switch y ticket de 1 uso)       │
│  scheduler (briefing, correo, recordatorios, backups, heartbeat)                       │
│  SQLite con campos cifrados (Fernet) + vault cifrado + FTS5 solo en RAM               │
│ watchdog (contenedor aparte, solo token del bot) ── avisa si no hay heartbeat >15 min │
└───────────────────────────────┬───────────────────────────────────────────────────────┘
                                ▼
            egress (Squid): allowlist HTTPS → api.anthropic.com, api.telegram.org,
            oauth2/www/gmail/speech.googleapis.com, api.search.brave.com, api.open-meteo.com
```

**Categorías de permiso** (decididas en código, nunca por el modelo):

| Categoría | Ejemplos | Comportamiento |
|---|---|---|
| READ | leer correo, calendario, Drive, vault, BD, búsqueda web | permitido |
| WRITE_INTERNAL | notas, tareas, recordatorios, gastos, memoria, archivar, `gmail_draft`, eventos **sin** invitados | permitido y auditado |
| SENSITIVE | `gmail_send`, eventos **con** invitados, borrar documentos | botones Aprobar/Rechazar con la acción y parámetros exactos; 10 min sin respuesta = rechazo; dry-run = simulación |
| FORBIDDEN | dinero, comunicaciones económicas o de salud a terceros, responder a fraude, shell/código/fetch, cualquier tool fuera del catálogo | rechazo siempre (también en dry-run y aunque lo apruebes), con aviso |

**Defensas en capas**

- **Superficie mínima**: el CLI arranca con `tools=["Agent"]`, `--restricted`, sin skills
  (`--disable-slash-commands`), sin plugins, `setting_sources=[]`, `strict_mcp_config`,
  `permission_mode=dontAsk` y los built-ins prohibidos además en `disallowed_tools`.
  Verificado contra el CLI real: el modelo solo ve `Task` + `mcp__marc__*`.
- **Doble control**: hook `PreToolUse` (gana a cualquier otra regla) + guardia en el
  ejecutor. Una acción SENSITIVE solo se ejecuta con un **ticket de un solo uso** ligado al
  hash de los parámetros aprobados. Fallo interno del control ⇒ denegar.
- **Dinero**: no existe ninguna tool de pago (test que inspecciona el catálogo). Todo texto
  saliente (correos, eventos con invitados, cancelaciones) pasa por el clasificador
  económico/salud: palabras clave (es/ca/en), IBAN, tarjetas (Luhn), importes y, si no hay
  coincidencias, el modelo económico. Si el modelo no responde ⇒ **falla en cerrado**.
  Lo económico solo puede ir a `gmail_draft` (lo envías tú a mano).
- **Fraude/phishing**: correos que piden confirmar pagos, datos bancarios o responder sobre
  dinero se marcan, se te avisa y queda prohibido responder o redactar respuesta a ese
  mensaje, hilo o remitente.
- **Prompt injection**: todo contenido externo va entre `<datos_no_fiables … clave=NONCE>`
  (nonce aleatorio por proceso, etiquetas falsas neutralizadas, texto oculto/invisible
  eliminado, señales de inyección detectadas y comunicadas). Si el turno leyó contenido
  externo, la aprobación lo advierte. La memoria nunca guarda instrucciones; lo que viene de
  fuera se guarda como no fiable y se revisa cada domingo.
- **Reglas FORBIDDEN inamovibles**: están en el system prompt y en código; un mensaje tipo
  «desactiva la regla de dinero» se rechaza sin llegar al modelo. Solo cambian editando el
  código y redesplegando.
- **Subagentes** (agenda, investigador, correo, documentos): solo herramientas READ,
  profundidad 1, máx. 4 en paralelo, `run_in_background=false` forzado por el hook.
- **Cifrado en reposo**: campos sensibles con Fernet/MultiFernet (salud, finanzas, notas,
  memoria, contactos, tokens OAuth, transcripts), ficheros del vault cifrados con verificación
  SHA-256, FTS5 solo en memoria. Transcripts del CLI solo en tmpfs (RAM); la copia
  persistente va cifrada en un `SessionStore` propio. Backups AES-256-GCM por bloques.
- **Auditoría**: log JSON con rotación; se registran tool, categoría, decisión, claves de
  argumentos y un digest HMAC — **nunca el contenido** ni secretos (redacción de tokens,
  claves, IBAN y tarjetas).
- **Presupuesto**: límite diario y mensual; aviso al 80 %, bloqueo al 100 % (`/gasto`).
- **Infraestructura**: Hetzner + Tailscale obligatorio, ufw sin entradas públicas, SSH solo
  por clave y sin root, fail2ban, actualizaciones automáticas, Cloud Firewall sin reglas.

**Riesgos residuales conocidos**: el modelo podría *intentar* acciones indebidas (se
bloquean en código); falsos positivos del clasificador económico (se resuelven con un
borrador); el CLI de Claude Code contiene plugins integrados que se desactivan por
configuración y se verifican en `make selftest-full`.

---

## 2. Checklist de pasos externos

Leyenda: ✅ hecho por Claude (vía navegador) · 👤 pendiente tuyo (login, cuentas, pagos,
términos, secretos) · ⏳ pendiente.

| # | Paso | Quién | Estado |
|---|---|---|---|
| 1 | Telegram: crear el bot con @BotFather y guardar el token en `.env` | 👤 | ⏳ |
| 2 | BotFather: `/setjoingroups` Disable, `/setprivacy` Enable, `/setinline` desactivado, descripción | ✅/👤 | ⏳ |
| 3 | Telegram: obtener tu `TELEGRAM_USER_ID` (p. ej. con @userinfobot) | 👤 | ⏳ |
| 4 | Telegram (tu cuenta): 2FA, código de bloqueo de la app, revisar sesiones activas, autodestrucción de mensajes en el chat del bot (1 semana) | 👤 | ⏳ |
| 5 | Anthropic Console: workspace dedicado, límite de gasto mensual, crear API key → `.env` | ✅/👤 | ⏳ |
| 6 | Google Cloud: proyecto, habilitar Gmail, Calendar, Drive y Speech-to-Text API | ✅ | ⏳ |
| 7 | Google Cloud: pantalla de consentimiento (External, tu email como test user; publicar «In production» para que el refresh token no caduque a los 7 días) | ✅/👤 | ⏳ |
| 8 | Google Cloud: OAuth client «Desktop app» → `GOOGLE_CLIENT_ID/SECRET` en `.env` | ✅/👤 | ⏳ |
| 9 | Google Cloud: API key restringida a Speech-to-Text → `GOOGLE_STT_API_KEY` | ✅/👤 | ⏳ |
| 10 | Brave Search API: alta (plan) y API key → `.env` | 👤 | ⏳ |
| 11 | Tailscale: cuenta, activar MagicDNS, generar auth key de un uso (la pegas tú en el script) | 👤 | ⏳ |
| 12 | Hetzner: crear servidor Ubuntu 24.04 (CX22/CPX21, x86) con tu clave SSH | 👤 (pago) | ⏳ |
| 13 | Hetzner: Cloud Firewall **sin reglas de entrada** aplicada al servidor | ✅ | ⏳ |
| 14 | Servidor: `scripts/harden_server.sh` y verificación por Tailscale | 👤 | ⏳ |
| 15 | Servidor: despliegue (`make init-keys`, `.env`, `make docker-up`, `make google-auth`, `make selftest`) | 👤 | ⏳ |
| 16 | Onboarding conversacional por Telegram (`/start`) | 👤 + agente | ⏳ |

### Despliegue paso a paso (servidor)

```bash
# 1) Desde tu equipo: copia el repo al servidor (por Tailscale tras el hardening)
scp scripts/harden_server.sh root@IP_PUBLICA:/root/    # solo la primera vez
ssh root@IP_PUBLICA 'bash /root/harden_server.sh --user marc'

# 2) Ya por Tailscale, como marc:
# Repo privado: el clon pide tu usuario de GitHub y un token; también puedes copiar la carpeta con rsync/scp
git clone https://github.com/femticalab-gif/agente-personal-pro.git /opt/marc-agent && cd /opt/marc-agent
cp .env.example .env && chmod 600 .env
make init-keys                       # genera las claves de cifrado sin mostrarlas
nano .env                            # pega TÚ los secretos (Telegram, Anthropic, Google, Brave)
make config && nano config/config.yaml
make docker-up                       # construye y arranca egress + agent + watchdog
make google-auth                     # abre la URL en tu navegador y pega la URL de vuelta
make selftest                        # todo en ✅ (egress bloqueado, BotFather, scopes...)
make selftest-full                   # opcional: prueba real del agente (céntimos)
```

---

## 3. Guía de uso

### Comandos

| Comando | Qué hace |
|---|---|
| `/start` | Presentación y onboarding guiado (préstamos y cuotas, tratamientos, citas, contactos clave, preferencias de briefing) |
| `/stop` | **Kill switch**: bloquea todas las herramientas, rechaza aprobaciones pendientes e interrumpe el turno en curso |
| `/resume` | Reactiva el agente |
| `/status` | Estado: kill switch, dry-run, sesión, cola, gasto, Google, heartbeat, backups, próximos jobs |
| `/briefing` | Briefing ahora (agenda, correo importante, vencimientos 7 días, salud, tareas, clima) |
| `/pendientes` | Tareas abiertas, vencimientos de la semana y recordatorios |
| `/memoria ver` · `buscar <texto>` · `purgar <id>` · `purgar externos` | Gestión de la memoria |
| `/dryrun on` · `off` | Simula las acciones sensibles (FORBIDDEN se rechaza igual) |
| `/gasto` | Consumo de API del día y del mes por tipo de uso |
| `/export` | Volcado completo cifrado (JSON) que se descifra en tu equipo con `marc-agent decrypt-export` |
| `/borrar_todo` | Borrado irrecuperable con doble confirmación (botón + código escrito) |

### Ejemplos

- «¿Qué tengo mañana?» · «Prepara el fin de semana» (subagentes en paralelo: agenda + búsqueda + clima).
- «Apunta 23,40 € de Mercadona en supermercado» · o envía la **foto del tique** → botones
  *Registrar gasto / Archivar / Descartar*.
- «Tengo hipoteca de 750 € el día 5» → préstamo + vencimiento mensual. «Avísame el día antes
  de la cuota» → recordatorio ligado que se reprograma solo cada mes.
- «Tomo Omeprazol 20 mg a las 8 y a las 20» → recordatorios con botones ✅ Tomada / ⏭ Saltada
  y adherencia (`¿qué tal mi adherencia este mes?`).
- **Nota de voz** → primero ves la transcripción, luego actúa.
- «Responde a la gestoría que les paso la factura» → crea un **borrador** (dinero ⇒ nunca se envía solo).
- «Escribe a Laura que llego tarde» → pide **aprobación** con el texto exacto.
- «Busca mi contrato de alquiler en Drive y dime cuándo vence».
- Correo sospechoso («confirme la transferencia…») → aviso 🚨 y nunca se responde.

### Proactividad (configurable en `config.yaml`)

Briefing diario 07:45 · vigilancia de correo cada 5 min (solo avisa de lo importante) ·
medicación a su hora · citas el día antes y 2 h antes · vencimientos a 7 y 1 días ·
caducidad de documentos a 30 y 7 días · resumen semanal el domingo con propuesta de limpieza
de memoria · backup cifrado diario 03:30 · aviso si un job falla · watchdog >15 min sin heartbeat.

### Minimización

Por defecto responde con resúmenes. Para ver importes o detalles clínicos pídelo
explícitamente, o fija la preferencia: «pon el nivel de detalle de salud y finanzas en completo».

---

## 4. Rotación de secretos y revocación ante un incidente

Regla general ante sospecha: **`/stop` en Telegram**, `make docker-down` en el servidor,
rotar el secreto afectado, revisar `docker compose logs` y `/data/logs/audit.jsonl`, y
volver a arrancar.

| Secreto | Revocar | Rotar |
|---|---|---|
| Token del bot de Telegram | @BotFather → `/revoke` (invalida el token al instante) | `/token` → nuevo token en `.env` → `make docker-up`. Revisa también las sesiones activas de tu cuenta de Telegram |
| `TELEGRAM_USER_ID` | — | Solo cambia si cambias de cuenta |
| Anthropic API key | Console → API keys → Disable/Delete | Crear nueva en el workspace del agente → `.env` → `make docker-up`. Revisa el uso en Console |
| Google OAuth (refresh token) | myaccount.google.com → Seguridad → Apps con acceso → quitar «marc-agent» (`/borrar_todo` no revoca este token) | `make google-auth` de nuevo |
| Google client secret | Cloud Console → Credentials → OAuth client → Reset secret | Nuevo secreto en `.env` + `make google-auth` |
| Google STT API key | Cloud Console → Credentials → API key → Delete/Regenerate | Nueva key restringida a Speech-to-Text → `.env` |
| Brave API key | Brave Search API dashboard → revocar key | Nueva key → `.env` |
| Tailscale | Admin console → Machines → quitar el nodo; Settings → Keys → revocar auth keys | En el servidor (consola Hetzner si hace falta): `sudo tailscale up --ssh --force-reauth` |
| `MARC_DATA_KEY` | — | Pon la nueva en `MARC_DATA_KEY` y la antigua en `MARC_DATA_KEY_PREVIOUS` → `docker compose run --rm agent rotate-key` → quita la antigua → `make docker-up`. Haz un backup nuevo después |
| `MARC_BACKUP_KEY` | Borra backups antiguos si la clave se filtró | Nueva clave (`make init-keys` tras vaciarla) → `make backup` |
| `MARC_EXPORT_KEY` | Borra exports antiguos | Nueva clave → `.env` |
| Clave SSH | Quitarla de `~/.ssh/authorized_keys` | Añadir la nueva por Tailscale SSH |

Acceso de emergencia al servidor: consola web de Hetzner → usuario `marc` con su contraseña →
`sudo tailscale up --ssh` o, temporalmente, `sudo ufw allow 22/tcp` (ciérralo después).

---

## 5. Actualización mensual

1. En tu equipo: `make update-deps` (hace `uv lock --upgrade`, `uv sync`, lint y tests).
   Revisa el changelog de `claude-agent-sdk`, `anthropic` y `python-telegram-bot`; si cambia
   la API, ajusta y añade tests.
2. Imagen base: comprueba la última `python:3.12.x-slim-trixie` y actualiza el `FROM` del
   `Dockerfile` (y `ghcr.io/astral-sh/uv`).
3. Commit y despliegue: `git pull` en el servidor → `make docker-build docker-up` →
   `make selftest` y `make selftest-full`.
4. `make restore-drill` (verifica que el último backup se descifra y es íntegro).
5. Servidor: `sudo apt update && sudo apt full-upgrade` (los parches de seguridad ya se
   aplican solos), `sudo tailscale update` si procede, y revisa `sudo ufw status`.
6. Revisa `/gasto`, el log de auditoría (decisiones `deny`/`FORBIDDEN`) y la memoria (`/memoria ver`).

---

## Desarrollo

```bash
make install   # uv sync --frozen
make lint      # ruff + mypy --strict
make test      # pytest con cobertura (mínimo 85 %; actual ≈ 91 %)
```

Estructura y decisiones: [docs/PLAN.md](docs/PLAN.md).
