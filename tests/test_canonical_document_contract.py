import copy
import codecs
import json
import re
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts" / "canonical-docs" / "v1"
VALID_FIXTURES = CONTRACT_DIR / "fixtures" / "valid"
INVALID_FIXTURES = CONTRACT_DIR / "fixtures" / "invalid"
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SECTION_HEADING_RE = re.compile(
    r"^(#{2,6})\s+(.+?)\s+\{#([a-z][a-z0-9]*(?:-[a-z0-9]+)*)\}\s*$"
)
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


class ContractError(ValueError):
    pass


class UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ContractError(f"chave YAML duplicada: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_contract_yaml_text(text):
    try:
        for event in yaml.parse(text, Loader=UniqueKeySafeLoader):
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
                raise ContractError("anchors e aliases YAML não são permitidos")
        value = yaml.load(text, Loader=UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ContractError(f"YAML inválido ou inseguro: {exc}") from exc

    if not isinstance(value, dict):
        raise ContractError("a raiz YAML deve ser um objeto")
    return value


def load_contract_yaml(path: Path):
    raw = path.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        raise ContractError("BOM UTF-8 não é permitido")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("o arquivo não é UTF-8 válido") from exc
    return _load_contract_yaml_text(text)


def load_canonical_document(path: Path):
    raw = path.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        raise ContractError("BOM UTF-8 não é permitido")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ContractError("o arquivo não é UTF-8 válido") from exc
    if not lines or lines[0] != "---":
        raise ContractError("documento canônico deve começar com front matter")
    try:
        closing_delimiter = lines.index("---", 1)
    except ValueError as exc:
        raise ContractError("front matter sem delimitador de fechamento") from exc

    front_matter = "\n".join(lines[1:closing_delimiter]) + "\n"
    document = _load_contract_yaml_text(front_matter)
    body = "\n".join(lines[closing_delimiter + 1 :])
    return document, body


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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
def _is_date_time(value):
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


def validate_schema(instance, schema):
    validator = Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ContractError(details)


def _require_unique(values, label):
    if len(values) != len(set(values)):
        raise ContractError(f"{label} duplicado")


def validate_document_semantics(document, vocabulary):
    source_ids = [source["source_id"] for source in document["sources"]]
    _require_unique(source_ids, "source_id")
    known_sources = set(source_ids)

    tax = document["taxonomy"]
    if tax["taxonomy_version"] != vocabulary["vocabulary_version"]:
        raise ContractError("taxonomy_version desconhecida")
    if any(product not in vocabulary["products"] for product in document["products"]):
        raise ContractError("produto fora do vocabulário")
    modules = [tax["primary_module"], *tax["secondary_modules"]]
    if any(module not in vocabulary["modules"] for module in modules):
        raise ContractError("módulo fora do vocabulário")
    if tax["answer_mode"] not in vocabulary["answer_modes"]:
        raise ContractError("answer_mode fora do vocabulário")
    if any(
        erp not in vocabulary["erp_systems"]
        for erp in document["applicability"]["erp_systems"]
    ):
        raise ContractError("ERP fora do vocabulário")

    for product_version in document["applicability"]["product_versions"]:
        if product_version["product"] not in document["products"]:
            raise ContractError("aplicabilidade referencia produto não declarado")

    for section in document["sections"].values():
        override = section["classification_override"] or {}
        if any(
            product not in vocabulary["products"]
            for product in override.get("products", [])
        ):
            raise ContractError("override usa produto fora do vocabulário")
        override_modules = [
            value
            for value in [override.get("primary_module")]
            if value is not None
        ] + override.get("secondary_modules", [])
        if any(module not in vocabulary["modules"] for module in override_modules):
            raise ContractError("override usa módulo fora do vocabulário")
        answer_mode = override.get("answer_mode")
        if answer_mode is not None and answer_mode not in vocabulary["answer_modes"]:
            raise ContractError("override usa answer_mode fora do vocabulário")

        for source_ref in section["source_refs"]:
            if source_ref["source_id"] not in known_sources:
                raise ContractError("seção referencia source_id inexistente")
            locator = source_ref["locator"]
            if locator["kind"] in {"line_range", "page_range"}:
                if locator["end"] < locator["start"]:
                    raise ContractError("intervalo de origem invertido")


def validate_document_body(document, body):
    h1_titles = []
    body_sections = {}
    fence = None
    previous_level = 1

    for line_number, line in enumerate(body.splitlines(), start=1):
        fence_match = FENCE_RE.match(line)
        if fence is not None:
            if re.match(rf"^\s*{re.escape(fence[0])}{{{fence[1]},}}\s*$", line):
                fence = None
            continue
        if fence_match:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            continue

        heading = HEADING_RE.match(line)
        if not heading:
            continue
        level = len(heading.group(1))
        if level == 1:
            h1_titles.append(heading.group(2))
            previous_level = 1
            continue

        section_heading = SECTION_HEADING_RE.match(line)
        if not section_heading:
            raise ContractError(f"heading H{level} sem chave estável na linha {line_number}")
        if level > previous_level + 1:
            raise ContractError(f"salto de nível de heading na linha {line_number}")
        previous_level = level
        title = section_heading.group(2)
        section_key = section_heading.group(3)
        if section_key in body_sections:
            raise ContractError(f"section_key duplicada no corpo: {section_key}")
        body_sections[section_key] = {"title": title, "heading_level": level}

    if fence is not None:
        raise ContractError("code fence não fechado")
    if h1_titles != [document["title"]]:
        raise ContractError("o corpo deve ter um H1 igual ao title")
    if set(body_sections) != set(document["sections"]):
        raise ContractError("sections e headings do corpo não correspondem")
    for section_key, body_section in body_sections.items():
        metadata = document["sections"][section_key]
        if body_section["title"] != metadata["title"]:
            raise ContractError(f"título divergente na seção {section_key}")
        if body_section["heading_level"] != metadata["heading_level"]:
            raise ContractError(f"nível divergente na seção {section_key}")
    return body_sections


def validate_manifest_semantics(manifest, documents=None):
    batch_ids = [batch["batch_id"] for batch in manifest["batches"]]
    _require_unique(batch_ids, "batch_id")
    known_batches = set(batch_ids)

    paths = [entry["path"] for entry in manifest["entries"]]
    _require_unique(paths, "path")
    document_ids = [
        entry["document_id"]
        for entry in manifest["entries"]
        if entry["document_id"] is not None
    ]
    _require_unique(document_ids, "document_id")
    canonical_ids = {
        entry["document_id"]
        for entry in manifest["entries"]
        if entry["format"] == "canonical"
    }

    for entry in manifest["entries"]:
        if entry["batch_id"] not in known_batches:
            raise ContractError("entrada referencia batch_id inexistente")
        for successor in entry["successors"]:
            successor_id = successor["document_id"]
            if successor_id not in canonical_ids:
                raise ContractError("sucessor não resolve para documento canônico")
            if documents is not None:
                missing_keys = set(successor["section_keys"]) - set(
                    documents[successor_id]["sections"]
                )
                if missing_keys:
                    raise ContractError("sucessor referencia section_key inexistente")


def validate_manifest_bundle(manifest, document_schema, manifest_schema, vocabulary):
    validate_schema(manifest, manifest_schema)
    validate_manifest_semantics(manifest)
    documents = {}

    for entry in manifest["entries"]:
        if entry["format"] != "canonical":
            continue
        path = (ROOT / entry["path"]).resolve()
        try:
            path.relative_to(ROOT)
        except ValueError as exc:
            raise ContractError("path do manifesto escapa do repositório") from exc
        if not path.is_file():
            raise ContractError(f"documento canônico inexistente: {entry['path']}")

        document, body = load_canonical_document(path)
        validate_schema(document, document_schema)
        validate_document_semantics(document, vocabulary)
        validate_document_body(document, body)
        if document["document_id"] != entry["document_id"]:
            raise ContractError("document_id diverge entre manifesto e front matter")
        if document["document_id"] in documents:
            raise ContractError("document_id global duplicado")
        documents[document["document_id"]] = document

    validate_manifest_semantics(manifest, documents)
    return documents


class CanonicalDocumentContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document_schema = load_json(CONTRACT_DIR / "document.schema.json")
        cls.manifest_schema = load_json(CONTRACT_DIR / "manifest.schema.json")
        cls.vocabulary = load_json(CONTRACT_DIR / "vocabulary.json")

    def test_schemas_are_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(self.document_schema)
        Draft202012Validator.check_schema(self.manifest_schema)

    def test_valid_markdown_fixtures_cover_semantic_profiles_and_h2_to_h6(self):
        expected_types = {
            "procedure",
            "catalog",
            "data_dictionary",
            "troubleshooting",
            "sql_reference",
        }
        found_types = set()
        document_ids = []
        heading_levels = set()
        for path in sorted(VALID_FIXTURES.glob("*.md")):
            with self.subTest(path=path.name):
                document, body = load_canonical_document(path)
                validate_schema(document, self.document_schema)
                validate_document_semantics(document, self.vocabulary)
                body_sections = validate_document_body(document, body)
                found_types.add(document["semantic_type"])
                document_ids.append(document["document_id"])
                heading_levels.update(
                    section["heading_level"] for section in body_sections.values()
                )
        self.assertEqual(found_types, expected_types)
        self.assertEqual(heading_levels, set(range(2, 7)))
        self.assertEqual(len(document_ids), len(set(document_ids)))

    def test_valid_manifest_and_document_bundle(self):
        manifest = load_contract_yaml(VALID_FIXTURES / "manifest.yaml")
        documents = validate_manifest_bundle(
            manifest,
            self.document_schema,
            self.manifest_schema,
            self.vocabulary,
        )
        canonical_ids = {
            entry["document_id"]
            for entry in manifest["entries"]
            if entry["format"] == "canonical"
        }
        self.assertEqual(set(documents), canonical_ids)

    def test_template_is_a_valid_complete_document(self):
        document, body = load_canonical_document(CONTRACT_DIR / "template.md")
        validate_schema(document, self.document_schema)
        validate_document_semantics(document, self.vocabulary)
        validate_document_body(document, body)

    def test_duplicate_yaml_key_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "duplicada"):
            load_canonical_document(INVALID_FIXTURES / "document-duplicate-key.md")

    def test_invalid_source_reference_is_rejected(self):
        document, body = load_canonical_document(
            INVALID_FIXTURES / "document-invalid-source-reference.md"
        )
        validate_schema(document, self.document_schema)
        validate_document_body(document, body)
        with self.assertRaisesRegex(ContractError, "source_id inexistente"):
            validate_document_semantics(document, self.vocabulary)

    def test_duplicate_manifest_document_id_is_rejected(self):
        manifest = load_contract_yaml(
            INVALID_FIXTURES / "manifest-duplicate-document-id.yaml"
        )
        validate_schema(manifest, self.manifest_schema)
        with self.assertRaisesRegex(ContractError, "document_id duplicado"):
            validate_manifest_semantics(manifest)

    def test_unknown_version_field_and_vocabulary_are_rejected(self):
        document, _ = load_canonical_document(VALID_FIXTURES / "procedure.md")

        unknown_version = copy.deepcopy(document)
        unknown_version["schema_version"] = "1.1.0"
        with self.assertRaises(ContractError):
            validate_schema(unknown_version, self.document_schema)

        unknown_field = copy.deepcopy(document)
        unknown_field["processing_hash"] = "não pertence ao metadado editorial"
        with self.assertRaises(ContractError):
            validate_schema(unknown_field, self.document_schema)

        unknown_code = copy.deepcopy(document)
        unknown_code["taxonomy"]["primary_module"] = "modulo_inventado"
        validate_schema(unknown_code, self.document_schema)
        with self.assertRaisesRegex(ContractError, "módulo"):
            validate_document_semantics(unknown_code, self.vocabulary)

    def test_unsafe_yaml_alias_and_invalid_utf8_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            alias_path = Path(temp_dir) / "alias.yaml"
            alias_path.write_text("a: &valor 1\nb: *valor\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "anchors e aliases"):
                load_contract_yaml(alias_path)

            unsafe_tag_path = Path(temp_dir) / "unsafe-tag.yaml"
            unsafe_tag_path.write_text(
                "value: !!python/object/apply:os.system ['echo inseguro']\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ContractError, "inválido ou inseguro"):
                load_contract_yaml(unsafe_tag_path)

            invalid_utf8_path = Path(temp_dir) / "invalid.yaml"
            invalid_utf8_path.write_bytes(b"title: \xff\n")
            with self.assertRaisesRegex(ContractError, "UTF-8"):
                load_contract_yaml(invalid_utf8_path)

            bom_path = Path(temp_dir) / "bom.yaml"
            bom_path.write_bytes(codecs.BOM_UTF8 + b"title: teste\n")
            with self.assertRaisesRegex(ContractError, "BOM"):
                load_contract_yaml(bom_path)

    def test_manifest_document_id_mismatch_is_rejected(self):
        manifest = load_contract_yaml(VALID_FIXTURES / "manifest.yaml")
        invalid_manifest = copy.deepcopy(manifest)
        invalid_manifest["entries"][2]["document_id"] = (
            "ffffffff-ffff-4fff-8fff-ffffffffffff"
        )
        with self.assertRaisesRegex(ContractError, "diverge"):
            validate_manifest_bundle(
                invalid_manifest,
                self.document_schema,
                self.manifest_schema,
                self.vocabulary,
            )

    def test_invalid_successor_document_and_section_references_are_rejected(self):
        manifest = load_contract_yaml(VALID_FIXTURES / "manifest.yaml")

        invalid_document = copy.deepcopy(manifest)
        invalid_successor_entry = next(
            entry for entry in invalid_document["entries"] if entry["successors"]
        )
        invalid_successor_entry["successors"][0]["document_id"] = (
            "ffffffff-ffff-4fff-8fff-ffffffffffff"
        )
        validate_schema(invalid_document, self.manifest_schema)
        with self.assertRaisesRegex(ContractError, "sucessor"):
            validate_manifest_semantics(invalid_document)

        invalid_section = copy.deepcopy(manifest)
        invalid_section_entry = next(
            entry for entry in invalid_section["entries"] if entry["successors"]
        )
        invalid_section_entry["successors"][0]["section_keys"] = ["nao-existe"]
        documents = validate_manifest_bundle(
            manifest,
            self.document_schema,
            self.manifest_schema,
            self.vocabulary,
        )
        with self.assertRaisesRegex(ContractError, "section_key inexistente"):
            validate_manifest_semantics(invalid_section, documents)


if __name__ == "__main__":
    unittest.main()
