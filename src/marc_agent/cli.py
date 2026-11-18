"""Punto de entrada ``marc-agent``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets as pysecrets
import stat
import sys
from pathlib import Path

from marc_agent.config import AppConfig, ConfigError, Secrets, load_config
from marc_agent.crypto import Cipher, generate_key

DEFAULT_CONFIG = os.environ.get("MARC_CONFIG", "/app/config.yaml")


def _load(args: argparse.Namespace, require_all: bool = True) -> tuple[AppConfig, Secrets]:
    config_path = args.config if Path(args.config).exists() else None
    config = load_config(config_path)
    secrets = Secrets.from_env(require_all=require_all)
    return config, secrets


def cmd_run(args: argparse.Namespace) -> int:
    from marc_agent.app import run

    config, secrets = _load(args)
    asyncio.run(run(config, secrets))
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    from marc_agent.integrations import make_http_client
    from marc_agent.ops.watchdog import Watchdog

    # Mínimo privilegio: el watchdog solo recibe el token del bot y el id de Marc.
    config, secrets = _load(args, require_all=False)
    if not secrets.telegram_bot_token or secrets.telegram_user_id <= 0:
        raise ConfigError("El watchdog necesita TELEGRAM_BOT_TOKEN y TELEGRAM_USER_ID")

    async def _main() -> None:
        http = make_http_client(config.network.effective_proxy)
        dog = Watchdog(
            config.heartbeat_file,
            config.schedule.watchdog_stale_minutes * 60,
            http,
            secrets.telegram_bot_token,
            secrets.telegram_user_id,
        )
        await dog.run_forever()

    asyncio.run(_main())
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    from marc_agent.ops.watchdog import healthcheck

    config = load_config(args.config if Path(args.config).exists() else None)
    return 0 if healthcheck(config.heartbeat_file, max_age_s=300) else 1


def cmd_google_auth(args: argparse.Namespace) -> int:
    from marc_agent.db import Database
    from marc_agent.integrations import make_http_client
    from marc_agent.integrations.google.auth import GoogleAuth, pkce_pair

    config, secrets = _load(args)

    async def _main() -> None:
        http = make_http_client(config.network.effective_proxy)
        db = Database(config.db_path, Cipher(secrets.data_key, secrets.data_key_previous))
        auth = GoogleAuth(db, http, secrets.google_client_id, secrets.google_client_secret)
        verifier, challenge = pkce_pair()
        state = pysecrets.token_urlsafe(16)
        print(
            "1) Abre esta URL en el navegador de TU equipo e inicia sesión con tu cuenta de Google:\n"
        )
        print(auth.build_auth_url(state, challenge))
        print(
            "\n2) Tras aceptar, el navegador irá a http://127.0.0.1:8765/... y mostrará un error "
            "de conexión: es normal.\n3) Copia la URL COMPLETA de la barra de direcciones y pégala "
            "aquí (no se mostrará):"
        )
        redirect = getpass.getpass("URL: ")
        code = auth.parse_redirect(redirect, state)
        await auth.exchange_code(code, verifier)
        print("✅ Google autorizado. Scopes concedidos:", " ".join(auth.granted_scopes()))
        await http.aclose()
        db.close()

    asyncio.run(_main())
    return 0


def _backups(config: AppConfig, secrets: Secrets):  # type: ignore[no-untyped-def]
    from marc_agent.ops.backup import BackupManager

    return BackupManager(
        config.backup_dir,
        config.tmp_dir,
        config.vault_dir,
        secrets.backup_key,
        config.schedule.backup_keep,
    )


def cmd_backup(args: argparse.Namespace) -> int:
    from marc_agent.db import Database

    config, secrets = _load(args)
    db = Database(config.db_path, Cipher(secrets.data_key, secrets.data_key_previous))
    path = _backups(config, secrets).create(db)
    db.close()
    print(f"Backup creado: {path}")
    return 0


def cmd_list_backups(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    for path in _backups(config, secrets).list_backups():
        print(path.name, path.stat().st_size)
    return 0


def cmd_verify_backup(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    manager = _backups(config, secrets)
    target = Path(args.file) if args.file else (manager.list_backups() or [None])[-1]
    if target is None:
        print("No hay backups")
        return 1
    manifest = manager.verify(Path(target))
    print(
        f"OK: {target} ({len(manifest['members'])} ficheros, esquema v{manifest['schema_version']})"
    )
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    manager = _backups(config, secrets)
    target = Path(args.file) if args.file else (manager.list_backups() or [None])[-1]
    if target is None:
        print("No hay backups")
        return 1
    if not args.yes:
        answer = input(
            f"Restaurar {Path(target).name}? El agente debe estar parado. [escribe SI]: "
        )
        if answer.strip() != "SI":
            print("Cancelado")
            return 1
    manifest = manager.restore(Path(target), config.db_path)
    print(
        f"Restaurado {target} (copia previa en *.pre-restore). Esquema v{manifest['schema_version']}"
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from marc_agent.app import build_services
    from marc_agent.ops.export import export_encrypted

    config, secrets = _load(args)
    svc = build_services(config, secrets)
    data, name = export_encrypted(svc, secrets.export_key)
    out = Path(args.out or config.data_dir / name)
    out.write_bytes(data)
    print(f"Export cifrado: {out}")
    return 0


def cmd_decrypt_export(args: argparse.Namespace) -> int:
    from marc_agent.ops.export import decrypt_export

    key = os.environ.get("MARC_EXPORT_KEY") or getpass.getpass("MARC_EXPORT_KEY (no se mostrará): ")
    data = decrypt_export(Path(args.input).read_bytes(), key.strip())
    import json

    Path(args.output).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"Descifrado en {args.output}. Guárdalo en un lugar seguro y bórralo cuando no lo necesites."
    )
    return 0


def cmd_init_keys(args: argparse.Namespace) -> int:
    """Añade claves nuevas al .env si faltan, sin mostrarlas por pantalla."""
    env_path = Path(args.env_file)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    present = {
        line.split("=", 1)[0].strip()
        for line in existing.splitlines()
        if "=" in line and line.split("=", 1)[1].strip()
    }
    added = []
    lines = []
    for name in ("MARC_DATA_KEY", "MARC_BACKUP_KEY", "MARC_EXPORT_KEY"):
        if name not in present:
            lines.append(f"{name}={generate_key()}")
            added.append(name)
    if lines:
        cleaned = "\n".join(
            line for line in existing.splitlines() if line.split("=", 1)[0].strip() not in added
        )
        env_path.write_text(
            (cleaned.rstrip() + "\n" if cleaned.strip() else "") + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    print("Claves añadidas: " + (", ".join(added) if added else "ninguna (ya existían)"))
    print("Guarda una copia de MARC_BACKUP_KEY y MARC_EXPORT_KEY en tu gestor de contraseñas.")
    return 0


def cmd_rotate_key(args: argparse.Namespace) -> int:
    from marc_agent.db import Database

    config, secrets = _load(args)
    if not secrets.data_key_previous:
        print("Pon la clave nueva en MARC_DATA_KEY y la antigua en MARC_DATA_KEY_PREVIOUS.")
        return 1
    cipher = Cipher(secrets.data_key, secrets.data_key_previous)
    db = Database(config.db_path, cipher)
    fields = db.rotate_encryption()
    files = 0
    for path in config.vault_dir.glob("*.bin"):
        path.write_bytes(cipher.rotate(path.read_bytes()))
        files += 1
    db.close()
    print(
        f"Re-cifrados {fields} campos y {files} ficheros. Ya puedes quitar MARC_DATA_KEY_PREVIOUS."
    )
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from marc_agent.ops.selftest import run_selftest

    config, secrets = _load(args)
    checks = asyncio.run(run_selftest(config, secrets, agent=args.agent, brave=args.brave))
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"{'✅' if check.ok else '❌'} {check.name.ljust(width)}  {check.detail}")
    return 0 if all(c.ok for c in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marc-agent", description="Agente personal privado de Marc"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Ruta de config.yaml")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Arranca el agente (por defecto)").set_defaults(func=cmd_run)
    sub.add_parser("watchdog", help="Watchdog de heartbeat").set_defaults(func=cmd_watchdog)
    sub.add_parser("healthcheck", help="Healthcheck de Docker").set_defaults(func=cmd_healthcheck)
    sub.add_parser("google-auth", help="Autoriza Google (OAuth)").set_defaults(func=cmd_google_auth)
    sub.add_parser("backup", help="Crea un backup cifrado").set_defaults(func=cmd_backup)
    sub.add_parser("list-backups", help="Lista backups").set_defaults(func=cmd_list_backups)
    p = sub.add_parser("verify-backup", help="Verifica un backup")
    p.add_argument("--file")
    p.set_defaults(func=cmd_verify_backup)
    p = sub.add_parser("restore", help="Restaura un backup (agente parado)")
    p.add_argument("--file")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("export", help="Export cifrado a fichero")
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("decrypt-export", help="Descifra un export (en tu equipo)")
    p.add_argument("--in", dest="input", required=True)
    p.add_argument("--out", dest="output", required=True)
    p.set_defaults(func=cmd_decrypt_export)
    p = sub.add_parser("init-keys", help="Genera claves de cifrado en el .env")
    p.add_argument("--env-file", default=".env")
    p.set_defaults(func=cmd_init_keys)
    sub.add_parser("rotate-key", help="Re-cifra con la clave nueva").set_defaults(
        func=cmd_rotate_key
    )
    p = sub.add_parser("selftest", help="Autodiagnóstico")
    p.add_argument(
        "--agent", action="store_true", help="Incluye prueba real del agente (coste mínimo)"
    )
    p.add_argument("--brave", action="store_true", help="Incluye una búsqueda real")
    p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", cmd_run)
    try:
        return int(func(args))
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
