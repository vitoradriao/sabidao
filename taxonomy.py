"""Classificação determinística para ingestão, sem dependências operacionais."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


REGISTRY_PATH = Path(__file__).resolve().parent / "contracts/canonical-docs/v1/vocabulary.json"
RULE_VERSION = "taxonomy_v1"
LEGACY_MODULE = {
    "configuracao": "parametros_configuracao",
    "pedidos_vendas": "pedidos_vendas",
    "logistica": "rotas_visitas_consultas",
    "financeiro": "financeiro_pagamentos",
    "sql_integracao": "sql_integracao",
    "unknown": "geral",
}
LEGACY_ANSWER_MODE = {
    "procedure": "process",
    "configuration": "configuration",
    "reference": "general",
    "troubleshooting": "troubleshooting",
    "sql_lookup": "sql_lookup",
    "unknown": "general",
}
CANONICAL_MODULE = {value: key for key, value in LEGACY_MODULE.items()}
CANONICAL_MODULE.update({
    "campanhas_descontos": "pedidos_vendas",
    "suporte_processos": "unknown",
    "gestao_operacional": "unknown",
    "glossario": "unknown",
})
CANONICAL_ANSWER_MODE = {
    "process": "procedure",
    "integration": "reference",
    "general": "unknown",
    "configuration": "configuration",
    "troubleshooting": "troubleshooting",
    "sql_lookup": "sql_lookup",
}


@dataclass(frozen=True)
class Classification:
    primary_module: str
    secondary_modules: tuple[str, ...]
    answer_mode: str
    reason: str
    signals: tuple[str, ...]
    products: tuple[str, ...] = ()
    legacy_module_value: str | None = None
    legacy_answer_mode_value: str | None = None

    def metadata(self) -> dict:
        return {
            "taxonomy_version": RULE_VERSION,
            "primary_module": self.primary_module,
            "secondary_modules": list(self.secondary_modules),
            "answer_mode": self.answer_mode,
            "reason": self.reason,
            "signals": list(self.signals),
            "products": list(self.products),
            "legacy_module": self.legacy_module,
            "legacy_answer_mode": self.legacy_answer_mode,
        }

    @property
    def legacy_module(self) -> str:
        return self.legacy_module_value or LEGACY_MODULE[self.primary_module]

    @property
    def legacy_answer_mode(self) -> str:
        return self.legacy_answer_mode_value or LEGACY_ANSWER_MODE[self.answer_mode]


@lru_cache(maxsize=1)
def registry() -> dict:
    value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if value["vocabulary_version"] != RULE_VERSION:
        raise ValueError("versão de taxonomia desconhecida")
    return value


def _validate(module: str, secondary: tuple[str, ...], answer_mode: str,
              products: tuple[str, ...] = ()) -> None:
    vocabulary = registry()
    if module not in vocabulary["modules"] or any(
        item not in vocabulary["modules"] for item in secondary
    ):
        raise ValueError("módulo fora do vocabulário")
    if answer_mode not in vocabulary["answer_modes"]:
        raise ValueError("modo de resposta fora do vocabulário")
    if any(product not in vocabulary["products"] for product in products):
        raise ValueError("produto fora do vocabulário")


def editorial(taxonomy: dict, override: dict | None = None,
              products: tuple[str, ...] = ()) -> Classification:
    override = override or {}
    if taxonomy.get("taxonomy_version") != RULE_VERSION:
        raise ValueError("versão de taxonomia desconhecida")
    module = override.get("primary_module", taxonomy["primary_module"])
    secondary = tuple(override.get("secondary_modules", taxonomy["secondary_modules"]))
    answer_mode = override.get("answer_mode", taxonomy["answer_mode"])
    effective_products = tuple(override.get("products", products))
    _validate(module, secondary, answer_mode, effective_products)
    changed = tuple(key for key in ("products", "primary_module", "secondary_modules", "answer_mode") if key in override)
    return Classification(
        module,
        secondary,
        answer_mode,
        "editorial_override" if changed else "editorial_inheritance",
        changed,
        effective_products,
    )


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    unaccented = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", unaccented).strip()


def heuristic(
    *, title: str, content: str, legacy_module: str, legacy_answer_mode: str
) -> Classification:
    heading = _normalized(title)
    body = _normalized(content)
    signals: list[str] = []
    module = CANONICAL_MODULE.get(legacy_module, "unknown")
    preserve_legacy = True
    # A finalidade expressa no título prevalece; tabela isolada só decide
    # fichas curtas, onde não é uma referência incidental entre muitos assuntos.
    sql_heading = re.search(r"\b(sql|select|consulta sql)\b", heading)
    parameter_heading = re.search(r"\b(parametr[oa]s?|configurac(?:ao|oes))\b", heading)
    parameter_ficha = len(content) <= 500 and re.search(r"\bmxs?parametros?\b", body)
    if sql_heading and legacy_module == "sql_integracao":
        module = "sql_integracao"
        signals.append("sql_heading")
        preserve_legacy = False
    elif parameter_heading or parameter_ficha:
        module = "configuracao"
        signals.append("parameter_configuration")
        preserve_legacy = False
    elif re.search(r"\b(permissao|usuario|perfil de acesso)\b", heading):
        module = "configuracao"
        signals.append("user_permission")
        preserve_legacy = False
    elif re.search(r"\b(conta corrente|pagamento|financeiro|boleto)\b", heading):
        module = "financeiro"
        signals.append("finance")
        preserve_legacy = False
    elif module == "unknown" and re.search(
        r"\b(pedidos?|vendas?|orcamentos?|campanhas?|descontos?)\b", heading
    ):
        module = "pedidos_vendas"
        signals.append("order_heading")
        preserve_legacy = False
    elif module == "unknown" and re.search(
        r"\b(rotas?|visitas?|logistica|entregas?)\b", heading
    ):
        module = "logistica"
        signals.append("logistics_heading")
        preserve_legacy = False
    elif module == "unknown" and re.search(r"\b(sql|select|tabela|banco de dados)\b", heading):
        module = "sql_integracao"
        signals.append("sql_heading")
        preserve_legacy = False
    else:
        signals.append("legacy_module")

    mode = CANONICAL_ANSWER_MODE.get(legacy_answer_mode, "unknown")
    if re.search(r"\b(erro|falha|problema|nao sincroniza|nao aparece)\b", heading):
        mode = "troubleshooting"
        signals.append("troubleshooting")
    elif sql_heading and module == "sql_integracao":
        mode = "sql_lookup"
        signals.append("sql_lookup")
    elif module == "configuracao" and (parameter_heading or parameter_ficha or "user_permission" in signals):
        mode = "configuration"
        signals.append("configuration")
    elif re.search(r"\b(fluxo|passo a passo|procedimento)\b", heading):
        mode = "procedure"
        signals.append("procedure")
    secondary = ("financeiro",) if module == "configuracao" and re.search(
        r"\b(conta corrente|pagamento|financeiro|boleto)\b", heading
    ) else ()
    _validate(module, secondary, mode)
    return Classification(
        module, secondary, mode, "heuristic", tuple(signals),
        legacy_module_value=legacy_module if preserve_legacy else None,
        legacy_answer_mode_value=(
            legacy_answer_mode
            if mode == CANONICAL_ANSWER_MODE.get(legacy_answer_mode, "unknown")
            else None
        ),
    )
