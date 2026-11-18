#!/usr/bin/env bash
# =============================================================================
# harden_server.sh — Endurecimiento de un Ubuntu 24.04 (Hetzner Cloud) para marc-agent.
#
# Ejecutar UNA vez como root en el servidor recién creado (con tu clave SSH ya instalada
# por Hetzner):   sudo bash harden_server.sh --user marc
#
# Qué hace:
#   1. Actualiza el sistema e instala ufw, fail2ban, unattended-upgrades, Docker y Tailscale.
#   2. Crea el usuario no-root con sudo (contraseña la escribes TÚ; sirve para la consola de
#      emergencia de Hetzner) y le copia tus claves SSH.
#   3. SSH: solo clave, sin root, sin contraseñas, solo tu usuario.
#   4. Tailscale + Tailscale SSH (la auth key la pegas TÚ, oculta; o login por URL).
#   5. Firewall por fases con verificación: abre tailscale0, mantiene el 22 público hasta que
#      confirmas acceso por Tailscale; después cierra el 22 con ROLLBACK AUTOMÁTICO (se reabre
#      solo a los 10 min) si no vuelves a confirmar desde una sesión nueva por Tailscale.
#   6. fail2ban, actualizaciones automáticas de seguridad y sysctl básicos.
#
# No toca: la Cloud Firewall de Hetzner (se configura en la consola: sin reglas de entrada).
# =============================================================================
set -Eeuo pipefail

ADMIN_USER="marc"
CONFIRM_TIMEOUT=600   # segundos para confirmar acceso por Tailscale en cada fase
TS_HOSTNAME="marc-agent"
LOG=/var/log/marc-hardening.log
STATE_DIR=/run/marc-hardening

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) ADMIN_USER="$2"; shift 2 ;;
    --hostname) TS_HOSTNAME="$2"; shift 2 ;;
    --timeout) CONFIRM_TIMEOUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Argumento desconocido: $1"; exit 2 ;;
  esac
done

exec > >(tee -a "$LOG") 2>&1
log()  { printf '\n\033[1;34m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m❌ %s\033[0m\n' "$*"; exit 1; }
trap 'warn "Error en la línea $LINENO. El puerto 22 sigue abierto salvo que la fase 2 lo cerrara (y su rollback está programado)."' ERR

[[ $EUID -eq 0 ]] || die "Ejecuta como root (sudo bash $0 --user $ADMIN_USER)."
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "Este script está pensado para Ubuntu (detectado: ${ID:-?})."
[[ "$ADMIN_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "Nombre de usuario inválido."
[[ -s /root/.ssh/authorized_keys ]] || die "No hay /root/.ssh/authorized_keys: añade tu clave SSH al crear el servidor."
export DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------------
log "1/8 Actualizando el sistema e instalando paquetes base"
apt-get update -y
apt-get full-upgrade -y
apt-get install -y ca-certificates curl gnupg ufw fail2ban unattended-upgrades apt-listchanges \
  openssl jq make git

# -----------------------------------------------------------------------------
log "2/8 Usuario administrador no-root: $ADMIN_USER"
if ! id "$ADMIN_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$ADMIN_USER"
fi
usermod -aG sudo "$ADMIN_USER"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
install -m 600 -o "$ADMIN_USER" -g "$ADMIN_USER" /root/.ssh/authorized_keys "/home/$ADMIN_USER/.ssh/authorized_keys"
if passwd -S "$ADMIN_USER" | awk '{print $2}' | grep -qE '^(L|NP)$'; then
  echo "Escribe una contraseña FUERTE para $ADMIN_USER (para sudo y la consola de emergencia de Hetzner)."
  echo "No se mostrará. Guárdala en tu gestor de contraseñas."
  until passwd "$ADMIN_USER"; do warn "Inténtalo de nuevo."; done
fi

# -----------------------------------------------------------------------------
log "3/8 Endureciendo SSH (solo clave, sin root, sin contraseñas)"
cat > /etc/ssh/sshd_config.d/00-marc-hardening.conf <<EOF
# Gestionado por harden_server.sh (00- tiene prioridad sobre 50-cloud-init.conf)
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
AllowUsers $ADMIN_USER
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
sshd -t || die "Configuración de sshd inválida; no se aplica."
systemctl reload ssh || systemctl reload sshd
warn "Las sesiones abiertas siguen vivas. NO cierres esta sesión hasta terminar."

# -----------------------------------------------------------------------------
log "4/8 Docker Engine (repositorio oficial)"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "5"},
  "live-restore": true,
  "no-new-privileges": true,
  "userland-proxy": false
}
EOF
systemctl enable --now docker
systemctl restart docker
usermod -aG docker "$ADMIN_USER"
install -d -m 750 -o "$ADMIN_USER" -g "$ADMIN_USER" /opt/marc-agent

