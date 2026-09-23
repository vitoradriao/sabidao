import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from preservation import _compare_evidence, compute_snapshot_sha256, verify_migration_report


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "preservation"
DOCUMENT_ID = "10000000-0000-4000-8000-000000000069"


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PreservationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ("source.md", "target.md"):
            (self.root / name).write_bytes((FIXTURES / name).read_bytes())
        (self.root / "figure.png").write_bytes(b"imagem sintetica")
        git(self.root, "init", "-q")
        git(self.root, "add", "source.md", "target.md", "figure.png")
        git(
            self.root,
            "-c", "user.name=Teste",
            "-c", "user.email=teste@example.invalid",
            "commit", "-qm", "snapshot sintetico",
        )
        commit = git(self.root, "rev-parse", "HEAD")
        target_lines = (self.root / "target.md").read_text(encoding="utf-8").splitlines()
        section_start = target_lines.index("## Consulta {#consulta}") + 1
        source_lines = (self.root / "source.md").read_text(encoding="utf-8").splitlines()
        source_start = source_lines.index("Se FLAG_X=1, não execute `--force`.") + 1
        self.report = {
            "schema_version": "1.0.0",
            "batch_id": "lote-sintetico",
            "original_snapshot": {"commit": commit, "sha256": ""},
            "source_unit_ids": ["u1"],
            "units": [{
                "unit_id": "u1",
                "source": {
                    "path": "source.md", "sha256": sha256(self.root / "source.md"),
                    "start_line": source_start, "end_line": len(source_lines),
                    "format": "legacy", "revision": None,
                },
                "destination": {
                    "path": "target.md", "sha256": sha256(self.root / "target.md"),
                    "canonical_id": DOCUMENT_ID, "section_key": "consulta",
                    "locator": {"kind": "line_range", "start": section_start, "end": len(target_lines)},
                },
                "disposition": None,
                "review": {"status": "reviewed", "reviewer": "Pessoa teste", "reviewed_at": "2026-09-22T12:00:00-03:00"},
            }],
        }
        self.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.report)

    def verify(self):
        return verify_migration_report(self.report, self.root)

    def change_target(self, original, replacement):
        path = self.root / "target.md"
        contents = path.read_text(encoding="utf-8")
        self.assertIn(original, contents)
        path.write_text(contents.replace(original, replacement), encoding="utf-8")
        self.report["units"][0]["destination"]["sha256"] = sha256(path)

    def test_complete_reviewed_preservation_passes(self):
        result = self.verify()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["original_snapshot"], self.report["original_snapshot"])
        self.assertEqual(result["coverage"], {"mapped": 1, "total": 1})
        self.assertEqual(result["unit_results"][0]["status"], "passed")
        json.dumps(result)

    def test_deliberate_losses_are_critical(self):
        cases = [
            ("| FLAG_X | ativo |", "| FLAG_X | inativo |", "table_cell"),
            ("`--force`", "`--safe`", "identifier"),
            ("Se FLAG_X=1, não execute `--force`.", "FLAG_X=1, não execute `--force`.", "condition"),
            ("não execute", "execute", "negation"),
            ("Exceto em homologação", "Em homologação", "exception"),
            ("- Mantenha o campo NUMPED", "- Remova o campo NUMPED", "list"),
            ("SELECT NUMPED FROM PCPEDC WHERE FLAG_X = 1;", "SELECT NUMPED FROM PCPEDC;", "code"),
            ("https://example.invalid/manual", "https://example.invalid/outro", "link"),
            ("![Fluxo](figure.png)", "", "image"),
        ]
        for original, replacement, expected_category in cases:
            with self.subTest(category=expected_category):
                path = self.root / "target.md"
                original_contents = (FIXTURES / "target.md").read_text(encoding="utf-8")
                path.write_text(original_contents, encoding="utf-8")
                self.change_target(original, replacement)
                result = self.verify()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["unit_results"][0]["status"], "failed")
                self.assertIn(expected_category, {loss["category"] for loss in result["critical_losses"]})

    def test_swapped_table_columns_are_critical_even_when_cells_remain(self):
        path = self.root / "target.md"
        contents = path.read_text(encoding="utf-8")
        self.assertIn("| FLAG_X | ativo |", contents)
        contents = contents.replace("| FLAG_X | ativo |", "| ativo | FLAG_X |", 1)
        path.write_text(contents, encoding="utf-8")
        self.report["units"][0]["destination"]["sha256"] = sha256(path)
        result = self.verify()
        self.assertEqual(result["status"], "failed")
        self.assertIn("table_cell", {loss["category"] for loss in result["critical_losses"]})

    def test_plain_facts_and_compound_identifiers_cannot_change_silently(self):
        for original, changed in (
            ("O prazo é de 2 dias.", "O prazo é de 3 dias."),
            ("Confirme o pedido PED-123.", "Confirme o pedido PED-124."),
            ("## Pedidos cancelados", "## Pedidos {#pedidos}"),
        ):
            with self.subTest(original=original):
                losses = _compare_evidence(original, changed, "u1")
                self.assertIn("text", {loss["category"] for loss in losses})

    def test_missing_local_image_in_destination_fails(self):
        (self.root / "figure.png").unlink()
        result = self.verify()
        self.assertEqual(result["status"], "failed")
        self.assertIn("image", {loss["category"] for loss in result["critical_losses"]})

    def test_changed_image_bytes_fail_even_when_link_is_unchanged(self):
        (self.root / "figure.png").write_bytes(b"outra imagem")
        result = self.verify()
        self.assertEqual(result["status"], "failed")
        self.assertIn("image", {loss["category"] for loss in result["critical_losses"]})

    def test_pending_review_and_unmapped_unit_are_incomplete(self):
        self.report["units"][0]["review"] = {"status": "pending", "reviewer": None, "reviewed_at": None}
        result = self.verify()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["review_status"], "pending")
        self.report["units"][0]["review"] = {"status": "reviewed", "reviewer": "Pessoa teste", "reviewed_at": "2026-09-22T12:00:00-03:00"}
        self.report["source_unit_ids"].append("u2")
        self.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.report)
        result = self.verify()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["coverage"], {"mapped": 1, "total": 2})

    def test_hash_mismatch_and_invalid_canonical_target_fail(self):
        self.change_target("ativo", "inativo")
        self.report["units"][0]["destination"]["sha256"] = "0" * 64
        result = self.verify()
        self.assertEqual(result["status"], "failed")
        self.assertIn("integrity", {loss["category"] for loss in result["critical_losses"]})
        self.report["units"][0]["destination"]["sha256"] = sha256(self.root / "target.md")
        self.change_target("schema_version: 1.0.0", "schema_version: 2.0.0")
        self.assertEqual(self.verify()["status"], "failed")

    def test_canonical_source_requires_valid_matching_revision(self):
        source = self.report["units"][0]["source"]
        target = self.report["units"][0]["destination"]
        source.update({
            "path": "target.md", "sha256": target["sha256"],
            "start_line": target["locator"]["start"],
            "end_line": target["locator"]["end"],
            "format": "canonical", "revision": 1,
        })
        self.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.report)
        self.assertEqual(self.verify()["status"], "passed")
        source["revision"] = 2
        self.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.report)
        self.assertEqual(self.verify()["status"], "failed")

    def test_snapshot_is_anchored_in_commit_and_inventory(self):
        altered = copy.deepcopy(self.report)
        altered["units"][0]["source"]["sha256"] = "0" * 64
        self.assertEqual(verify_migration_report(altered, self.root)["status"], "failed")
        altered = copy.deepcopy(self.report)
        altered["original_snapshot"]["commit"] = "0" * 40
        altered["original_snapshot"]["sha256"] = compute_snapshot_sha256(altered)
        self.assertEqual(verify_migration_report(altered, self.root)["status"], "incomplete")

    def test_reviewed_exclusion_and_fusion_rules(self):
        unit = self.report["units"][0]
        unit["destination"] = None
        unit["disposition"] = {
            "kind": "excluded", "reason": "duplicata sintética fora do lote",
            "reviewer": "Pessoa teste", "reviewed_at": "2026-09-22T12:00:00-03:00",
        }
        self.assertEqual(self.verify()["status"], "passed")
        unit["disposition"]["kind"] = "merged"
        self.assertEqual(self.verify()["status"], "failed")

    def test_cli_returns_distinct_status_codes(self):
        cli = Path(__file__).resolve().parents[1] / "scripts" / "verify_canonical_migration.py"
        report_path = self.root / "report.json"

        def run():
            report_path.write_text(json.dumps(self.report), encoding="utf-8")
            process = subprocess.run(
                [sys.executable, str(cli), str(report_path), "--root", str(self.root)],
                capture_output=True, text=True, check=False, timeout=10,
            )
            return process.returncode, json.loads(process.stdout)["status"]

        self.assertEqual(run(), (0, "passed"))
        self.report["units"][0]["review"]["status"] = "pending"
        self.assertEqual(run(), (2, "incomplete"))
        self.report["units"][0]["review"]["status"] = "reviewed"
        self.change_target("| FLAG_X | ativo |", "| FLAG_X | inativo |")
        self.assertEqual(run(), (1, "failed"))


if __name__ == "__main__":
    unittest.main()
