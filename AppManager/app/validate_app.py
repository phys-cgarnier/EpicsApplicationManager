#!/usr/bin/env python3
"""
Validate an EPICS application directory or selected files.

This module is the production-facing app-level validator entry point.

Extended help
-------------

This file contains an embedded, extended help description of the parser
and repository functionality. Use the `--help-full` flag to print this
extended help and exit.

Purpose
    - Validate EPICS application files: substitution files, template/db
        files, and archive files.
    - Produce human-friendly summaries or machine-readable JSON suitable
        for CI consumption.

Key components
    - AppManager/app/validate_app.py: CLI entrypoint that discovers files,
        classifies them, runs appropriate validators/analyzers, and emits
        either a human summary or a JSON payload.
    - AppManager/app/template_analyzer.py: Template parsing/analysis logic
        used to extract records, fields, macros, includes, and parsing
        errors from `.db`/`.template` files.
    - AppManager/app/ioc_validation_engine.py (imported as
        `ioc_validation_engine`): Provides `ValidationEngine` used to validate
        substitution and archive files and exposes a `Severity` enum used in
        reports.
    - AppManager/scripts/: Helper scripts for producing annotations and
        summarizing reports.
    - AppManager/tools/: Lower-level helpers used by the application and
        analyzers (archive/backup managers, consolidators, etc.).
    - tests/: Unit and integration tests. Run with `pytest` from the repo
        root.

File discovery and classification
    - Input `paths` may be files, directories, or glob patterns.
    - Directories are optionally scanned recursively with `-r/--recursive`.
    - Files are classified by extension into three categories:
        * Substitution: `.substitutions`, `.sub`, `.vdb`
        * Template: `.db`, `.template`
        * Archive: `.archive`, `.txt`

Template parsing behavior
    - `TemplateAnalyzer` parses `.db` and `.template` files to build a
        `template` object (or returns `None` on parse failure).
    - Collected metadata includes:
        * `records`: list of record objects with `record_type`,
            `record_name`, `line_number`, and `fields`.
        * `macros`: set/list of macros used in the template.
        * `includes`: list of included filenames referenced by the template.
        * `errors`: parse errors captured while analyzing the file.
    - If parsing fails, the analyzer populates an error message and this
        CLI reports a CRITICAL issue for that file.
    - The analyzer warns when an included file cannot be found relative
        to the template file's directory.

Substitution & archive validation
    - Substitution and archive files are validated via `ValidationEngine`.
    - Validation results are converted to JSON and merged with file
        metadata (file path and file type) by the CLI.

CLI usage (examples)
    python AppManager/app/validate_app.py BpmSoft --recursive --types all
    python AppManager/app/validate_app.py BpmSoft --recursive --types all --json
    python AppManager/app/validate_app.py BpmSoft --recursive --types all --json -o validation_report.json

Options summary
    - `paths` (positional): Files, directories, or glob patterns to validate.
    - `-r, --recursive`: Recurse into directories when discovering files.
    - `-t, --types`: Which types to validate (choices: `substitution`,
        `template`, `archive`, `all`). Default: `substitution template`.
    - `--json`: Emit JSON instead of a human-readable summary.
    - `-o, --output`: Write JSON output to a file (requires `--json`).
    - `--fail-on`: Smallest severity that causes non-zero exit. Choices:
        `critical`, `warning`, `info`. Default: `critical`.
    - `-v, --verbose`: Print discovered files to stderr and enable verbose
        analyzer output.
    - `--help-full`: Show this extended help text and exit.

Exit codes
    - `0`: Success and no issue meets or exceeds the `--fail-on` threshold.
    - `1`: One or more issues meet or exceed the `--fail-on` threshold.
    - `2`: No files found matching the requested types/paths.

JSON output schema
    - Top-level object with a `results` array. Each element represents one
        file and contains keys such as `file_path`, `file_type`, `passed`,
        and `issues`.
    - Template-specific fields include `records`, `macros`, `includes`,
        and `record_summary` (summarized record metadata).

Troubleshooting & tips
    - Use `-v/--verbose` to see discovered files and extra analyzer output.
    - If parsing fails, inspect `TemplateAnalyzer.errors` and re-run with
        `--help-full` or `-v` to get more context.

Use `--json` in CI to consume structured results and fail builds using
the `--fail-on` threshold.
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import List

from validation_engine import ValidationEngine, Severity
from template_analyzer import TemplateAnalyzer


SUB_EXTS = {".substitutions", ".sub", ".vdb"}
TEMPLATE_EXTS = {".db", ".template"}
ARCHIVE_EXTS = {".archive", ".txt"}

SEVERITY_ORDER = {
    "info": 1,
    "warning": 2,
    "critical": 3,
}


def find_files(paths: List[str], recursive: bool, types: List[str]) -> List[tuple[Path, str]]:
    """Resolve paths (files, directories, or globs) and classify matches by EPICS file type.

    Args:
        paths: File paths, directory paths, or glob patterns to search.
        recursive: If True, recurse into subdirectories.
        types: File categories to include — any combination of
               "substitution", "template", "archive", or "all".

    Returns:
        Sorted list of (path, category) tuples for every matching file.
    """
    files = []

    for p in paths:
        # Expand user (~) and environment variables
        p_expanded = os.path.expanduser(os.path.expandvars(p))
        pth = Path(p_expanded)

        if pth.is_file():
            files.append(pth)
            continue

        if pth.is_dir():
            if recursive:
                for f in pth.rglob("*"):
                    if f.is_file():
                        files.append(f)
            else:
                for f in pth.iterdir():
                    if f.is_file():
                        files.append(f)
            continue

        # Treat as glob pattern. Use the stdlib glob module which supports
        # absolute and non-relative patterns, and expands '**' when
        # recursive=True.
        matches = glob.glob(p_expanded, recursive=recursive)
        for m in matches:
            f = Path(m)
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

    parser.add_argument(
        "--help-full",
        action="store_true",
        help="Show extended help (embedded in the module docstring) and exit",
    )

    args = parser.parse_args()

    if getattr(args, "help_full", False):
        print(__doc__)
        sys.exit(0)

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