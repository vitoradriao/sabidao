"""Compara classificações locais sem acessar banco, rede ou modelos."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ingest  # noqa: E402
from canonical_docs import DEFAULT_MANIFEST, load_yaml  # noqa: E402
from taxonomy import RULE_VERSION  # noqa: E402


def compare(path: Path, root: Path | None = None) -> dict:
    text = path.read_text(encoding="utf-8")
    title = path.stem
    filename = ingest._path_to_document_filename(path, root)
    source = str(path.resolve())
    before_module = ingest._infer_module(filename, title, source, "md")
    before_sections = ingest._split_markdown_sections(
        text, doc_title=title, base_module=before_module, doc_type="md"
    )
    document, after_sections = ingest._classify_text_sections(
        text, filename=filename, title=title, source=source, doc_type="md"
    )
    return {
        "path": filename,
        "before": {
            "module": before_module,
            "sections": [
                {"section_index": section.section_index, "module": section.module,
                 "answer_mode": section.answer_mode}
                for section in before_sections
            ],
        },
        "after": {
            "module": document.legacy_module,
            "classification": document.metadata(),
            "sections": [
                {"section_index": section.section_index, "module": section.module,
                 "answer_mode": section.answer_mode,
                 "classification": section.classification.metadata()}
                for section in after_sections
            ],
        },
    }


def eligible_paths(root: Path) -> list[Path]:
    root = root.resolve()
    if root == (ROOT / "documentos").resolve():
        manifest = load_yaml(DEFAULT_MANIFEST)
        return sorted(
            ROOT / entry["path"]
            for entry in manifest["entries"]
            if entry["ingestion"] == "include"
            and entry["state"] == "active"
            and Path(entry["path"]).suffix.lower() == ".md"
            and (ROOT / entry["path"]).is_file()
        )
    _root, files = ingest._collect_local_files(str(root), recursive=True)
    return sorted(path for path in files if path.suffix.lower() == ".md")


def _counts(values: list[str]) -> dict[str, int]:
    return {value: values.count(value) for value in sorted(set(values))}


def _section_counts(sections: list[dict]) -> dict:
    return {
        "modules": _counts([section["module"] for section in sections]),
        "answer_modes": _counts([section["answer_mode"] for section in sections]),
    }


def _document_summary(item: dict) -> dict:
    before = item["before"]
    after = item["after"]
    module_changes = sum(
        old["module"] != new["module"]
        for old, new in zip(before["sections"], after["sections"])
    )
    mode_changes = sum(
        old["answer_mode"] != new["answer_mode"]
        for old, new in zip(before["sections"], after["sections"])
    )
    return {
        "path": item["path"],
        "changed": before["module"] != after["module"] or bool(module_changes or mode_changes),
        "changed_module_sections_count": module_changes,
        "changed_answer_mode_sections_count": mode_changes,
        "before_module": before["module"],
        "after_module": after["module"],
        "classification": after["classification"],
        "before_sections": _section_counts(before["sections"]),
        "after_sections": _section_counts(after["sections"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Diretório local de Markdown")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--output", type=Path, help="Arquivo JSON opcional")
    args = parser.parse_args(argv)
    if args.sample_size < 1:
        parser.error("--sample-size deve ser positivo")
    if not args.root.is_dir():
        parser.error("--root deve ser um diretório existente")
    paths = eligible_paths(args.root)
    compared = [compare(path, args.root) for path in paths]
    documents = [_document_summary(item) for item in compared]
    section_changes = [
        {"path": item["path"], "section_index": before["section_index"],
         "before": {"module": before["module"], "answer_mode": before["answer_mode"]},
         "after": {"module": after["module"], "answer_mode": after["answer_mode"],
                   "classification": after["classification"]}}
        for item in compared
        for before, after in zip(item["before"]["sections"], item["after"]["sections"])
        if before["module"] != after["module"] or before["answer_mode"] != after["answer_mode"]
    ]
    report = {
        "taxonomy_version": RULE_VERSION,
        "documents_count": len(compared),
        "changed_documents_count": sum(item["changed"] for item in documents),
        "changed_sections_count": len(section_changes),
        "changed_module_sections_count": sum(item["changed_module_sections_count"] for item in documents),
        "changed_answer_mode_sections_count": sum(item["changed_answer_mode_sections_count"] for item in documents),
        "before_modules": _counts([item["before"]["module"] for item in compared]),
        "after_modules": _counts([item["after"]["module"] for item in compared]),
        "before_sections": _section_counts([
            section for item in compared for section in item["before"]["sections"]
        ]),
        "after_sections": _section_counts([
            section for item in compared for section in item["after"]["sections"]
        ]),
        "sample_size": min(args.sample_size, len(section_changes)),
        "database_writes": 0,
        "rollback": "Descartar este relatório; nenhuma classificação persistida foi alterada.",
        "documents": documents,
        "section_changes_sample": section_changes[: args.sample_size],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
