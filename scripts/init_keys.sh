#!/usr/bin/env bash
# Genera las claves de cifrado que falten en el .env SIN mostrarlas por pantalla.
# Una clave Fernet = 32 bytes aleatorios en base64 url-safe.
set -euo pipefail
umask 077

ENV_FILE="${1:-.env}"
[[ -f "$ENV_FILE" ]] || { echo "No existe $ENV_FILE (cp .env.example .env)"; exit 1; }
command -v openssl >/dev/null || { echo "Falta openssl"; exit 1; }

added=()
for name in MARC_DATA_KEY MARC_BACKUP_KEY MARC_EXPORT_KEY; do
  current="$(grep -E "^${name}=" "$ENV_FILE" | head -n1 | cut -d= -f2- || true)"
  if [[ -z "$current" ]]; then
    key="$(openssl rand -base64 32 | tr '+/' '-_')"
    if grep -qE "^${name}=" "$ENV_FILE"; then
      tmp="$(mktemp)"
      awk -v n="$name" -v k="$key" 'BEGIN{FS=OFS="="} $1==n {print n"="k; next} {print}' "$ENV_FILE" > "$tmp"
      cat "$tmp" > "$ENV_FILE" && rm -f "$tmp"
    else
      printf '%s=%s\n' "$name" "$key" >> "$ENV_FILE"
    fi
    added+=("$name")
  fi
done
chmod 600 "$ENV_FILE"
if ((${#added[@]})); then
  echo "Claves añadidas a $ENV_FILE: ${added[*]}"
  echo "Guarda una copia de MARC_BACKUP_KEY y MARC_EXPORT_KEY en tu gestor de contraseñas."
else
  echo "Ninguna clave añadida (ya existían)."
fi
