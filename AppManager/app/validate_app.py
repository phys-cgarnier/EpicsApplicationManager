#!/usr/bin/env python3
"""
Validate an EPICS application directory or selected files.

This is the production-facing app-level validator entry point.

Current capabilities:
  - substitution validation through ValidationEngine
  - archive validation through ValidationEngine
  - template/db parsing through TemplateAnalyzer
  - JSON output to stdout or file

Usage:
  python AppManager/app/validate_app.py BpmSoft --recursive --types all
  python AppManager/app/validate_app.py BpmSoft --recursive --types all --json
  python AppManager/app/validate_app.py BpmSoft --recursive --types all --json -o validation_report.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List

from ioc_validation_engine import ValidationEngine, Severity
from template_analyzer import TemplateAnalyzer


SUB_EXTS = {".substitutions", ".sub", ".vdb"}
TEMPLATE_EXTS = {".db", ".template"}
ARCHIVE_EXTS = {".archive", ".txt"}

SEVERITY_ORDER = {
    "info": 1,
    "warning": 2,
    "critical": 3,
}


def find_files(paths: List[str], recursive: bool, types: List[str]):
    files = []

    for p in paths:
        pth = Path(p)

        if pth.is_file():
            files.append(pth)

        elif pth.is_dir():
            if recursive:
                for f in pth.rglob("*"):
                    if f.is_file():
                        files.append(f)
            else:
                for f in pth.iterdir():
                    if f.is_file():
                        files.append(f)

        else:
            # Treat as glob.
            for f in Path(".").glob(p):
                if f.is_file():
                    files.append(f)

    out = []

    for f in files:
        suf = f.suffix.lower()

        if "all" in types:
            if suf in SUB_EXTS:
                out.append((f, "substitution"))
            elif suf in TEMPLATE_EXTS:
                out.append((f, "template"))
            elif suf in ARCHIVE_EXTS:
                out.append((f, "archive"))

        elif "substitution" in types and suf in SUB_EXTS:
            out.append((f, "substitution"))

        elif "template" in types and suf in TEMPLATE_EXTS:
            out.append((f, "template"))

        elif "archive" in types and suf in ARCHIVE_EXTS:
            out.append((f, "archive"))

    return sorted(out, key=lambda item: str(item[0]))


def validate_template_file(path: Path, analyzer: TemplateAnalyzer) -> dict:
    """
    Validate/analyze a .db or .template file using TemplateAnalyzer.

    This replaces the old simplified validate_template_basic() function.
    """
    template = analyzer.parse_file(path)

    if template is None:
        matching_errors = [
            error for error in analyzer.errors if str(path) in error
        ]

        message = (
            matching_errors[-1]
            if matching_errors
            else f"Failed to parse template file: {path}"
        )

        return {
            "file_path": str(path),
            "file_type": "template",
            "passed": False,
            "records": 0,
            "macros": [],
            "includes": [],
            "issues": [
                {
                    "severity": Severity.CRITICAL.value,
                    "message": message,
                }
            ],
        }

    issues = []

    if not template.records:
        issues.append(
            {
                "severity": Severity.WARNING.value,
                "message": "No EPICS records found in template/db file",
            }
        )

    for include_path in template.includes:
        include_candidate = path.parent / include_path

        if not include_candidate.exists():
            issues.append(
                {
                    "severity": Severity.WARNING.value,
                    "message": f"Included file does not exist: {include_path}",
                }
            )

    return {
        "file_path": str(path),
        "file_type": "template",
        "passed": not any(
            issue.get("severity") == Severity.CRITICAL.value
            for issue in issues
        ),
        "records": len(template.records),
        "macros": sorted(template.macros),
        "includes": template.includes,
        "record_summary": [
            {
                "record_type": record.record_type,
                "record_name": record.record_name,
                "line_number": record.line_number,
                "field_count": len(record.fields),
                "fields": sorted(record.fields.keys()),
            }
            for record in template.records
        ],
        "issues": issues,
    }


def should_fail(results: list[dict], fail_on: str) -> bool:
    fail_level = SEVERITY_ORDER[fail_on]

    for result in results:
        for issue in result.get("issues", []):
            severity = issue.get("severity")
            if SEVERITY_ORDER.get(severity, 0) >= fail_level:
                return True

    return False


def write_json_report(payload: dict, output_path: str | None):
    json_text = json.dumps(payload, indent=2)

    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_text + "\n")
    else:
        print(json_text)


def print_human_summary(results: list[dict]):
    for result in results:
        fp = result.get("file_path") or result.get("file") or "unknown"
        file_type = result.get("file_type", "unknown")

        print(f"\nFile: {fp}")
        print(f"  Type: {file_type}")

        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue

        if "passed" in result:
            print(f"  PASSED: {result['passed']}")

        if file_type == "template":
            print(f"  Records: {result.get('records', 0)}")
            print(f"  Macros: {len(result.get('macros', []))}")
            print(f"  Includes: {len(result.get('includes', []))}")

        issues = result.get("issues", [])

        print(f"  Total issues: {len(issues)}")

        critical_count = len(
            [i for i in issues if i.get("severity") == Severity.CRITICAL.value]
        )
        warning_count = len(
            [i for i in issues if i.get("severity") == Severity.WARNING.value]
        )

        print(f"  Critical: {critical_count}")
        print(f"  Warnings: {warning_count}")

        for issue in issues:
            print(f"   - {issue.get('severity')}: {issue.get('message')}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate EPICS application files"
    )

    parser.add_argument(
        "paths",
        nargs="+",
        help="Files, directories, or glob patterns",
    )

    parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help="Recurse into directories",
    )

    parser.add_argument(
        "--types",
        "-t",
        nargs="+",
        default=["substitution", "template"],
        choices=["substitution", "template", "archive", "all"],
        help="Types to validate",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )

    parser.add_argument(
        "--output",
        "-o",
        help="Write JSON output to this file. Requires --json.",
    )

    parser.add_argument(
        "--fail-on",
        choices=["critical", "warning", "info"],
        default="critical",
        help="Smallest severity that causes a nonzero exit code",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print discovered files before validation",
    )

    args = parser.parse_args()

    if args.output and not args.json:
        parser.error("--output/-o requires --json")

    engine = ValidationEngine()
    template_analyzer = TemplateAnalyzer(verbose=args.verbose)

    files = find_files(args.paths, args.recursive, args.types)

    if args.verbose:
        print(f"Discovered {len(files)} file(s):", file=sys.stderr)
        for fpath, ftype in files:
            print(f"  [{ftype}] {fpath}", file=sys.stderr)

    if not files:
        print("No files found matching requested types/paths")
        sys.exit(2)

    results = []

    for fpath, ftype in files:
        if ftype == "substitution":
            validation_result = engine.validate_substitution_file(str(fpath))
            result = json.loads(validation_result.to_json())
            result.setdefault("file_path", str(fpath))
            result.setdefault("file_type", "substitution")
            results.append(result)

        elif ftype == "archive":
            validation_result = engine.validate_archive_file(str(fpath))
            result = json.loads(validation_result.to_json())
            result.setdefault("file_path", str(fpath))
            result.setdefault("file_type", "archive")
            results.append(result)

        elif ftype == "template":
            result = validate_template_file(fpath, template_analyzer)
            results.append(result)

    payload = {
        "results": results,
    }

    if args.json:
        write_json_report(payload, args.output)
    else:
        print_human_summary(results)

    sys.exit(1 if should_fail(results, args.fail_on) else 0)


if __name__ == "__main__":
    main()