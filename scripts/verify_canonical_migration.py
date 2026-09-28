"""CLI offline do gate de preservação documental canônica."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from preservation import verify_migration_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="relatório JSON da migração")
    parser.add_argument("--root", type=Path, default=ROOT, help="raiz do checkout")
    arguments = parser.parse_args(argv)
    try:
        report = json.loads(arguments.report.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "incomplete", "error": str(exc)}, ensure_ascii=False))
        return 2
    result = verify_migration_report(report, arguments.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"passed": 0, "failed": 1, "incomplete": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
