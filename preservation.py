"""Gate offline e determinístico para preservação de migrações documentais.

O relatório ancora cada unidade em um commit Git local e em hashes SHA-256. A
comparação é conservadora: reescritas de evidências críticas exigem revisão do
relatório, em vez de serem consideradas equivalentes por heurística.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from canonical_docs import CanonicalDocumentError, validate_canonical_text


REPORT_VERSION = "1.0.0"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^[ \t]{0,3}(?:[-+*]|\d+[.)])[ \t]+(.+)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)|(?<![!\w])https?://[^\s>)]+")
_IMAGE = re.compile(r"!\[[^]]*\]\(([^)]+)\)")
_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b|(?<!\w)--[a-z][a-z0-9-]*|`([A-Za-z_][A-Za-z0-9_.]*)`")
_NEGATION = re.compile(r"\b(?:não|nunca|jamais|sem|proibido|proibida|not|never|without)\b", re.I)
_CONDITION = re.compile(r"\b(?:se|quando|caso|somente se|apenas se|if|when|unless)\b", re.I)
_EXCEPTION = re.compile(r"\b(?:exceto|exceção|salvo|ressalva|except|exception)\b", re.I)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("caminho deve ser relativo POSIX")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or ":" in value:
        raise ValueError("caminho fora da raiz do relatório")
    return value


def _snapshot_payload(report: dict) -> dict:
    """Material de hash estável, independente da ordem dos mapeamentos."""

    snapshot = report["original_snapshot"]
    unit_ids = report["source_unit_ids"]
    units = report["units"]
    if not isinstance(unit_ids, list) or not isinstance(units, list):
        raise ValueError("source_unit_ids e units devem ser listas")
    sources = [
        {"unit_id": unit["unit_id"], **unit["source"]}
        for unit in units
        if isinstance(unit, dict) and isinstance(unit.get("source"), dict)
    ]
    return {
        "commit": snapshot["commit"],
        "source_unit_ids": sorted(unit_ids),
        "sources": sorted(sources, key=lambda item: item["unit_id"]),
    }


def compute_snapshot_sha256(report: dict) -> str:
    """SHA-256 do JSON UTF-8 com chaves ordenadas e separadores compactos.

    O objeto inclui commit, IDs esperados e metadados de origem de cada unidade.
    Ele não inclui o próprio hash, destino ou decisões editoriais.
    """

    payload = json.dumps(
        _snapshot_payload(report), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _sha256(payload)


def _git_source(root: Path, commit: str, path: str) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{path}"],
        capture_output=True,
        check=False,
        timeout=15,
    )
    if process.returncode:
        raise ValueError("fonte indisponível no commit original")
    return process.stdout


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("fonte não é UTF-8") from exc


def _line_slice(text: str, start: object, end: object) -> str:
    lines = text.splitlines()
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise ValueError("intervalo de linhas inválido")
    if start < 1 or end < start or end > len(lines):
        raise ValueError("intervalo de linhas fora do arquivo")
    return "\n".join(lines[start - 1 : end])


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _code_blocks(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    marker = ""
    length = 0
    content: list[str] = []
    for line in lines:
        if marker:
            if re.fullmatch(rf"[ \t]{{0,3}}{re.escape(marker)}{{{length},}}[ \t]*", line):
                blocks.append("\n".join(content))
                marker = ""
                content = []
            else:
                content.append(line)
        else:
            match = _FENCE.match(line)
            if match:
                marker = match.group(1)[0]
                length = len(match.group(1))
    if marker:
        blocks.append("\n".join(content))
    return blocks


def _table_rows(text: str) -> list[str]:
    rows: list[str] = []
    for line in text.splitlines():
        if "|" not in line or _TABLE_SEPARATOR.fullmatch(line):
            continue
        pieces = re.split(r"(?<!\\)\|", line.strip().strip("|"))
        if len(pieces) > 1:
            rows.append(" | ".join(_normalized(piece.replace(r"\|", "|")) for piece in pieces))
    return rows


def _list_items(text: str) -> list[str]:
    return [_normalized(match.group(1)) for line in text.splitlines() if (match := _LIST.match(line))]


def _critical_clauses(text: str, pattern: re.Pattern[str]) -> list[str]:
    return [
        _normalized(line)
        for line in text.splitlines()
        if pattern.search(line) and line.strip() and not _FENCE.match(line)
    ]


def _missing(source: list[str], target: list[str]) -> list[str]:
    remaining = Counter(target)
    lost: list[str] = []
    for item in source:
        if remaining[item]:
            remaining[item] -= 1
        else:
            lost.append(item)
    return lost


def _body_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        heading = re.match(r"^[ \t]{0,3}#{1,6}[ \t]+(.+)", line)
        if heading:
            title = re.sub(r"[ \t]+\{#[a-z][a-z0-9-]*\}[ \t]*$", "", heading.group(1))
            lines.append(f"heading: {title.strip()}")
        else:
            lines.append(line.rstrip())
    return lines


def _missing_in_order(source: list[str], target: list[str]) -> list[str]:
    remaining = iter(target)
    lost = []
    for line in source:
        if not any(candidate == line for candidate in remaining):
            lost.append(line)
    return lost


def _compare_evidence(source: str, target: str, unit_id: str) -> list[dict]:
    losses: list[dict] = []
    for line in _missing_in_order(_body_lines(source), _body_lines(target)):
        losses.append({"unit_id": unit_id, "category": "text", "detail": line[:240]})
    checks = {
        "code": (_code_blocks(source), _code_blocks(target)),
        "identifier": (
            [match.group(1) or match.group(0) for match in _IDENTIFIER.finditer(source)],
            [match.group(1) or match.group(0) for match in _IDENTIFIER.finditer(target)],
        ),
        "link": ([match.group(1) or match.group(0) for match in _LINK.finditer(source)],
                 [match.group(1) or match.group(0) for match in _LINK.finditer(target)]),
        "image": (_IMAGE.findall(source), _IMAGE.findall(target)),
        "list": (_list_items(source), _list_items(target)),
        "table_cell": (_table_rows(source), _table_rows(target)),
        "negation": (_critical_clauses(source, _NEGATION), _critical_clauses(target, _NEGATION)),
        "condition": (_critical_clauses(source, _CONDITION), _critical_clauses(target, _CONDITION)),
        "exception": (_critical_clauses(source, _EXCEPTION), _critical_clauses(target, _EXCEPTION)),
    }
    for category, (original, migrated) in checks.items():
        for value in _missing(original, migrated):
            losses.append({"unit_id": unit_id, "category": category, "detail": value[:240]})
    return losses


def _local_image_paths(text: str, containing_path: str) -> list[str]:
    paths = []
    for reference in _IMAGE.findall(text):
        address = reference.strip().split()[0].strip("<>")
        parsed = urlsplit(address)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        image_path = posixpath.normpath(posixpath.join(posixpath.dirname(containing_path), unquote(parsed.path)))
        paths.append(_relative_path(image_path))
    return paths


def _source_image_hashes(root: Path, commit: str, source_path: str, excerpt: str) -> list[str]:
    hashes = []
    for image_path in _local_image_paths(excerpt, source_path):
        try:
            hashes.append(_sha256(_git_source(root, commit, image_path)))
        except ValueError as exc:
            raise ValueError(f"imagem original indisponível no commit: {image_path}") from exc
    return hashes


def _missing_target_images(
    root: Path, target_path: str, excerpt: str, unit_id: str, original_hashes: list[str]
) -> list[dict]:
    losses = []
    for index, image_path in enumerate(_local_image_paths(excerpt, target_path)):
        resolved = (root / image_path).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            losses.append({"unit_id": unit_id, "category": "image", "detail": f"imagem de destino ausente: {image_path}"})
        elif index < len(original_hashes) and _sha256(resolved.read_bytes()) != original_hashes[index]:
            losses.append({"unit_id": unit_id, "category": "image", "detail": f"imagem de destino alterada: {image_path}"})
    return losses


def _reviewed(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("status") == "reviewed"
        and isinstance(value.get("reviewer"), str)
        and bool(value["reviewer"].strip())
        and _reviewed_at(value.get("reviewed_at"))
    )


def _reviewed_at(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _check_destination(destination: dict, root: Path) -> tuple[str, dict]:
    path = _relative_path(destination["path"])
    expected_hash = destination["sha256"]
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        raise ValueError("SHA-256 do destino inválido")
    file_path = root / path
    if not file_path.is_file() or not file_path.resolve().is_relative_to(root.resolve()):
        raise ValueError("destino canônico ausente ou fora da raiz")
    raw = file_path.read_bytes()
    if _sha256(raw) != expected_hash:
        raise ValueError("SHA-256 do destino diverge")
    text = _text(raw)
    try:
        document, parsed = validate_canonical_text(text)
    except CanonicalDocumentError as exc:
        raise ValueError(f"destino canônico inválido: {exc}") from exc
    body_start = text.splitlines().index("---", 1) + 2
    if document["document_id"] != destination.get("canonical_id"):
        raise ValueError("canonical_id do destino diverge")
    section_key = destination.get("section_key")
    if section_key not in document["sections"]:
        raise ValueError("section_key do destino inexistente")
    headings = [heading for heading in parsed.headings if heading.section_key]
    selected = next(heading for heading in headings if heading.section_key == section_key)
    following = next((h for h in headings if h.line > selected.line), None)
    locator = destination.get("locator")
    if not isinstance(locator, dict) or locator.get("kind") != "line_range":
        raise ValueError("destino exige locator line_range")
    start, end = locator.get("start"), locator.get("end")
    section_start = body_start + selected.line - 1
    section_end = body_start + following.line - 2 if following else len(text.splitlines())
    if not isinstance(start, int) or not isinstance(end, int) or start < section_start or end > section_end:
        raise ValueError("locator fora da seção canônica")
    excerpt = _line_slice(text, start, end)
    return excerpt, document


def verify_migration_report(report: dict, root: Path) -> dict:
    """Verifica um lote sem rede, banco, embeddings ou geração.

    `passed` exige inventário completo, fontes Git e destinos íntegros, revisão
    humana e nenhuma perda crítica. Evidência indisponível produz `incomplete`;
    divergência comprovada ou perda crítica produz `failed`.
    """

    snapshot_data = report.get("original_snapshot") if isinstance(report, dict) else None
    snapshot_data = snapshot_data if isinstance(snapshot_data, dict) else {}
    result = {
        "batch_id": report.get("batch_id") if isinstance(report, dict) else None,
        "original_snapshot": {
            "commit": snapshot_data.get("commit"),
            "sha256": snapshot_data.get("sha256"),
        },
        "status": "incomplete",
        "coverage": {"mapped": 0, "total": 0},
        "critical_losses": [],
        "unit_results": [],
        "review_status": "pending",
    }
    if not isinstance(report, dict) or report.get("schema_version") != REPORT_VERSION:
        result["critical_losses"].append({"unit_id": None, "category": "report", "detail": "versão do relatório inválida"})
        result["status"] = "failed"
        return result
    try:
        snapshot = report["original_snapshot"]
        commit = snapshot["commit"]
        expected_snapshot_hash = snapshot["sha256"]
        expected = report["source_unit_ids"]
        units = report["units"]
        if not isinstance(report.get("batch_id"), str) or not report["batch_id"].strip():
            raise ValueError("batch_id obrigatório")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise ValueError("commit original inválido")
        if not isinstance(expected_snapshot_hash, str) or not _SHA256.fullmatch(expected_snapshot_hash):
            raise ValueError("SHA-256 do snapshot inválido")
        if not isinstance(expected, list) or not expected or any(not isinstance(i, str) or not i for i in expected):
            raise ValueError("source_unit_ids inválido")
        if len(expected) != len(set(expected)):
            raise ValueError("unit_id duplicado no inventário")
        if not isinstance(units, list) or any(not isinstance(u, dict) for u in units):
            raise ValueError("units inválido")
        ids = [u.get("unit_id") for u in units]
        if len(ids) != len(set(ids)) or any(i not in expected for i in ids):
            raise ValueError("unit_id duplicado ou fora do inventário")
        result["coverage"]["total"] = len(expected)
        snapshot_valid = compute_snapshot_sha256(report) == expected_snapshot_hash
    except (KeyError, TypeError, ValueError) as exc:
        result["critical_losses"].append({"unit_id": None, "category": "report", "detail": str(exc)})
        result["status"] = "failed"
        return result

    if not snapshot_valid:
        result["critical_losses"].append({"unit_id": None, "category": "snapshot", "detail": "SHA-256 do inventário diverge"})

    mapped = 0
    pending_review = False
    any_incomplete = False
    units_by_id = {unit["unit_id"]: unit for unit in units}
    for unit_id in expected:
        unit = units_by_id.get(unit_id)
        unit_result = {"unit_id": unit_id, "status": "incomplete", "destination": None, "disposition": None}
        result["unit_results"].append(unit_result)
        if unit is None:
            any_incomplete = True
            pending_review = True
            continue
        destination = unit.get("destination")
        disposition = unit.get("disposition")
        unit_result["destination"] = destination
        unit_result["disposition"] = disposition
        if destination is not None or (isinstance(disposition, dict) and disposition.get("kind") == "excluded"):
            mapped += 1
        review = unit.get("review")
        unit_pending_review = not _reviewed(review)
        if unit_pending_review:
            pending_review = True
        losses = []
        try:
            source = unit["source"]
            if not isinstance(source, dict):
                raise ValueError("fonte inválida")
            source_path = _relative_path(source["path"])
            source_hash = source["sha256"]
            if not isinstance(source_hash, str) or not _SHA256.fullmatch(source_hash):
                raise ValueError("SHA-256 da fonte inválido")
            if source.get("format") not in {"legacy", "canonical"}:
                raise ValueError("formato da fonte inválido")
            raw = _git_source(root, commit, source_path)
            if _sha256(raw) != source_hash:
                raise ValueError("SHA-256 da fonte diverge do commit")
            source_text = _text(raw)
            source_revision = source.get("revision")
            if source["format"] == "canonical":
                source_doc, _ = validate_canonical_text(source_text)
                if source_revision != source_doc["revision"]:
                    raise ValueError("revisão da fonte canônica diverge")
            elif source_revision is not None:
                raise ValueError("revisão de fonte legada deve ser null")
            excerpt = _line_slice(source_text, source.get("start_line"), source.get("end_line"))
            if not excerpt.strip():
                raise ValueError("unidade de origem vazia")
            original_image_hashes = _source_image_hashes(root, commit, source_path, excerpt)
            if disposition is not None:
                if not isinstance(disposition, dict) or disposition.get("kind") not in {"excluded", "merged"}:
                    raise ValueError("disposition inválida")
                if not all(isinstance(disposition.get(key), str) and disposition[key].strip()
                           for key in ("reason", "reviewer")) or not _reviewed_at(disposition.get("reviewed_at")):
                    raise ValueError("exclusão/fusão exige justificativa e revisão")
                if disposition["kind"] == "excluded" and destination is not None:
                    raise ValueError("exclusão não pode ter destino")
                if disposition["kind"] == "merged" and destination is None:
                    raise ValueError("fusão exige destino canônico")
            elif destination is None:
                raise ValueError("unidade sem destino ou exclusão")
            if disposition is None or disposition["kind"] == "merged":
                if not isinstance(destination, dict):
                    raise ValueError("destino inválido")
                target_excerpt, target_doc = _check_destination(destination, root)
                if target_doc["review"]["status"] != "reviewed":
                    unit_pending_review = True
                    pending_review = True
                losses = _compare_evidence(excerpt, target_excerpt, unit_id)
                losses.extend(
                    _missing_target_images(root, destination["path"], target_excerpt, unit_id, original_image_hashes)
                )
        except (CanonicalDocumentError, KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            message = str(exc)
            if "indisponível" in message or "sem destino" in message:
                any_incomplete = True
                unit_result["detail"] = message
            else:
                losses = [{"unit_id": unit_id, "category": "integrity", "detail": message}]
        if losses:
            result["critical_losses"].extend(losses)
            unit_result["status"] = "failed"
        elif "detail" not in unit_result and not unit_pending_review:
            unit_result["status"] = "passed"
        elif "detail" not in unit_result:
            unit_result["detail"] = "revisão humana pendente"
            any_incomplete = True
    result["coverage"]["mapped"] = mapped
    result["review_status"] = "pending" if pending_review else "reviewed"
    if not snapshot_valid:
        for unit_result in result["unit_results"]:
            unit_result["status"] = "failed"
            unit_result["detail"] = "SHA-256 do inventário diverge"
    if result["critical_losses"]:
        result["status"] = "failed"
    elif mapped == len(expected) and not any_incomplete and not pending_review:
        result["status"] = "passed"
    return result
