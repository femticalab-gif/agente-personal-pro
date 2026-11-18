"""Backups cifrados (AES-256-GCM por bloques) de la base de datos y el vault, y restore.

Formato: ``backup-YYYYmmdd-HHMMSS.tar.enc`` = tar con ``manifest.json``, ``db/marc.sqlite3``
(snapshot consistente con la API de backup de SQLite) y ``vault/*.bin`` (ya cifrados con la
clave de datos). Restaurar requiere tener el agente parado (``make restore``).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from marc_agent.crypto import CryptoError, StreamEncryptor, sha256_hex
from marc_agent.db import Database

PURPOSE = "backup"
_ALLOWED_PREFIXES = ("db/", "vault/", "manifest.json")


class BackupError(RuntimeError):
    pass


class BackupManager:
    def __init__(
        self, backup_dir: Path, tmp_dir: Path, vault_dir: Path, backup_key: str, keep: int = 14
    ) -> None:
        self.backup_dir = backup_dir
        self.tmp_dir = tmp_dir
        self.vault_dir = vault_dir
        self._enc = StreamEncryptor(backup_key, PURPOSE)
        self.keep = keep

    def list_backups(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("backup-*.tar.enc"))

    def create(self, db: Database) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        snapshot = self.tmp_dir / f"snapshot-{stamp}.sqlite3"
        tar_path = self.tmp_dir / f"backup-{stamp}.tar"
        final = self.backup_dir / f"backup-{stamp}.tar.enc"
        try:
            db.backup_to(snapshot)
            members: dict[str, str] = {"db/marc.sqlite3": sha256_hex(snapshot.read_bytes())}
            vault_files = sorted(self.vault_dir.glob("*.bin")) if self.vault_dir.exists() else []
            for vf in vault_files:
                members[f"vault/{vf.name}"] = sha256_hex(vf.read_bytes())
            manifest = {
                "format": 1,
                "created": stamp,
                "schema_version": db.schema_version,
                "members": members,
            }
            with tarfile.open(tar_path, "w") as tar:
                data = json.dumps(manifest, indent=1).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                tar.add(snapshot, arcname="db/marc.sqlite3")
                for vf in vault_files:
                    tar.add(vf, arcname=f"vault/{vf.name}")
            tmp_final = final.with_suffix(".partial")
            with tar_path.open("rb") as src, tmp_final.open("wb") as dst:
                self._enc.encrypt_stream(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            tmp_final.replace(final)
        finally:
            for p in (snapshot, tar_path):
                p.unlink(missing_ok=True)
        self.prune()
        return final

    def prune(self) -> list[Path]:
        backups = self.list_backups()
        removed = backups[: max(0, len(backups) - self.keep)]
        for path in removed:
            path.unlink(missing_ok=True)
        return removed

    def _decrypt_to_tar(self, path: Path) -> Path:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="restore-", suffix=".tar", dir=self.tmp_dir)
        try:
            with path.open("rb") as src, os.fdopen(fd, "wb") as dst:
                for chunk in self._enc.decrypt_stream(src):
                    dst.write(chunk)
        except CryptoError as exc:
            Path(tmp).unlink(missing_ok=True)
            raise BackupError(f"No se pudo descifrar el backup: {exc}") from exc
        return Path(tmp)

    def verify(self, path: Path) -> dict[str, Any]:
        """Descifra y valida un backup (manifiesto, hashes, integridad de SQLite)."""
        tar_path = self._decrypt_to_tar(path)
        workdir = Path(tempfile.mkdtemp(prefix="verify-", dir=self.tmp_dir))
        try:
            manifest = self._extract(tar_path, workdir)
            self._check_sqlite(workdir / "db" / "marc.sqlite3")
            return manifest
        finally:
            tar_path.unlink(missing_ok=True)
            shutil.rmtree(workdir, ignore_errors=True)

    def _extract(self, tar_path: Path, workdir: Path) -> dict[str, Any]:
        with tarfile.open(tar_path, "r") as tar:
            for member in tar.getmembers():
                name = member.name
                if (
                    name.startswith("/")
                    or ".." in Path(name).parts
                    or not name.startswith(_ALLOWED_PREFIXES)
                    or not (member.isfile() or member.isdir())
                ):
                    raise BackupError(f"Entrada no permitida en el backup: {name!r}")
            tar.extractall(workdir, filter="data")
        manifest: dict[str, Any] = json.loads((workdir / "manifest.json").read_text())
        for name, digest in manifest.get("members", {}).items():
            file_path = workdir / name
            if not file_path.exists() or sha256_hex(file_path.read_bytes()) != digest:
                raise BackupError(f"Hash incorrecto o fichero ausente: {name}")
        return manifest

    @staticmethod
    def _check_sqlite(db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if row is None or row[0] != "ok":
            raise BackupError("La base de datos del backup no supera integrity_check")

    def restore(self, path: Path, db_path: Path) -> dict[str, Any]:
        """Restaura un backup. El agente debe estar parado. Guarda copia previa ``*.pre-restore``."""
        tar_path = self._decrypt_to_tar(path)
        workdir = Path(tempfile.mkdtemp(prefix="restore-", dir=self.tmp_dir))
        try:
            manifest = self._extract(tar_path, workdir)
            new_db = workdir / "db" / "marc.sqlite3"
            self._check_sqlite(new_db)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            if db_path.exists():
                shutil.copy2(db_path, db_path.with_suffix(".pre-restore"))
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            shutil.copy2(new_db, db_path)
            if self.vault_dir.exists():
                previous = self.vault_dir.with_name(self.vault_dir.name + ".pre-restore")
                shutil.rmtree(previous, ignore_errors=True)
                self.vault_dir.rename(previous)
            self.vault_dir.mkdir(parents=True, exist_ok=True)
            extracted_vault = workdir / "vault"
            if extracted_vault.exists():
                for vf in extracted_vault.glob("*.bin"):
                    shutil.copy2(vf, self.vault_dir / vf.name)
            return manifest
        finally:
            tar_path.unlink(missing_ok=True)
            shutil.rmtree(workdir, ignore_errors=True)

    def wipe_all(self) -> int:
        count = 0
        for path in self.list_backups():
            path.unlink(missing_ok=True)
            count += 1
        return count