# -----------------------------------------------------------------------------
log "5/8 Tailscale + Tailscale SSH"
if ! command -v tailscale >/dev/null; then
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update -y
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled
if [[ "$(tailscale status --json 2>/dev/null | jq -r '.BackendState // empty')" != "Running" ]]; then
  TS_AUTHKEY="${TS_AUTHKEY:-}"
  if [[ -z "$TS_AUTHKEY" ]]; then
    read -rsp "Pega tu Tailscale auth key (no se mostrará; Enter = login por URL): " TS_AUTHKEY; echo
  fi
  if [[ -n "$TS_AUTHKEY" ]]; then
    tailscale up --ssh --hostname="$TS_HOSTNAME" --auth-key="$TS_AUTHKEY"
  else
    echo "Abre en tu navegador la URL que aparece a continuación e inicia sesión en Tailscale:"
    tailscale up --ssh --hostname="$TS_HOSTNAME"
  fi
  unset TS_AUTHKEY
fi
TS_IP="$(tailscale ip -4 | head -n1)"
[[ -n "$TS_IP" ]] || die "Tailscale no tiene IP. Revisa 'tailscale status'."
log "Tailscale activo: $TS_HOSTNAME ($TS_IP)"

# -----------------------------------------------------------------------------
log "6/8 Firewall (ufw) — fase 1: tailscale0 abierto, 22 público TODAVÍA abierto"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0 comment 'tailnet'
ufw allow 22/tcp comment 'temporal-hardening'
ufw --force enable
ufw status verbose

install -d -m 770 -o root -g "$ADMIN_USER" "$STATE_DIR"
cat > /usr/local/bin/marc-confirm-access <<'EOF'
#!/usr/bin/env bash
# Confirma que esta sesión SSH llega por Tailscale (100.64.0.0/10 o fd7a:115c:a1e0::/48).
set -euo pipefail
ip="${SSH_CONNECTION%% *}"
[[ -n "${SSH_CONNECTION:-}" ]] || { echo "Ejecuta esto dentro de una sesión SSH."; exit 1; }
if [[ "$ip" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. || "$ip" == fd7a:115c:a1e0:* ]]; then
  date -Is > /run/marc-hardening/confirmed
  echo "✅ Acceso por Tailscale confirmado desde $ip"
else
  echo "❌ Esta sesión viene de $ip, no de Tailscale. Conecta con: ssh $(whoami)@<IP-tailscale>"
  exit 1
fi
EOF
chmod 755 /usr/local/bin/marc-confirm-access

cat > /usr/local/sbin/marc-fw-rollback <<'EOF'
#!/usr/bin/env bash
# Rollback automático: reabre el 22 público si no se confirmó el acceso por Tailscale.
ufw allow 22/tcp comment 'rollback-automatico'
logger -t marc-hardening "ROLLBACK: puerto 22 reabierto por falta de confirmación"
EOF
chmod 700 /usr/local/sbin/marc-fw-rollback

wait_confirmation() {
  local deadline=$((SECONDS + CONFIRM_TIMEOUT))
  rm -f "$STATE_DIR/confirmed"
  while (( SECONDS < deadline )); do
    [[ -f "$STATE_DIR/confirmed" ]] && return 0
    sleep 3
  done
  return 1
}

