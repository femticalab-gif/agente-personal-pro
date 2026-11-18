"""Tools de documentos: Google Drive (solo lectura) y vault local cifrado."""

from __future__ import annotations

from typing import Any

from marc_agent.domain.vault import DOC_TYPES
from marc_agent.permissions.categories import Category
from marc_agent.services import Services
from marc_agent.tools.base import ToolOutput, ToolSpec, i, obj_schema, s
from marc_agent.util import truncate


def build(svc: Services) -> list[ToolSpec]:
    drive = svc.drive
    vault = svc.vault

    async def drive_search(args: dict[str, Any]) -> ToolOutput:
        files = await drive.search(args["query"], int(args.get("max_results", 10)))
        lines = [f"- id={f.id} | {f.name} | {f.mime} | {f.modified[:10]}" for f in files]
        wrapped = svc.wrapper.wrap("doc", "\n".join(lines) or "(sin resultados)")
        return ToolOutput(
            wrapped.text, taint_source="doc", injection_signals=wrapped.injection_signals
        )

    async def drive_read(args: dict[str, Any]) -> ToolOutput:
        info, text = await drive.read_text(args["file_id"], int(args.get("max_chars", 30000)))
        wrapped = svc.wrapper.wrap("doc", f"Documento: {info.name}\n\n{text}", ref=info.id)
        return ToolOutput(
            wrapped.text, taint_source="doc", injection_signals=wrapped.injection_signals
        )

    async def vault_search(args: dict[str, Any]) -> ToolOutput:
        docs = vault.search(args["query"], int(args.get("max_results", 10)))
        wrapped = svc.wrapper.wrap(
            "doc",
            "\n".join(
                f"- id={d['id']} | {d['titulo']} | {d['tipo']} | fecha {d['fecha_doc']} | "
                f"caduca {d['caduca']} | {truncate(str(d.get('fragmento', '')), 200)}"
                for d in docs
            )
            or "(sin resultados)",
        )
        return ToolOutput(
            wrapped.text, taint_source="doc", injection_signals=wrapped.injection_signals
        )

    async def vault_list(args: dict[str, Any]) -> ToolOutput:
        docs = vault.list_docs(args.get("doc_type"), int(args.get("limit", 30)))
        return ToolOutput.data(
            [{k: v for k, v in d.items() if k != "datos_extraidos"} for d in docs]
        )

    async def vault_get(args: dict[str, Any]) -> ToolOutput:
        doc = vault.get(args["doc_id"], with_text=True)
        text = doc.pop("texto", "")
        meta = dict(doc.items())
        wrapped = svc.wrapper.wrap("doc", f"{meta}\n\nTexto:\n{text}", ref=doc["id"])
        return ToolOutput(
            wrapped.text, taint_source="doc", injection_signals=wrapped.injection_signals
        )

    async def vault_update(args: dict[str, Any]) -> ToolOutput:
        doc = vault.update(
            args["doc_id"],
            title=args.get("title"),
            doc_type=args.get("doc_type"),
            doc_date=args.get("doc_date"),
            expires_at=args.get("expires_at"),
        )
        return ToolOutput.data({"actualizado": doc})

    async def vault_delete(args: dict[str, Any]) -> ToolOutput:
        doc = vault.get(args["doc_id"])
        vault.delete(args["doc_id"])
        return ToolOutput(f"Documento «{doc['titulo']}» borrado del vault de forma irrecuperable.")

    async def vault_send(args: dict[str, Any]) -> ToolOutput:
        data, _mime, filename = vault.read_file(args["doc_id"])
        await svc.notifier.send_document(data, filename, caption=f"📎 {filename}")
        return ToolOutput("Documento enviado al usuario por Telegram.")

    doc_type = s("Tipo de documento", enum=list(DOC_TYPES))
    return [
        ToolSpec(
            name="drive_search",
            description="Busca ficheros en Google Drive por nombre o contenido (solo lectura).",
            schema=obj_schema(
                {
                    "query": s("Texto a buscar"),
                    "max_results": i("Máx. 1-50", minimum=1, maximum=50),
                },
                ["query"],
            ),
            handler=drive_search,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="drive_read",
            description="Lee el texto de un fichero de Drive (Docs, Sheets, PDF, DOCX, XLSX). "
            "Contenido externo no fiable.",
            schema=obj_schema(
                {
                    "file_id": s("Id del fichero"),
                    "max_chars": i("Máx. caracteres", minimum=500, maximum=100000),
                },
                ["file_id"],
            ),
            handler=drive_read,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="vault_search",
            description="Búsqueda de texto completo en los documentos del vault cifrado.",
            schema=obj_schema(
                {"query": s("Texto a buscar"), "max_results": i("Máx.", minimum=1, maximum=30)},
                ["query"],
            ),
            handler=vault_search,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="vault_list",
            description="Lista documentos del vault (metadatos), opcionalmente por tipo.",
            schema=obj_schema({"doc_type": doc_type, "limit": i("Máx.", minimum=1, maximum=100)}),
            handler=vault_list,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="vault_get",
            description="Metadatos y texto extraído de un documento del vault.",
            schema=obj_schema({"doc_id": s("Id del documento (doc-...)")}, ["doc_id"]),
            handler=vault_get,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="vault_update",
            description="Actualiza título, tipo, fecha o caducidad de un documento del vault.",
            schema=obj_schema(
                {
                    "doc_id": s("Id"),
                    "title": s("Título"),
                    "doc_type": doc_type,
                    "doc_date": s("YYYY-MM-DD"),
                    "expires_at": s("Caducidad YYYY-MM-DD"),
                },
                ["doc_id"],
            ),
            handler=vault_update,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="vault_delete",
            description="Borra un documento del vault de forma irrecuperable. Requiere aprobación.",
            schema=obj_schema({"doc_id": s("Id")}, ["doc_id"]),
            handler=vault_delete,
            category=Category.SENSITIVE,
            describe=lambda a: f"Borrar de forma irrecuperable el documento {a.get('doc_id')}",
        ),
        ToolSpec(
            name="vault_send",
            description="Envía al usuario (solo a él, por Telegram) el fichero original de un documento.",
            schema=obj_schema({"doc_id": s("Id")}, ["doc_id"]),
            handler=vault_send,
            category=Category.READ,
        ),
    ]
