"""Promocao explicita e reversivel de um lote documental canonico.

Preview e validacao do plano sao offline. Apply e rollback exigem banco,
manifesto gravavel e invocacao operacional explicita.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import db
import ingest
from canonical_docs import DEFAULT_MANIFEST, validate_manifest
from preservation import _git_source, compute_snapshot_sha256


logger = logging.getLogger(__name__)
_LOCK_TABLES = (
    "LOCK TABLE public.documents, public.document_sections, "
    "public.document_chunks IN SHARE ROW EXCLUSIVE MODE"
)


class PublicationPending(RuntimeError):
    """O banco confirmou a troca, mas a publicacao do YAML requer reconciliacao."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _included(entry: dict) -> bool:
    return entry["state"] == "active" and entry["ingestion"] == "include"


def _document_filename(path: str) -> str:
    parts = Path(path).parts
    return Path(*parts[1:]).as_posix() if parts and parts[0] == "documentos" else Path(path).name


def _within_root(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("caminho fora do checkout")
    return resolved


def preview_plan(
    *, root: Path, before_manifest: Path, after_manifest: Path,
    source_report: dict, expected_corpus_sha256: str,
    expected_candidate_corpus_sha256: str,
) -> dict:
    """Deriva mapa e hashes sem banco, providers ou mutacao de arquivo."""
    root = root.resolve()
    before_manifest = _within_root(root, before_manifest)
    after_manifest = _within_root(root, after_manifest)
    if before_manifest != (root / "contracts/canonical-docs/manifest.yaml").resolve():
        raise ValueError("manifesto predecessor deve ser o manifesto publicado do repositorio")
    if before_manifest == after_manifest:
        raise ValueError("manifesto candidato deve ser um arquivo de staging distinto")
    before, before_docs = validate_manifest(before_manifest, root)
    after, after_docs = validate_manifest(after_manifest, root)
    batch_id = source_report.get("batch_id")
    snapshot = source_report.get("original_snapshot") or {}
    if not batch_id or batch_id not in {batch["batch_id"] for batch in after["batches"]}:
        raise ValueError("batch_id do relatorio ausente no manifesto candidato")
    if not snapshot.get("sha256") or compute_snapshot_sha256(source_report) != snapshot["sha256"]:
        raise ValueError("snapshot original do relatorio invalido")
    for digest in (expected_corpus_sha256, expected_candidate_corpus_sha256):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("hash de corpus invalido")
    if expected_corpus_sha256 == expected_candidate_corpus_sha256:
        raise ValueError("identidades anterior e candidata do corpus sao iguais")

    old_active = {entry["path"]: entry for entry in before["entries"] if _included(entry)}
    new_active = {entry["path"]: entry for entry in after["entries"] if _included(entry)}
    for path in old_active.keys() & new_active.keys():
        if old_active[path]["document_id"] != new_active[path]["document_id"]:
            raise ValueError("reutilizacao de path ativo exige staging; use caminho novo para o sucessor")
    retired = {path: entry for path, entry in old_active.items() if path not in new_active}
    introduced = {path: entry for path, entry in new_active.items() if path not in old_active}
    introduced_by_id = {entry["document_id"]: entry for entry in introduced.values() if entry["document_id"]}
    candidate_by_path = {entry["path"]: entry for entry in after["entries"]}
    if not retired or not introduced:
        raise ValueError("lote sem predecessores retirados ou sucessores introduzidos")
    predecessors = []
    archived_paths = []
    referenced: set[str] = set()
    for path, old in sorted(retired.items()):
        candidate_old = candidate_by_path.get(path)
        if candidate_old is not None:
            if candidate_old["batch_id"] != batch_id or _included(candidate_old) or not candidate_old["successors"]:
                raise ValueError("predecessor nao aposentado com sucessores no lote")
            successors = candidate_old["successors"]
        elif (old["format"] == "canonical" and old["document_id"] in introduced_by_id
              and introduced_by_id[old["document_id"]]["batch_id"] == batch_id
              and after_docs[old["document_id"]]["revision"] >= before_docs[old["document_id"]]["revision"]):
            # O schema v1 exige document_id unico: rename conserva identidade,
            # portanto o caminho antigo nao pode coexistir no candidato.
            if after_docs[old["document_id"]]["revision"] == before_docs[old["document_id"]]["revision"]:
                _, before_body = ingest._canonical_source((root / path).read_text(encoding="utf-8"))
                new_path = introduced_by_id[old["document_id"]]["path"]
                _, after_body = ingest._canonical_source((root / new_path).read_text(encoding="utf-8"))
                if ingest._canonical_semantic_hash(before_docs[old["document_id"]], before_body) != ingest._canonical_semantic_hash(after_docs[old["document_id"]], after_body):
                    raise ValueError("rename na mesma revisao possui conteudo divergente")
            successors = [{"document_id": old["document_id"],
                           "section_keys": sorted(after_docs[old["document_id"]]["sections"])}]
            archived_paths.append(path)
        else:
            raise ValueError("predecessor nao aposentado com sucessores no lote")
        for successor in successors:
            successor_key = successor["document_id"]
            if successor_key not in introduced_by_id or introduced_by_id[successor_key]["format"] != "canonical":
                raise ValueError("sucessor nao e documento canonico novo e elegivel")
            if introduced_by_id[successor_key]["batch_id"] != batch_id:
                raise ValueError("sucessor pertence a outro lote")
            referenced.add(successor_key)
        predecessors.append({
            "path": old["path"], "canonical_id": old["document_id"],
            "successors": successors,
        })
    if referenced != set(introduced_by_id) or len(introduced_by_id) != len(introduced):
        raise ValueError("sucessor introduzido sem predecessor no lote")
    predecessor_filenames = [_document_filename(item["path"]) for item in predecessors]
    if len(predecessor_filenames) != len(set(predecessor_filenames)):
        raise ValueError("predecessores compartilham filename")
    predecessor_by_path = {item["path"]: item for item in predecessors}
    successor_path_by_id = {entry["document_id"]: entry["path"] for entry in introduced.values()}
    source_paths_seen: set[str] = set()
    normalized_source_paths: set[str] = set()
    units = source_report.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("relatorio de unidades ausente")
    if ({unit.get("unit_id") for unit in units if isinstance(unit, dict)}
            != set(source_report.get("source_unit_ids") or [])
            or len(units) != len(source_report["source_unit_ids"])):
        raise ValueError("inventario de unidades incompleto ou duplicado")
    for unit in units:
        source_path = (unit.get("source") or {}).get("path")
        predecessor = predecessor_by_path.get(source_path)
        if predecessor is None:
            raise ValueError("unidade do relatorio fora dos predecessores")
        source_hash = (unit.get("source") or {}).get("sha256")
        current_source = _within_root(root, root / source_path).read_bytes()
        if source_hash != _sha(current_source):
            try:
                original_source = _git_source(root, snapshot["commit"], source_path)
            except ValueError as exc:
                raise ValueError("arquivo predecessor diverge do snapshot original") from exc
            normalize = lambda raw: raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if source_hash != _sha(original_source) or normalize(current_source) != normalize(original_source):
                raise ValueError("arquivo predecessor diverge do snapshot original")
            normalized_source_paths.add(source_path)
        source_paths_seen.add(source_path)
        destination = unit.get("destination")
        disposition = unit.get("disposition")
        if destination is None:
            if not isinstance(disposition, dict) or disposition.get("kind") != "excluded":
                raise ValueError("unidade sem destino ou exclusao revisada")
            continue
        matched = next((successor for successor in predecessor["successors"]
                        if successor["document_id"] == destination.get("canonical_id")
                        and destination.get("section_key") in successor["section_keys"]), None)
        if matched is None or destination.get("path") != successor_path_by_id[matched["document_id"]]:
            raise ValueError("destino da unidade diverge do mapa de sucessores")
        if destination.get("sha256") != _sha(_within_root(root, root / destination["path"]).read_bytes()):
            raise ValueError("arquivo sucessor diverge do destino revisado")
    if source_paths_seen != set(predecessor_by_path):
        raise ValueError("predecessor sem unidade inventariada")
    successors = []
    successor_filenames = [_document_filename(entry["path"]) for entry in introduced.values()]
    if len(successor_filenames) != len(set(successor_filenames)):
        raise ValueError("sucessores compartilham filename")
    for path, entry in sorted(introduced.items()):
        key = entry["document_id"]
        path = _within_root(root, root / entry["path"])
        successors.append({
            "path": entry["path"], "canonical_id": key,
            "revision": after_docs[key]["revision"], "sha256": _sha(path.read_bytes()),
        })
    plan = {
        "schema_version": 1,
        "batch_id": batch_id,
        "original_snapshot": {"commit": snapshot["commit"], "sha256": snapshot["sha256"]},
        "source_inventory": {
            "source_unit_ids": source_report["source_unit_ids"],
            "units": [{"unit_id": unit["unit_id"], "source": unit["source"],
                       "destination": unit.get("destination"), "disposition": unit.get("disposition")}
                      for unit in source_report["units"]],
        },
        "before_manifest": {"path": before_manifest.relative_to(root).as_posix(), "sha256": _sha(before_manifest.read_bytes())},
        "after_manifest": {"path": after_manifest.relative_to(root).as_posix(), "sha256": _sha(after_manifest.read_bytes())},
        "predecessors": predecessors,
        "normalized_source_paths": sorted(normalized_source_paths),
        "archived_paths": archived_paths,
        "successors": successors,
        "expected_corpus_sha256": expected_corpus_sha256,
        "expected_candidate_corpus_sha256": expected_candidate_corpus_sha256,
    }
    plan["fingerprint"] = _sha(_canonical_json(plan))
    return plan


def _verify_plan(plan: dict, root: Path) -> tuple[bytes, bytes]:
    fingerprint = plan.get("fingerprint")
    if not isinstance(fingerprint, str) or fingerprint != _sha(_canonical_json({k: v for k, v in plan.items() if k != "fingerprint"})):
        raise ValueError("fingerprint do plano invalido")
    if plan.get("schema_version") != 1 or not plan.get("predecessors") or not plan.get("successors"):
        raise ValueError("plano incompleto")
    before_path = _within_root(root, root / plan["before_manifest"]["path"])
    after_path = _within_root(root, root / plan["after_manifest"]["path"])
    before_bytes, after_bytes = before_path.read_bytes(), after_path.read_bytes()
    if _sha(before_bytes) != plan["before_manifest"]["sha256"] or _sha(after_bytes) != plan["after_manifest"]["sha256"]:
        raise ValueError("manifesto diverge do plano")
    for successor in plan["successors"]:
        if _sha(_within_root(root, root / successor["path"]).read_bytes()) != successor["sha256"]:
            raise ValueError("sucessor diverge do plano")
    source_report = {
        "batch_id": plan["batch_id"], "original_snapshot": plan["original_snapshot"],
        **plan["source_inventory"],
    }
    expected = preview_plan(
        root=root, before_manifest=before_path, after_manifest=after_path,
        source_report=source_report,
        expected_corpus_sha256=plan["expected_corpus_sha256"],
        expected_candidate_corpus_sha256=plan["expected_candidate_corpus_sha256"],
    )
    if expected != plan:
        raise ValueError("mapa do plano diverge dos manifestos")
    return before_bytes, after_bytes


def _check_gates(plan: dict, preservation: dict, gate: dict) -> None:
    snapshot = plan["original_snapshot"]
    inventory = plan.get("source_inventory") or {}
    reviewed = preservation.get("unit_results") or []
    mappings = {
        str(unit.get("unit_id")): _canonical_json({
            "destination": unit.get("destination"), "disposition": unit.get("disposition"),
        })
        for unit in inventory.get("units", []) if isinstance(unit, dict)
    }
    reviewed_mappings = {
        str(unit.get("unit_id")): _canonical_json({
            "destination": unit.get("destination"), "disposition": unit.get("disposition"),
        })
        for unit in reviewed if isinstance(unit, dict) and unit.get("status") == "passed"
    }
    if (preservation.get("status") != "passed" or gate.get("status") != "passed"
            or preservation.get("batch_id") != plan["batch_id"]
            or gate.get("batch_id") != plan["batch_id"]
            or preservation.get("original_snapshot") != snapshot
            or not mappings or len(mappings) != len(inventory.get("units", []))
            or len(reviewed_mappings) != len(reviewed) or mappings != reviewed_mappings
            or not any(item.get("id") == "source_snapshot_identity" and item.get("status") == "passed"
                       for item in gate.get("checks", []) if isinstance(item, dict))):
        raise ValueError("gates #69 ausentes, divergentes ou reprovados")


def _prepare_successor(root: Path, successor: dict) -> dict:
    """Prepara secoes e todos os vetores antes da transacao de promocao."""
    path = _within_root(root, root / successor["path"])
    text = path.read_text(encoding="utf-8")
    canonical, body = ingest._canonical_source(text)
    if canonical["document_id"] != successor["canonical_id"] or canonical["revision"] != successor["revision"]:
        raise ValueError("identidade/revisao do sucessor diverge")
    filename = _document_filename(successor["path"])
    title = canonical["title"]
    classification, sections = ingest._classify_text_sections(
        body, filename=filename, title=title, source=str(path), doc_type="md", canonical=canonical,
    )
    chunks = []
    for section in sections:
        for raw in ingest._get_splitter().split_text(section.content) or ([section.content.strip()] if section.content.strip() else []):
            retrieval = f"{section.semantic_context}\n\n{raw}" if section.semantic_context else raw
            chunks.append((len(chunks), raw, retrieval, section))
    if not chunks:
        raise ValueError("sucessor sem chunks")
    content_hash = ingest._content_hash(body)
    processing_hash = ingest._processing_hash(
        content_hash=content_hash, title=title, doc_type="md",
        module=classification.legacy_module, doc_priority=5,
        sections=sections, chunk_items=chunks, document_classification=classification,
        canonical_schema_version=canonical["schema_version"],
    )
    document_id = str(uuid4())
    section_rows = ingest._prepare_document_section_rows(
        document_id, title, sections, content_hash=content_hash,
        processing_hash=processing_hash, source_id=ingest._source_id(filename),
    )
    if len(section_rows) != len(sections):
        raise ValueError("preparacao de secoes incompleta")
    chunk_rows, failed = ingest._prepare_chunk_rows(
        doc_id=document_id, filename=filename, title=title, text=body,
        doc_type="md", source_type="file", module=classification.legacy_module,
        doc_priority=5, content_hash=content_hash, processing_hash=processing_hash,
        chunk_items=chunks, source_id=ingest._source_id(filename),
    )
    if failed or len(chunk_rows) != len(chunks):
        raise ValueError("preparacao de chunks incompleta")
    for row in [*section_rows, *chunk_rows]:
        row.setdefault("metadata", {})["document_classification"] = classification.metadata()
    ingest._project_canonical_rows(canonical, sections, section_rows, chunk_rows)
    document = {
        "id": document_id, "filename": filename, "title": title, "source": str(path),
        "doc_type": "md", "chunk_count": len(chunk_rows), "priority": 5,
        "content_hash": content_hash, "processing_hash": processing_hash,
        "canonical_id": canonical["document_id"], "schema_version": canonical["schema_version"],
        "document_revision": canonical["revision"],
        "metadata": {"semantic_hash_version": "canonical-semantic-v1",
                     "semantic_hash": ingest._canonical_semantic_hash(canonical, body),
                     "canonical": json.loads(json.dumps(canonical, ensure_ascii=False, default=str))},
    }
    return {"document": document, "sections": section_rows, "chunks": chunk_rows}


def _lock_corpus(connection) -> None:
    # Ingestao comum usa locks por documento antes do DML. Nao adquirir esses
    # advisory locks aqui: a ordem inversa criaria deadlock. O lock de tabela
    # precede qualquer leitura do snapshot e serializa todo DML do corpus.
    with connection.cursor() as cursor:
        cursor.execute(_LOCK_TABLES)


def _corpus_identity(connection) -> str:
    rows = db.db_call("get_evaluation_data_identity", {}, connection=connection)
    if len(rows) != 1 or not rows[0].get("corpus_sha256"):
        raise RuntimeError("identidade de corpus indisponivel")
    return rows[0]["corpus_sha256"]


def _json_ready(row: dict) -> dict:
    result = {}
    for key, value in row.items():
        if key in {"fts", "retrieval_fts"}:
            continue
        if isinstance(value, (UUID, date, datetime)):
            value = str(value)
        elif key == "embedding" and value is not None:
            value = str(value)
        result[key] = value
    return result


def _snapshot_ids(document_ids: list[str], connection) -> dict:
    documents = []
    sections = []
    chunks = []
    for doc_id in document_ids:
        rows = db.db_select("documents", filters={"id": f"eq.{doc_id}"}, connection=connection)
        if len(rows) != 1:
            raise ValueError("documento do snapshot ausente")
        documents.append(_json_ready(rows[0]))
        sections.extend(_json_ready(row) for row in db.db_select(
            "document_sections", filters={"document_id": f"eq.{doc_id}"},
            order_by="section_index.asc", connection=connection,
        ))
        chunks.extend(_json_ready(row) for row in db.db_select(
            "document_chunks", filters={"document_id": f"eq.{doc_id}"},
            order_by="chunk_index.asc", connection=connection,
        ))
    return {"documents": documents, "sections": sections, "chunks": chunks}


def _snapshot_predecessors(plan: dict, connection) -> dict:
    document_ids = []
    for predecessor in plan["predecessors"]:
        filename = _document_filename(predecessor["path"])
        rows = db.db_select("documents", filters={"filename": f"eq.{filename}"}, connection=connection)
        if len(rows) != 1 or str(rows[0].get("canonical_id") or "") != str(predecessor["canonical_id"] or ""):
            raise ValueError("predecessor indexado ausente ou divergente")
        document_ids.append(str(rows[0]["id"]))
    return _snapshot_ids(document_ids, connection)


def _publication(connection) -> dict | None:
    rows = db.db_select("canonical_publication_state", connection=connection)
    return rows[0] if rows else None


def _assert_successor_slots(plan: dict, connection) -> None:
    predecessor_names = {Path(row["path"]).name for row in plan["predecessors"]}
    predecessor_ids = {row["canonical_id"] for row in plan["predecessors"] if row["canonical_id"]}
    for successor in plan["successors"]:
        filename = _document_filename(successor["path"])
        by_filename = db.db_select("documents", columns="id", filters={"filename": f"eq.{filename}"}, connection=connection)
        by_id = db.db_select("documents", columns="id", filters={"canonical_id": f"eq.{successor['canonical_id']}"}, connection=connection)
        if (by_filename and filename not in predecessor_names) or (by_id and successor["canonical_id"] not in predecessor_ids):
            raise ValueError("identidade de sucessor ocupada fora do lote")


def _poststate_matches(plan: dict, ledger: dict, connection) -> bool:
    document_ids = ledger.get("after_document_ids") or []
    if len(document_ids) != len(plan["successors"]):
        return False
    for successor, document_id in zip(plan["successors"], document_ids):
        rows = db.db_select("documents", columns="id,filename,canonical_id,document_revision",
                            filters={"id": f"eq.{document_id}"}, connection=connection)
        if (len(rows) != 1 or rows[0]["filename"] != _document_filename(successor["path"])
                or str(rows[0]["canonical_id"]) != successor["canonical_id"]
                or rows[0]["document_revision"] != successor["revision"]):
            return False
    return _sha(_canonical_json(_snapshot_ids(document_ids, connection))) == ledger.get("after_rows_sha256")


def _successor_files_match(plan: dict, root: Path) -> bool:
    return all(_sha(_within_root(root, root / item["path"]).read_bytes()) == item["sha256"]
               for item in plan["successors"])


def _count(snapshot: dict) -> dict:
    return {name: len(snapshot[name]) for name in ("documents", "sections", "chunks")}


def _file_snapshots(root: Path, paths: list[str], batch_id: str, label: str) -> list[dict]:
    result = []
    for path in paths:
        raw = _within_root(root, root / path).read_bytes()
        archive_path = f"runtime/canonical-archive/{batch_id}/{label}-{_sha(path.encode('utf-8'))}.md"
        result.append({"path": path, "sha256": _sha(raw),
                       "content_b64": base64.b64encode(raw).decode("ascii"),
                       "archive_path": archive_path})
    return result


def _publish_manifest(path: Path, content: str) -> None:
    raw = content.encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=".canonical-publish-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _archive_predecessors(root: Path, archived: list[dict]) -> None:
    for item in archived:
        source = _within_root(root, root / item["path"])
        archive = _within_root(root, root / item["archive_path"])
        if source.exists():
            if _sha(source.read_bytes()) != item["sha256"]:
                raise ValueError("predecessor renomeado mudou antes do arquivamento")
            archive.parent.mkdir(parents=True, exist_ok=True)
            if archive.exists():
                raise ValueError("arquivo de backup ja ocupado")
            os.replace(source, archive)
            if _sha(archive.read_bytes()) != item["sha256"]:
                raise RuntimeError("backup do predecessor incompleto")
        elif archive.is_file():
            if _sha(archive.read_bytes()) != item["sha256"]:
                raise ValueError("arquivo arquivado diverge do snapshot")
        else:
            original = base64.b64decode(item["content_b64"])
            if _sha(original) != item["sha256"]:
                raise ValueError("snapshot de arquivo diverge")
            archive.parent.mkdir(parents=True, exist_ok=True)
            _publish_manifest(archive, original.decode("utf-8"))
            if _sha(archive.read_bytes()) != item["sha256"]:
                raise RuntimeError("backup reconstituido incompleto")


def _restore_predecessors(root: Path, archived: list[dict]) -> None:
    for item in archived:
        source = _within_root(root, root / item["path"])
        archive = _within_root(root, root / item["archive_path"])
        if source.exists():
            if _sha(source.read_bytes()) != item["sha256"]:
                raise ValueError("caminho do predecessor ocupado no rollback")
            continue
        if archive.is_file():
            if _sha(archive.read_bytes()) != item["sha256"]:
                raise ValueError("backup do predecessor diverge")
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(archive, source)
            if _sha(source.read_bytes()) != item["sha256"]:
                raise RuntimeError("restauracao do predecessor incompleta")
        else:
            original = base64.b64decode(item["content_b64"])
            if _sha(original) != item["sha256"]:
                raise ValueError("snapshot do predecessor diverge")
            _publish_manifest(source, original.decode("utf-8"))
            if _sha(source.read_bytes()) != item["sha256"]:
                raise RuntimeError("predecessor reconstituido incompleto")


def _finalize_publication(plan: dict, root: Path, ledger: dict, *, rollback: bool) -> dict:
    expected_status = "restoring_manifest" if rollback else "applying_manifest"
    target_hash = ledger["before_manifest_sha256"] if rollback else ledger["after_manifest_sha256"]
    target_text = ledger["before_manifest_text"] if rollback else ledger["after_manifest_text"]
    manifest_path = _within_root(root, root / plan["before_manifest"]["path"])
    with db.db_transaction() as connection:
        _lock_corpus(connection)
        current = db.db_select("canonical_migration_batches", filters={"batch_id": f"eq.{plan['batch_id']}"}, connection=connection)[0]
        publication = _publication(connection)
        if current["status"] != expected_status or not publication or publication["batch_id"] != plan["batch_id"]:
            raise ValueError("publicacao pendente foi alterada concorrentemente")
        expected_corpus = ledger["before_corpus_sha256"] if rollback else ledger["after_corpus_sha256"]
        if _corpus_identity(connection) != expected_corpus:
            raise ValueError("corpus mudou antes de publicar manifesto")
        if not rollback and not _poststate_matches(plan, current, connection):
            raise ValueError("sucessores indexados mudaram antes da publicacao")
        if not rollback and not _successor_files_match(plan, root):
            raise ValueError("arquivos sucessores mudaram antes da publicacao")
        current_manifest_hash = _sha(manifest_path.read_bytes())
        if current_manifest_hash not in {ledger["before_manifest_sha256"], ledger["after_manifest_sha256"]}:
            raise ValueError("manifesto alterado fora da promocao")
        if rollback:
            _archive_predecessors(root, ledger["successor_files"])
            _restore_predecessors(root, ledger["archived_predecessors"])
        else:
            _archive_predecessors(root, ledger["archived_predecessors"])
        if current_manifest_hash != target_hash:
            _publish_manifest(manifest_path, target_text)
        if _sha(manifest_path.read_bytes()) != target_hash:
            raise RuntimeError("publicacao do manifesto incompleta")
        if rollback:
            previous = ledger["before_publication"]
            db.db_delete("canonical_publication_state", {"singleton": "eq.true"}, connection=connection)
            if previous:
                db.db_insert("canonical_publication_state", {
                    "singleton": True, "batch_id": previous["batch_id"],
                    "manifest_sha256": previous["manifest_sha256"], "status": previous["status"],
                }, connection=connection)
            db.db_update("canonical_migration_batches", {"status": "rolled_back", "rolled_back_at": datetime.now().astimezone()},
                         {"batch_id": f"eq.{plan['batch_id']}"}, connection=connection)
        else:
            db.db_update("canonical_publication_state", {"status": "applied"},
                         {"singleton": "eq.true"}, connection=connection)
            db.db_update("canonical_migration_batches", {"status": "applied"},
                         {"batch_id": f"eq.{plan['batch_id']}"}, connection=connection)
    return {"batch_id": plan["batch_id"], "fingerprint": plan["fingerprint"],
            "status": "rolled_back" if rollback else "applied", "corpus_sha256": expected_corpus}


def apply_plan(plan: dict, preservation: dict, gate: dict, root: Path) -> dict:
    root = root.resolve()
    _check_gates(plan, preservation, gate)
    if plan.get("fingerprint") != _sha(_canonical_json({k: v for k, v in plan.items() if k != "fingerprint"})):
        raise ValueError("fingerprint do plano invalido")
    existing = db.db_select("canonical_migration_batches", filters={"batch_id": f"eq.{plan['batch_id']}"})
    if existing:
        ledger = existing[0]
        if ledger["plan_fingerprint"] != plan["fingerprint"]:
            raise ValueError("batch_id reutilizado por plano diferente")
        if ledger["status"] == "applying_manifest":
            try:
                return _finalize_publication(plan, root, ledger, rollback=False)
            except Exception as exc:
                raise PublicationPending("publicacao do manifesto continua pendente") from exc
        if ledger["status"] != "applied":
            raise ValueError("lote nao pode ser reaplicado apos rollback")
        with db.db_transaction() as connection:
            _lock_corpus(connection)
            publication = _publication(connection)
            if (_corpus_identity(connection) != ledger["after_corpus_sha256"]
                    or not _poststate_matches(plan, ledger, connection)
                    or not publication or publication["status"] != "applied"
                    or publication["batch_id"] != plan["batch_id"]
                    or publication["manifest_sha256"] != ledger["after_manifest_sha256"]):
                raise ValueError("lote aplicado diverge do estado atual")
        if _sha(_within_root(root, root / plan["before_manifest"]["path"]).read_bytes()) != ledger["after_manifest_sha256"]:
            raise ValueError("manifesto publicado diverge do ledger")
        if not _successor_files_match(plan, root):
            raise ValueError("arquivos sucessores divergem do plano aplicado")
        return {"batch_id": plan["batch_id"], "fingerprint": plan["fingerprint"],
                "status": "already_applied", "corpus_sha256": ledger["after_corpus_sha256"]}

    before_bytes, after_bytes = _verify_plan(plan, root)
    prepared = [_prepare_successor(root, item) for item in plan["successors"]]
    with db.db_transaction() as connection:
        _lock_corpus(connection)
        if db.db_select("canonical_migration_batches", filters={"batch_id": f"eq.{plan['batch_id']}"}, connection=connection):
            raise ValueError("lote aplicado por outro escritor; repita para verificar idempotencia")
        before_hash = _corpus_identity(connection)
        if before_hash != plan["expected_corpus_sha256"]:
            raise ValueError("snapshot de corpus mudou antes da promocao")
        publication = _publication(connection)
        if publication and (publication["status"] != "applied" or publication["manifest_sha256"] != plan["before_manifest"]["sha256"]):
            raise ValueError("publicacao anterior pendente ou manifesto anterior divergente")
        if _sha(_within_root(root, root / plan["before_manifest"]["path"]).read_bytes()) != plan["before_manifest"]["sha256"]:
            raise ValueError("manifesto predecessor mudou durante preparacao")
        snapshot = _snapshot_predecessors(plan, connection)
        _assert_successor_slots(plan, connection)
        archived = _file_snapshots(root, plan["archived_paths"], plan["batch_id"], "predecessor")
        successor_files = _file_snapshots(root, [item["path"] for item in plan["successors"]],
                                          plan["batch_id"], "successor")
        for document in snapshot["documents"]:
            db.db_delete("documents", {"id": f"eq.{document['id']}"}, connection=connection)
        for item in prepared:
            db.db_insert("documents", item["document"], connection=connection)
            ingest._insert_rows_in_batches("document_sections", item["sections"], connection=connection)
            inserted = ingest._insert_chunk_rows(item["chunks"], connection=connection)
            if len(inserted) != len(item["chunks"]):
                raise RuntimeError("insercao de sucessor incompleta")
        after_hash = _corpus_identity(connection)
        if after_hash != plan["expected_candidate_corpus_sha256"]:
            raise ValueError("corpus candidato diverge da rodada #69")
        after_ids = [item["document"]["id"] for item in prepared]
        after_rows_sha256 = _sha(_canonical_json(_snapshot_ids(after_ids, connection)))
        aliases = {item["document"]["canonical_id"]: item["document"]["metadata"]["canonical"].get("aliases", [])
                   for item in prepared}
        counts = {"before": _count(snapshot), "after": {
            "documents": len(prepared), "sections": sum(len(item["sections"]) for item in prepared),
            "chunks": sum(len(item["chunks"]) for item in prepared),
        }}
        db.db_insert("canonical_migration_batches", {
            "batch_id": plan["batch_id"], "plan_fingerprint": plan["fingerprint"],
            "status": "applying_manifest", "original_snapshot_sha256": plan["original_snapshot"]["sha256"],
            "before_manifest_sha256": plan["before_manifest"]["sha256"],
            "after_manifest_sha256": plan["after_manifest"]["sha256"],
            "before_manifest_text": before_bytes.decode("utf-8"), "after_manifest_text": after_bytes.decode("utf-8"),
            "before_corpus_sha256": before_hash, "after_corpus_sha256": after_hash,
            "after_rows_sha256": after_rows_sha256,
            "before_rows": snapshot, "before_publication": publication,
            "after_document_ids": after_ids,
            "archived_predecessors": archived,
            "successor_files": successor_files,
            "aliases": aliases, "counts": counts,
        }, connection=connection)
        if publication:
            db.db_update("canonical_publication_state", {
                "batch_id": plan["batch_id"], "manifest_sha256": plan["after_manifest"]["sha256"],
                "status": "applying_manifest",
            }, {"singleton": "eq.true"}, connection=connection)
        else:
            db.db_insert("canonical_publication_state", {
                "singleton": True, "batch_id": plan["batch_id"],
                "manifest_sha256": plan["after_manifest"]["sha256"], "status": "applying_manifest",
            }, connection=connection)
    logger.info("PROMOTION_COMMITTED batch_hash=%s counts=%s", _sha(plan["batch_id"].encode())[:12], counts)
    try:
        return _finalize_publication(plan, root, {
            "before_manifest_sha256": plan["before_manifest"]["sha256"],
            "after_manifest_sha256": plan["after_manifest"]["sha256"],
            "before_manifest_text": before_bytes.decode("utf-8"),
            "after_manifest_text": after_bytes.decode("utf-8"),
            "before_corpus_sha256": before_hash, "after_corpus_sha256": after_hash,
            "archived_predecessors": archived,
            "successor_files": successor_files,
        }, rollback=False)
    except Exception as exc:
        raise PublicationPending("promocao confirmada no banco; publicacao do manifesto pendente") from exc


def rollback_plan(plan: dict, root: Path) -> dict:
    """Restaura snapshot completo sem chamar embeddings ou provedores."""
    root = root.resolve()
    if plan.get("fingerprint") != _sha(_canonical_json({k: v for k, v in plan.items() if k != "fingerprint"})):
        raise ValueError("fingerprint do plano invalido")
    rows = db.db_select("canonical_migration_batches", filters={"batch_id": f"eq.{plan['batch_id']}"})
    if len(rows) != 1 or rows[0]["plan_fingerprint"] != plan["fingerprint"]:
        raise ValueError("lote nao encontrado ou plano divergente")
    ledger = rows[0]
    manifest_path = _within_root(root, root / plan["before_manifest"]["path"])
    if ledger["status"] == "restoring_manifest":
        try:
            return _finalize_publication(plan, root, ledger, rollback=True)
        except Exception as exc:
            raise PublicationPending("publicacao do rollback continua pendente") from exc
    if ledger["status"] == "rolled_back":
        with db.db_transaction() as connection:
            _lock_corpus(connection)
            if _corpus_identity(connection) != ledger["before_corpus_sha256"]:
                raise ValueError("estado apos rollback diverge")
        if _sha(manifest_path.read_bytes()) != ledger["before_manifest_sha256"]:
            raise ValueError("manifesto anterior nao restaurado")
        return {"batch_id": plan["batch_id"], "fingerprint": plan["fingerprint"],
                "status": "already_rolled_back", "corpus_sha256": ledger["before_corpus_sha256"]}
    if ledger["status"] not in {"applied", "applying_manifest"}:
        raise ValueError("estado do lote nao permite rollback")
    if _sha(manifest_path.read_bytes()) not in {ledger["before_manifest_sha256"], ledger["after_manifest_sha256"]}:
        raise ValueError("manifesto publicado diverge antes do rollback")
    with db.db_transaction() as connection:
        _lock_corpus(connection)
        current = db.db_select("canonical_migration_batches", filters={"batch_id": f"eq.{plan['batch_id']}"}, connection=connection)[0]
        publication = _publication(connection)
        if (current["status"] not in {"applied", "applying_manifest"} or not publication
                or publication["batch_id"] != plan["batch_id"]
                or publication["status"] != current["status"]
                or publication["manifest_sha256"] != ledger["after_manifest_sha256"]
                or _corpus_identity(connection) != ledger["after_corpus_sha256"]
                or not _poststate_matches(plan, ledger, connection)):
            raise ValueError("snapshot pos-promocao mudou; rollback bloqueado")
        successor_ids = ledger["after_document_ids"]
        for document_id in successor_ids:
            rows = db.db_select("documents", columns="id", filters={"id": f"eq.{document_id}"}, connection=connection)
            if len(rows) != 1:
                raise ValueError("sucessor ausente no rollback")
            db.db_delete("documents", {"id": f"eq.{document_id}"}, connection=connection)
        old = ledger["before_rows"]
        ingest._insert_rows_in_batches("documents", old["documents"], connection=connection)
        ingest._insert_rows_in_batches("document_sections", old["sections"], connection=connection)
        ingest._insert_rows_in_batches("document_chunks", old["chunks"], connection=connection)
        if _corpus_identity(connection) != ledger["before_corpus_sha256"]:
            raise RuntimeError("snapshot restaurado diverge do original")
        db.db_update("canonical_migration_batches", {"status": "restoring_manifest"},
                     {"batch_id": f"eq.{plan['batch_id']}"}, connection=connection)
        db.db_update("canonical_publication_state", {
            "status": "restoring_manifest", "manifest_sha256": ledger["before_manifest_sha256"],
        }, {"singleton": "eq.true"}, connection=connection)
    logger.info("PROMOTION_ROLLBACK_COMMITTED batch_hash=%s counts=%s",
                _sha(plan["batch_id"].encode())[:12], ledger["counts"]["before"])
    try:
        return _finalize_publication(plan, root, ledger, rollback=True)
    except Exception as exc:
        raise PublicationPending("rollback confirmado no banco; publicacao do manifesto pendente") from exc


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON deve ser um objeto")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="gerar plano offline")
    preview.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    preview.add_argument("--before-manifest", type=Path, default=DEFAULT_MANIFEST)
    preview.add_argument("--after-manifest", type=Path, required=True)
    preview.add_argument("--report", type=Path, required=True)
    preview.add_argument("--expected-corpus-sha256", required=True)
    preview.add_argument("--expected-candidate-corpus-sha256", required=True)
    preview.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("apply", help="promover lote explicitamente")
    apply.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--preservation", type=Path, required=True)
    apply.add_argument("--gate", type=Path, required=True)
    rollback = commands.add_parser("rollback", help="restaurar snapshot anterior")
    rollback.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    rollback.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            result = preview_plan(
                root=args.root, before_manifest=args.before_manifest,
                after_manifest=args.after_manifest, source_report=_read_json(args.report),
                expected_corpus_sha256=args.expected_corpus_sha256,
                expected_candidate_corpus_sha256=args.expected_candidate_corpus_sha256,
            )
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif args.command == "apply":
            result = apply_plan(_read_json(args.plan), _read_json(args.preservation),
                                _read_json(args.gate), args.root)
        else:
            result = rollback_plan(_read_json(args.plan), args.root)
    except PublicationPending as exc:
        print(json.dumps({"status": "publication_pending", "error_type": type(exc.__cause__).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