echo
echo "=========================================================================="
echo " VERIFICACIÓN 1/2 — Abre OTRA terminal en tu equipo (con Tailscale activo):"
echo "     ssh $ADMIN_USER@$TS_IP        (o: ssh $ADMIN_USER@$TS_HOSTNAME)"
echo " y dentro ejecuta:   marc-confirm-access"
echo " Tienes $((CONFIRM_TIMEOUT / 60)) minutos. El puerto 22 público sigue abierto mientras tanto."
echo "=========================================================================="
if ! wait_confirmation; then
  die "No se confirmó el acceso por Tailscale. NO se cierra el 22. Revisa Tailscale y vuelve a ejecutar el script."
fi
log "Acceso por Tailscale confirmado (1/2)"

# -----------------------------------------------------------------------------
log "7/8 Firewall — fase 2: cierro el 22 público con rollback automático armado"
systemctl stop marc-fw-rollback.timer marc-fw-rollback.service 2>/dev/null || true
systemctl reset-failed marc-fw-rollback.timer marc-fw-rollback.service 2>/dev/null || true
systemd-run --unit=marc-fw-rollback --on-active="${CONFIRM_TIMEOUT}s" /usr/local/sbin/marc-fw-rollback >/dev/null
while ufw status numbered | grep -q 'temporal-hardening'; do
  ufw --force delete "$(ufw status numbered | grep 'temporal-hardening' | head -n1 | sed -E 's/^\[ *([0-9]+)\].*/\1/')"
done
ufw status verbose
echo
echo "=========================================================================="
echo " VERIFICACIÓN 2/2 — Con el 22 público YA CERRADO, abre una sesión NUEVA:"
echo "     ssh $ADMIN_USER@$TS_IP   y ejecuta:   marc-confirm-access"
echo " Si no confirmas en $((CONFIRM_TIMEOUT / 60)) min, el 22 se reabrirá solo (rollback)."
echo "=========================================================================="
if wait_confirmation; then
  systemctl stop marc-fw-rollback.timer 2>/dev/null || true
  log "Confirmado (2/2). Rollback cancelado: SSH solo por Tailscale."
else
  warn "Sin confirmación: el rollback reabrirá el puerto 22 en breve. Revisa Tailscale y repite."
fi

# -----------------------------------------------------------------------------
log "8/8 fail2ban, actualizaciones automáticas y sysctl"
cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 ::1 100.64.0.0/10

[sshd]
enabled = true
backend = systemd
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
cat > /etc/apt/apt.conf.d/52marc-unattended <<'EOF'
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
systemctl enable --now unattended-upgrades

cat > /etc/sysctl.d/99-marc-hardening.conf <<'EOF'
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.tcp_syncookies = 1
kernel.dmesg_restrict = 1
kernel.kptr_restrict = 2
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
EOF
sysctl --system >/dev/null

log "✅ Hardening completado"
cat <<EOF

Resumen:
  - Usuario: $ADMIN_USER (sudo con contraseña; SSH solo con clave).
  - SSH: root deshabilitado, sin contraseñas, AllowUsers $ADMIN_USER.
  - Tailscale: $TS_HOSTNAME ($TS_IP) con Tailscale SSH.
  - ufw: entrada denegada salvo tailscale0. Ningún puerto público.
  - fail2ban (sshd), unattended-upgrades (reinicio 04:30 si hace falta), sysctl básicos.
  - Docker instalado; $ADMIN_USER en el grupo docker (cierra sesión y vuelve a entrar).

Pendiente en la consola de Hetzner (Firewalls): crea una Cloud Firewall SIN reglas de entrada
y aplícala a este servidor.

Acceso de emergencia: consola web de Hetzner (Servers > tu servidor > Console), entra como
$ADMIN_USER con su contraseña y ejecuta 'sudo ufw allow 22/tcp' o 'sudo tailscale up --ssh'.
Log de este proceso: $LOG
EOF
