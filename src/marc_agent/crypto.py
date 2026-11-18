"""Primitivas criptográficas.

* ``Cipher``: cifrado de campos con Fernet (AES-128-CBC + HMAC-SHA256) y rotación de
  claves con MultiFernet (la primera clave cifra; las anteriores solo descifran).
* ``derive_key``: HKDF-SHA256 para derivar subclaves con propósito separado.
* ``StreamEncryptor``: AES-256-GCM por bloques para backups y exports grandes. Cada
  bloque lleva su índice y una marca de "último" en el AAD, de modo que reordenar,
  duplicar o truncar el fichero se detecta al descifrar.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from collections.abc import Iterator
from typing import IO, Any

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class CryptoError(RuntimeError):
    """Fallo de cifrado o descifrado (clave errónea o datos manipulados)."""


def generate_key() -> str:
    """Genera una clave Fernet nueva (para ``.env``)."""
    return Fernet.generate_key().decode("ascii")


def _key_bytes(key: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CryptoError("Clave con formato inválido (se espera Fernet base64)") from exc
    if len(raw) != 32:
        raise CryptoError("La clave debe tener 32 bytes")
    return raw


def derive_key(master_key: str, purpose: str, length: int = 32) -> bytes:
    """Deriva una subclave determinista para ``purpose`` a partir de una clave maestra."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=b"marc-agent/v1",
        info=purpose.encode("utf-8"),
    )
    return hkdf.derive(_key_bytes(master_key))


class Cipher:
    """Cifrado de campos para la base de datos y el vault."""

    def __init__(self, primary_key: str, previous_keys: tuple[str, ...] | list[str] = ()) -> None:
        try:
            fernets = [Fernet(primary_key.encode("ascii"))]
            fernets += [Fernet(k.encode("ascii")) for k in previous_keys]
        except (ValueError, UnicodeEncodeError) as exc:
            raise CryptoError("Clave de datos inválida") from exc
        self._multi = MultiFernet(fernets)
        self._hmac_key = derive_key(primary_key, "blind-index")

    def encrypt_bytes(self, data: bytes) -> bytes:
        return self._multi.encrypt(data)

    def decrypt_bytes(self, token: bytes) -> bytes:
        try:
            return self._multi.decrypt(token)
        except InvalidToken as exc:
            raise CryptoError("No se pudo descifrar (clave incorrecta o datos alterados)") from exc

    def encrypt_json(self, obj: Any) -> bytes:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self.encrypt_bytes(raw.encode("utf-8"))

    def decrypt_json(self, token: bytes) -> Any:
        return json.loads(self.decrypt_bytes(token).decode("utf-8"))

    def encrypt_str(self, text: str) -> bytes:
        return self.encrypt_bytes(text.encode("utf-8"))

    def decrypt_str(self, token: bytes) -> str:
        return self.decrypt_bytes(token).decode("utf-8")

    def rotate(self, token: bytes) -> bytes:
        """Re-cifra un token con la clave primaria (para rotación de claves)."""
        try:
            return self._multi.rotate(token)
        except InvalidToken as exc:
            raise CryptoError("No se pudo rotar el token") from exc

    def blind_index(self, value: str) -> str:
        """HMAC determinista para buscar por igualdad sin guardar el valor en claro."""
        normalized = value.strip().lower().encode("utf-8")
        return hmac.new(self._hmac_key, normalized, hashlib.sha256).hexdigest()


_MAGIC = b"MARCENC1"
_CHUNK = 1024 * 1024
_HEADER = struct.Struct(">8s8s")  # magic, nonce prefix


class StreamEncryptor:
    """AES-256-GCM por bloques con protección frente a truncado y reordenación."""

    def __init__(self, master_key: str, purpose: str) -> None:
        self._aead = AESGCM(derive_key(master_key, purpose))

    def encrypt_stream(self, src: IO[bytes], dst: IO[bytes], chunk_size: int = _CHUNK) -> None:
        prefix = os.urandom(8)
        dst.write(_HEADER.pack(_MAGIC, prefix))
        index = 0
        current = src.read(chunk_size)
        while True:
            nxt = src.read(chunk_size)
            last = not nxt
            nonce = prefix + struct.pack(">I", index)
            aad = struct.pack(">I?", index, last)
            sealed = self._aead.encrypt(nonce, current, aad)
            dst.write(struct.pack(">I", len(sealed)))
            dst.write(sealed)
            if last:
                break
            current = nxt
            index += 1

    def decrypt_stream(self, src: IO[bytes]) -> Iterator[bytes]:
        header = src.read(_HEADER.size)
        if len(header) != _HEADER.size:
            raise CryptoError("Fichero cifrado vacío o truncado")
        magic, prefix = _HEADER.unpack(header)
        if magic != _MAGIC:
            raise CryptoError("Formato de fichero cifrado desconocido")
        index = 0
        while True:
            raw_len = src.read(4)
            if len(raw_len) != 4:
                raise CryptoError("Fichero cifrado truncado (falta el bloque final)")
            (length,) = struct.unpack(">I", raw_len)
            sealed = src.read(length)
            if len(sealed) != length:
                raise CryptoError("Bloque cifrado truncado")
            nonce = prefix + struct.pack(">I", index)
            plain: bytes | None = None
            last = False
            for candidate_last in (False, True):
                try:
                    plain = self._aead.decrypt(
                        nonce, sealed, struct.pack(">I?", index, candidate_last)
                    )
                except InvalidTag:  # probamos ambas marcas de "último"
                    continue
                last = candidate_last
                break
            if plain is None:
                raise CryptoError("Bloque cifrado alterado o clave incorrecta")
            yield plain
            if last:
                if src.read(1):
                    raise CryptoError("Datos extra tras el bloque final")
                return
            index += 1

    def encrypt_bytes(self, data: bytes) -> bytes:
        import io

        src, dst = io.BytesIO(data), io.BytesIO()
        self.encrypt_stream(src, dst)
        return dst.getvalue()

    def decrypt_bytes(self, data: bytes) -> bytes:
        import io

        return b"".join(self.decrypt_stream(io.BytesIO(data)))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
