"""Detección heurística de intentos de prompt injection en contenido externo.

No pretende ser perfecta: la defensa real es estructural (permisos en código, aprobación
humana, FORBIDDEN inamovible). Esta heurística sirve para avisar al usuario y para que
nada con forma de instrucción se guarde en memoria.
"""

from __future__ import annotations

import re

from marc_agent.util import normalize

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignorar_instrucciones",
        re.compile(
            r"\b(ignora\w*|olvida\w*|omite|descarta|ignore|disregard|forget|oblida)\b.{0,40}"
            r"\b(instruc\w*|reglas?|normas?|indicaciones|rules|instructions|prompt|regles)\b"
        ),
    ),
    (
        "nuevas_instrucciones",
        re.compile(
            r"\b(nuevas instrucciones|new instructions|noves instruccions|system prompt|"
            r"prompt del sistema|mensaje del sistema|system message)\b"
        ),
    ),
    (
        "reenviar_todo",
        re.compile(
            r"\b(reenvia\w*|forward\w*|envia\w*|send|manda\w*|comparte\w*|share)\b.{0,30}"
            r"\b(todo|todos|todas|all|everything|tot|tots|cada correo|every email)\b"
        ),
    ),
    (
        "cambio_de_rol",
        re.compile(
            r"\b(ahora eres|a partir de ahora eres|you are now|act as|actua como|"
            r"finge que|pretend to be|ara ets)\b"
        ),
    ),
    (
        "desactivar_seguridad",
        re.compile(
            r"\b(desactiva\w*|deshabilita\w*|disable|bypass|salta\w*|elimina\w*|quita\w*|"
            r"desconnecta\w*|apaga\w*)\b.{0,40}"
            r"\b(reglas?|seguridad|restricci\w*|proteccion\w*|filtros?|rules|safety|security|"
            r"guardrails?|bloqueos?|regles|seguretat)\b"
        ),
    ),
    (
        "ocultar_al_usuario",
        re.compile(
            r"\b(no (se lo )?(digas|avises|informes|comentes|menciones)|"
            r"don.?t (tell|inform|mention)|sin avisar|without telling)\b"
        ),
    ),
    (
        "suplantacion_usuario",
        re.compile(
            r"\b(marc (te )?(autoriza|ordena|pide que)|el usuario (ha autorizado|autoriza)|"
            r"user (has )?authori[sz]ed|pre-?aprobad\w*|pre-?approved)\b"
        ),
    ),
]

_DIRECTIVE = re.compile(
    r"\b(a partir de ahora|de ahora en adelante|from now on|de ara endavant|en adelante|"
    r"siempre que|cada vez que|whenever)\b.{0,80}"
    r"\b(envia\w*|reenvia\w*|responde\w*|contesta\w*|ejecuta\w*|ignora\w*|comparte\w*|"
    r"transfier\w*|paga\w*|borra\w*|elimina\w*|send|forward|reply|delete|pay)\b"
    r"|\b(debes|tienes que|has de|you must|you should|deberas)\b.{0,40}"
    r"\b(enviar|reenviar|responder|ejecutar|ignorar|compartir|transferir|pagar|borrar|"
    r"send|forward|reply|ignore|share|pay|delete)\b"
)


def detect_injection(text: str) -> list[str]:
    """Devuelve las etiquetas de patrones de inyección detectados (vacío si ninguno)."""
    norm = normalize(text)
    return [label for label, pattern in _INJECTION_PATTERNS if pattern.search(norm)]


def looks_like_instruction(text: str) -> bool:
    """¿Parece una orden dirigida al agente? Se usa para impedir guardarla en memoria."""
    norm = normalize(text)
    return bool(detect_injection(text)) or bool(_DIRECTIVE.search(norm))


_RULE_TAMPER = re.compile(
    r"\b(desactiva\w*|deshabilita\w*|quita\w*|elimina\w*|ignora\w*|salta\w*|suspende\w*|"
    r"anula\w*|levanta\w*|disable|bypass|turn off|remove|desactiva-?la|treu\w*)\b.{0,50}"
    r"\b(regla\w*|restricci\w*|bloqueo\w*|proteccion\w*|prohibici\w*|forbidden|"
    r"seguridad|clasificador\w*|rules?|regles?|seguretat)\b"
)


def is_rule_tamper_request(text: str) -> bool:
    """El usuario (o alguien con su móvil) intenta desactivar reglas por chat."""
    return bool(_RULE_TAMPER.search(normalize(text)))
