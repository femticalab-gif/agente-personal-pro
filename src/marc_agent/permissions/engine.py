"""Motor de decisión de permisos.

Orden de evaluación (el primero que decide, gana):

1. Kill switch (/stop) ⇒ deny todo.
2. Herramienta de subagentes (``Agent``): solo desde el agente principal y solo hacia
   subagentes definidos por nosotros.
3. Cualquier herramienta que no sea del catálogo propio ``mcp__marc__*`` ⇒ FORBIDDEN
   (shell, código, ficheros, fetch, MCP de terceros...).
4. Categoría de la tool (estática o dinámica).
5. Comprobaciones FORBIDDEN de la tool (p. ej. responder a un correo marcado como fraude).
6. Contenido saliente ⇒ clasificador económico/salud ⇒ FORBIDDEN si detecta algo.
7. Subagentes: solo READ.
8. SENSITIVE ⇒ dry-run ? simular : pedir aprobación.
9. READ / WRITE_INTERNAL ⇒ permitir (WRITE_INTERNAL queda logueado).
"""

from __future__ import annotations

from typing import Any

from marc_agent.permissions.categories import Category, Decision, Verdict
from marc_agent.permissions.state import ControlState
from marc_agent.security.classifier import OutboundClassifier
from marc_agent.tools.registry import ToolRegistry

AGENT_TOOL_NAMES = frozenset({"Agent", "Task"})


class PolicyEngine:
    def __init__(
        self,
        registry: ToolRegistry,
        state: ControlState,
        classifier: OutboundClassifier,
        allowed_subagents: frozenset[str],
    ) -> None:
        self._registry = registry
        self._state = state
        self._classifier = classifier
        self._allowed_subagents = allowed_subagents

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def evaluate(
        self, tool_name: str, args: dict[str, Any], agent_id: str | None = None
    ) -> Decision:
        if self._state.kill_switch:
            return Decision(
                Verdict.DENY,
                Category.FORBIDDEN,
                tool_name,
                "Kill switch activo (/stop): todas las herramientas están bloqueadas hasta /resume.",
            )

        if tool_name in AGENT_TOOL_NAMES:
            return self._evaluate_subagent_spawn(tool_name, args, agent_id)

        spec = self._registry.resolve_external(tool_name)
        if spec is None:
            return Decision(
                Verdict.DENY,
                Category.FORBIDDEN,
                tool_name,
                f"La herramienta «{tool_name}» no existe en el catálogo permitido. Shell, "
                "ejecución de código, acceso a ficheros, fetch HTTP arbitrario e instalación "
                "de paquetes están prohibidos.",
                notify_user=True,
            )

        summary = spec.summary(args)
        category = await spec.category_for(args)
        if category is Category.FORBIDDEN:
            return Decision(
                Verdict.DENY, category, spec.name, "Acción prohibida por política.", summary, True
            )

        if spec.forbidden_check is not None:
            reason = await spec.forbidden_check(args)
            if reason:
                return Decision(Verdict.DENY, Category.FORBIDDEN, spec.name, reason, summary, True)

        if spec.outbound_text is not None:
            text = await spec.outbound_text(args)
            if text is not None:
                verdict = await self._classifier.classify(text)
                if verdict.blocked:
                    kinds = [
                        k
                        for k, v in (("económico", verdict.economic), ("de salud", verdict.health))
                        if v
                    ]
                    return Decision(
                        Verdict.DENY,
                        Category.FORBIDDEN,
                        spec.name,
                        "FORBIDDEN: el contenido saliente es "
                        + " y ".join(kinds)
                        + f" ({'; '.join(verdict.reasons)}). No se envía a terceros aunque se "
                        "apruebe. Alternativa permitida: gmail_draft (borrador que Marc envía "
                        "manualmente) o un evento sin invitados.",
                        summary,
                        True,
                    )

        if agent_id is not None and category is not Category.READ:
            return Decision(
                Verdict.DENY,
                category,
                spec.name,
                "Los subagentes solo pueden usar herramientas de lectura. Devuelve la propuesta "
                "al agente principal.",
                summary,
            )

        if category is Category.SENSITIVE:
            if self._state.dry_run:
                return Decision(Verdict.SIMULATE, category, spec.name, "Modo dry-run", summary)
            return Decision(Verdict.ASK, category, spec.name, "Requiere aprobación", summary)

        return Decision(Verdict.ALLOW, category, spec.name, "", summary)

    def _evaluate_subagent_spawn(
        self, tool_name: str, args: dict[str, Any], agent_id: str | None
    ) -> Decision:
        if agent_id is not None:
            return Decision(
                Verdict.DENY, Category.FORBIDDEN, tool_name, "Los subagentes no lanzan subagentes."
            )
        subagent = str(args.get("subagent_type", ""))
        if subagent not in self._allowed_subagents:
            return Decision(
                Verdict.DENY,
                Category.FORBIDDEN,
                tool_name,
                f"Subagente no permitido: «{subagent}». Disponibles: "
                + ", ".join(sorted(self._allowed_subagents)),
            )
        return Decision(Verdict.ALLOW, Category.READ, tool_name, "", f"Subagente {subagent}")
