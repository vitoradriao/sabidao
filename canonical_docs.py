"""Validação e inventário offline do contrato documental canônico.

Este módulo não importa configuração de runtime, banco ou clientes de IA. O CLI
serve como gate editorial antes de qualquer ingestão.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from markdown_parser import ParsedMarkdown, parse_markdown


CONTRACT_DIR = Path(__file__).resolve().parent / "contracts" / "canonical-docs" / "v1"
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "contracts" / "canonical-docs" / "manifest.yaml"
BACKUP_DIRECTORY_NAMES = frozenset({"docbkp", "backup", "backups", "bkp"})
IMAGE_RE = re.compile(r"!\[[^]]*\]\(([^)]+)\)")


class CanonicalDocumentError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        rule: str = "canonical-invalid",
        line: int | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rule = rule
        self.line = line
        self.field = field


@dataclass(frozen=True)
class Diagnostic:
    path: str
    rule: str
    severity: str
    message: str
    line: int | None = None
    field: str | None = None

    def render(self) -> str:
        location = self.path
        if self.line is not None:
            location += f":{self.line}"
        if self.field:
            location += f" [{self.field}]"
        return f"{location}: {self.severity}: {self.rule}: {self.message}"


class UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise CanonicalDocumentError(
                f"chave YAML duplicada: {key}",
                rule="yaml-duplicate-key",
                field=str(key),
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _decode_utf8(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        raise CanonicalDocumentError("BOM UTF-8 não é permitido", rule="encoding-bom")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalDocumentError(
            "o arquivo não é UTF-8 válido",
            rule="encoding-utf8",
        ) from exc


def load_yaml_text(text: str) -> dict:
    try:
        for event in yaml.parse(text, Loader=UniqueKeySafeLoader):
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
                raise CanonicalDocumentError(
                    "anchors e aliases YAML não são permitidos",
                    rule="yaml-alias",
                )
        value = yaml.load(text, Loader=UniqueKeySafeLoader)
    except CanonicalDocumentError:
        raise
    except yaml.YAMLError as exc:
        raise CanonicalDocumentError(
            f"YAML inválido ou inseguro: {exc}",
            rule="yaml-invalid",
        ) from exc
    if not isinstance(value, dict):
        raise CanonicalDocumentError(
            "a raiz YAML deve ser um objeto",
            rule="yaml-root",
        )
    return value


def load_yaml(path: Path) -> dict:
    return load_yaml_text(_decode_utf8(path))


def _load_canonical_text(text: str) -> tuple[dict, str, int]:
    if text.startswith("\ufeff"):
        raise CanonicalDocumentError("BOM UTF-8 não é permitido", rule="encoding-bom")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise CanonicalDocumentError(
            "documento canônico deve começar com front matter",
            rule="front-matter-missing",
            line=1,
        )
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise CanonicalDocumentError(
            "front matter sem delimitador de fechamento",
            rule="front-matter-unclosed",
            line=1,
        ) from exc
    document = load_yaml_text("\n".join(lines[1:closing]) + "\n")
    return document, "\n".join(lines[closing + 1 :]), closing + 2


def load_canonical_document(path: Path) -> tuple[dict, str, int]:
    return _load_canonical_text(_decode_utf8(path))


FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date")
def _is_date(value):
    if not isinstance(value, str):
        return True
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@FORMAT_CHECKER.checks("date-time")
def _is_datetime(value):
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


@FORMAT_CHECKER.checks("uuid")
def _is_uuid(value):
    if not isinstance(value, str):
        return True
    try:
        UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _load_contract_assets() -> tuple[dict, dict, dict]:
    def load_json(name: str) -> dict:
        return json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))

    return (
        load_json("document.schema.json"),
        load_json("manifest.schema.json"),
        load_json("vocabulary.json"),
    )


def _validate_schema(instance: dict, schema: dict) -> None:
    validator = Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if not errors:
        return
    error = errors[0]
    field = ".".join(str(part) for part in error.absolute_path) or None
    raise CanonicalDocumentError(
        error.message,
        rule="schema-invalid",
        field=field,
    )


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise CanonicalDocumentError(
            f"{label} duplicado",
            rule=f"duplicate-{label.replace('_', '-')}",
            field=label,
        )


def _validate_document_semantics(document: dict, vocabulary: dict) -> None:
    source_ids = [source["source_id"] for source in document["sources"]]
    _require_unique(source_ids, "source_id")
    known_sources = set(source_ids)
    taxonomy = document["taxonomy"]
    if taxonomy["taxonomy_version"] != vocabulary["vocabulary_version"]:
        raise CanonicalDocumentError("taxonomy_version desconhecida", field="taxonomy.taxonomy_version")
    if any(product not in vocabulary["products"] for product in document["products"]):
        raise CanonicalDocumentError("produto fora do vocabulário", field="products")
    modules = [taxonomy["primary_module"], *taxonomy["secondary_modules"]]
    if any(module not in vocabulary["modules"] for module in modules):
        raise CanonicalDocumentError("módulo fora do vocabulário", field="taxonomy")
    if taxonomy["answer_mode"] not in vocabulary["answer_modes"]:
        raise CanonicalDocumentError("answer_mode fora do vocabulário", field="taxonomy.answer_mode")
    applicability = document["applicability"]
    if any(erp not in vocabulary["erp_systems"] for erp in applicability["erp_systems"]):
        raise CanonicalDocumentError("ERP fora do vocabulário", field="applicability.erp_systems")
    for product_version in applicability["product_versions"]:
        if product_version["product"] not in document["products"]:
            raise CanonicalDocumentError(
                "aplicabilidade referencia produto não declarado",
                field="applicability.product_versions",
            )

    for section_key, section in document["sections"].items():
        override = section["classification_override"] or {}
        if any(product not in vocabulary["products"] for product in override.get("products", [])):
            raise CanonicalDocumentError("override usa produto fora do vocabulário", field=f"sections.{section_key}")
        override_modules = ([override["primary_module"]] if override.get("primary_module") else [])
        override_modules += override.get("secondary_modules", [])
        if any(module not in vocabulary["modules"] for module in override_modules):
            raise CanonicalDocumentError("override usa módulo fora do vocabulário", field=f"sections.{section_key}")
        answer_mode = override.get("answer_mode")
        if answer_mode is not None and answer_mode not in vocabulary["answer_modes"]:
            raise CanonicalDocumentError("override usa answer_mode fora do vocabulário", field=f"sections.{section_key}")
        for source_ref in section["source_refs"]:
            if source_ref["source_id"] not in known_sources:
                raise CanonicalDocumentError(
                    "seção referencia source_id inexistente",
                    field=f"sections.{section_key}.source_refs",
                )
            locator = source_ref["locator"]
            if locator["kind"] in {"line_range", "page_range"} and locator["end"] < locator["start"]:
                raise CanonicalDocumentError("intervalo de origem invertido", field=f"sections.{section_key}.source_refs")


def _validate_document_body(document: dict, body: str, body_start_line: int) -> ParsedMarkdown:
    parsed = parse_markdown(body)
    if parsed.unclosed_fence_line is not None:
        raise CanonicalDocumentError(
            "code fence não fechado",
            rule="fence-unclosed",
            line=body_start_line + parsed.unclosed_fence_line - 1,
        )
    h1 = [heading for heading in parsed.headings if heading.level == 1]
    if len(h1) != 1 or h1[0].title != document["title"]:
        raise CanonicalDocumentError(
            "o corpo deve ter um H1 igual ao title",
            rule="canonical-h1",
            field="title",
        )

    body_sections: dict[str, object] = {}
    previous_level = 1
    for heading in parsed.headings:
        if heading.level == 1:
            previous_level = 1
            continue
        line = body_start_line + heading.line - 1
        if heading.section_key is None:
            raise CanonicalDocumentError(
                f"heading H{heading.level} sem chave estável",
                rule="section-key-missing",
                line=line,
            )
        if heading.level > previous_level + 1:
            raise CanonicalDocumentError(
                "salto de nível de heading",
                rule="heading-level-jump",
                line=line,
            )
        previous_level = heading.level
        if heading.section_key in body_sections:
            raise CanonicalDocumentError(
                f"section_key duplicada: {heading.section_key}",
                rule="section-key-duplicate",
                line=line,
            )
        body_sections[heading.section_key] = heading

    if set(body_sections) != set(document["sections"]):
        raise CanonicalDocumentError(
            "sections e headings do corpo não correspondem",
            rule="section-metadata-mismatch",
            field="sections",
        )
    for section_key, heading in body_sections.items():
        metadata = document["sections"][section_key]
        if heading.title != metadata["title"] or heading.level != metadata["heading_level"]:
            raise CanonicalDocumentError(
                f"metadados divergentes na seção {section_key}",
                rule="section-metadata-mismatch",
                line=body_start_line + heading.line - 1,
                field=f"sections.{section_key}",
            )
    return parsed


def validate_canonical_document(path: Path) -> tuple[dict, ParsedMarkdown]:
    document_schema, _manifest_schema, vocabulary = _load_contract_assets()
    document, body, body_start_line = load_canonical_document(path)
    _validate_schema(document, document_schema)
    _validate_document_semantics(document, vocabulary)
    return document, _validate_document_body(document, body, body_start_line)


def validate_canonical_text(text: str) -> tuple[dict, ParsedMarkdown]:
    """Valida um documento canônico em memória antes de efeitos externos."""

    document_schema, _manifest_schema, vocabulary = _load_contract_assets()
    document, body, body_start_line = _load_canonical_text(text)
    _validate_schema(document, document_schema)
    _validate_document_semantics(document, vocabulary)
    return document, _validate_document_body(document, body, body_start_line)


def _validate_manifest_semantics(manifest: dict) -> None:
    batch_ids = [batch["batch_id"] for batch in manifest["batches"]]
    _require_unique(batch_ids, "batch_id")
    paths = [entry["path"] for entry in manifest["entries"]]
    _require_unique(paths, "path")
    document_ids = [entry["document_id"] for entry in manifest["entries"] if entry["document_id"]]
    _require_unique(document_ids, "document_id")
    known_batches = set(batch_ids)
    canonical_ids = {
        entry["document_id"] for entry in manifest["entries"] if entry["format"] == "canonical"
    }
    for entry in manifest["entries"]:
        if entry["batch_id"] not in known_batches:
            raise CanonicalDocumentError("entrada referencia batch_id inexistente", field="batch_id")
        for successor in entry["successors"]:
            if successor["document_id"] not in canonical_ids:
                raise CanonicalDocumentError("sucessor não resolve para documento canônico", field="successors")


def validate_manifest(manifest_path: Path, repo_root: Path) -> tuple[dict, dict[str, dict]]:
    document_schema, manifest_schema, vocabulary = _load_contract_assets()
    manifest = load_yaml(manifest_path)
    _validate_schema(manifest, manifest_schema)
    _validate_manifest_semantics(manifest)
    documents: dict[str, dict] = {}
    for entry in manifest["entries"]:
        path = (repo_root / entry["path"]).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise CanonicalDocumentError("path do manifesto escapa do repositório", field="path") from exc
        if not path.is_file():
            raise CanonicalDocumentError(f"arquivo inexistente: {entry['path']}", field="path")
        if entry["format"] != "canonical":
            continue
        document, body, body_start_line = load_canonical_document(path)
        _validate_schema(document, document_schema)
        _validate_document_semantics(document, vocabulary)
        _validate_document_body(document, body, body_start_line)
        if document["document_id"] != entry["document_id"]:
            raise CanonicalDocumentError("document_id diverge entre manifesto e front matter", field="document_id")
        documents[document["document_id"]] = document
    for entry in manifest["entries"]:
        for successor in entry["successors"]:
            missing = set(successor["section_keys"]) - set(documents[successor["document_id"]]["sections"])
            if missing:
                raise CanonicalDocumentError("sucessor referencia section_key inexistente", field="successors")
    return manifest, documents


def _legacy_diagnostics(path: Path, display_path: str, text: str) -> list[Diagnostic]:
    parsed = parse_markdown(text)
    diagnostics: list[Diagnostic] = []
    if parsed.unclosed_fence_line is not None:
        diagnostics.append(Diagnostic(display_path, "fence-unclosed", "warning", "code fence não fechado; legado mantido elegível", parsed.unclosed_fence_line))
    h1 = [heading for heading in parsed.headings if heading.level == 1]
    if not h1:
        diagnostics.append(Diagnostic(display_path, "title-missing", "warning", "documento legado sem H1"))
    elif len(h1) > 1:
        diagnostics.append(Diagnostic(display_path, "multiple-h1", "warning", f"documento legado possui {len(h1)} headings H1"))
    previous_level = 0
    for heading in parsed.headings:
        if previous_level and heading.level > previous_level + 1:
            diagnostics.append(Diagnostic(display_path, "heading-level-jump", "warning", f"salto de H{previous_level} para H{heading.level}", heading.line))
        previous_level = heading.level
    line_count = text.count("\n") + 1
    if len(text.encode("utf-8")) > 100_000 or line_count > 2_000 or len(h1) > 3:
        diagnostics.append(Diagnostic(display_path, "document-heterogeneous", "warning", f"documento grande/heterogêneo ({line_count} linhas, {len(h1)} H1)"))
    for match in IMAGE_RE.finditer(text):
        target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
        if re.match(r"^(?:https?:|data:|#)", target, re.IGNORECASE):
            continue
        if not (path.parent / target).resolve().is_file():
            line = text.count("\n", 0, match.start()) + 1
            diagnostics.append(Diagnostic(display_path, "visual-source-missing", "warning", f"fonte visual local ausente: {target}", line))
    return diagnostics


def lint_manifest(manifest_path: Path, repo_root: Path) -> tuple[list[Diagnostic], dict]:
    try:
        manifest, _documents = validate_manifest(manifest_path, repo_root)
    except CanonicalDocumentError as exc:
        relative = manifest_path.resolve().relative_to(repo_root.resolve()).as_posix()
        return [Diagnostic(relative, exc.rule, "error", str(exc), exc.line, exc.field)], {}

    diagnostics: list[Diagnostic] = []
    content_hashes: dict[str, str] = {}
    for entry in manifest["entries"]:
        display_path = entry["path"]
        path = repo_root / display_path
        if entry["format"] == "legacy":
            try:
                text = _decode_utf8(path)
            except CanonicalDocumentError as exc:
                diagnostics.append(Diagnostic(display_path, exc.rule, "error", str(exc), exc.line, exc.field))
                continue
            diagnostics.extend(_legacy_diagnostics(path, display_path, text))
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in content_hashes:
                diagnostics.append(Diagnostic(display_path, "probable-duplicate", "warning", f"conteúdo idêntico a {content_hashes[digest]}"))
            else:
                content_hashes[digest] = display_path

    docs_dir = repo_root / "documentos"
    immediate = {path.relative_to(repo_root).as_posix() for path in docs_dir.glob("*.md")}
    included = {
        entry["path"] for entry in manifest["entries"]
        if entry["ingestion"] == "include" and entry["state"] == "active"
    }
    if immediate != included:
        missing = sorted(immediate - included)
        extra = sorted(included - immediate)
        diagnostics.append(Diagnostic(
            manifest_path.relative_to(repo_root).as_posix(),
            "selection-mismatch",
            "error",
            f"seleção difere do corpus publicado; ausentes={missing}, extras={extra}",
            field="entries",
        ))
    return diagnostics, manifest


def build_inventory(manifest: dict, repo_root: Path) -> dict:
    entries = manifest.get("entries", [])
    backups = sorted(
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "documentos").rglob("*.md")
        if any(part.lower() in BACKUP_DIRECTORY_NAMES for part in path.relative_to(repo_root / "documentos").parts[:-1])
    )
    return {
        "manifest_entries": len(entries),
        "included": sum(entry["ingestion"] == "include" for entry in entries),
        "excluded": sum(entry["ingestion"] == "exclude" for entry in entries),
        "legacy": sum(entry["format"] == "legacy" for entry in entries),
        "canonical": sum(entry["format"] == "canonical" for entry in entries),
        "backup_files": backups,
        "business_rules_fallback": "bootstrap/00-DOCUMENTO-PRINCIPAL.md",
        "business_rules_in_corpus": any(entry["path"] == "bootstrap/00-DOCUMENTO-PRINCIPAL.md" for entry in entries),
        "entries": entries,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Linter e inventário offline dos documentos canônicos")
    parser.add_argument("command", choices=("lint", "inventory"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    manifest_path = args.manifest if args.manifest.is_absolute() else repo_root / args.manifest
    diagnostics, manifest = lint_manifest(manifest_path, repo_root)
    if args.command == "lint":
        if args.as_json:
            print(json.dumps([diagnostic.__dict__ for diagnostic in diagnostics], ensure_ascii=False, indent=2))
        else:
            for diagnostic in diagnostics:
                print(diagnostic.render())
            errors = sum(item.severity == "error" for item in diagnostics)
            warnings = sum(item.severity == "warning" for item in diagnostics)
            print(f"lint concluído: {errors} erro(s), {warnings} aviso(s)")
        return 1 if any(item.severity == "error" for item in diagnostics) else 0

    if not manifest:
        for diagnostic in diagnostics:
            print(diagnostic.render(), file=sys.stderr)
        return 1
    inventory = build_inventory(manifest, repo_root)
    if args.as_json:
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
    else:
        print(
            "inventário: "
            f"{inventory['manifest_entries']} fontes, {inventory['legacy']} legacy, "
            f"{inventory['canonical']} canonical, {len(inventory['backup_files'])} backup(s)"
        )
        for entry in inventory["entries"]:
            print(f"{entry['ingestion']:7} {entry['format']:9} {entry['path']}")
        print(f"fallback de regras (fora do corpus): {inventory['business_rules_fallback']}")
        for backup in inventory["backup_files"]:
            print(f"backup excluído: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
