#!/usr/bin/env python3
"""
CLI validator for substitution and template files (non-GUI)

Usage examples:
  python3 tools/ioc_manager/cli_validate.py path/to/file.substitutions
  python3 tools/ioc_manager/cli_validate.py path/to/dir --recursive --json

This script uses the existing ValidationEngine from `ioc_validation_engine.py`.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List

from ioc_validation_engine import ValidationEngine, Severity

SUB_EXTS = {".substitutions", ".sub", ".vdb"}
TEMPLATE_EXTS = {".db", ".template"}
ARCHIVE_EXTS = {".archive", ".txt"}


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
            # treat as glob
            for f in Path(".").glob(p):
                if f.is_file():
                    files.append(f)
    # filter by types
    out = []
    for f in files:
        suf = f.suffix.lower()
        if "substitution" in types and suf in SUB_EXTS:
            out.append((f, "substitution"))
        elif "template" in types and suf in TEMPLATE_EXTS:
            out.append((f, "template"))
        elif "archive" in types and suf in ARCHIVE_EXTS:
            out.append((f, "archive"))
        else:
            # if user asked for all types, include known extensions
            if "all" in types:
                if suf in SUB_EXTS:
                    out.append((f, "substitution"))
                elif suf in TEMPLATE_EXTS:
                    out.append((f, "template"))
                elif suf in ARCHIVE_EXTS:
                    out.append((f, "archive"))
    return out


def validate_template_basic(path: Path):
    """Very small sanity check for template/db files: readable and contains 'record('"""
    try:
        text = path.read_text()
    except Exception as e:
        return {"file_path": str(path), "error": f"Cannot read file: {e}"}

    contains_record = "record(" in text or "record (" in text
    issues = []
    if not contains_record:
        issues.append(
            {
                "severity": Severity.INFO.value,
                "message": "No 'record(' occurrences found - file may not contain DB templates",
            }
        )

    return {
        "file_path": str(path),
        "sanity": True,
        "contains_record": contains_record,
        "issues": issues,
    }


def main():
    p = argparse.ArgumentParser(
        description="Validate substitution and template files (non-GUI)"
    )
    p.add_argument("paths", nargs="+", help="Files or directories or glob patterns")
    p.add_argument(
        "--recursive", "-r", action="store_true", help="Recurse into directories"
    )
    p.add_argument(
        "--types",
        "-t",
        nargs="+",
        default=["substitution", "template"],
        choices=["substitution", "template", "archive", "all"],
        help="Types to validate",
    )
    p.add_argument("--json", action="store_true", help="Output JSON")

    p.add_argument(
        "--output",
        "-o",
        help="Write JSON output to this file. Requires --json.",
    )
    args = p.parse_args()

    if args.output and not args.json:
        p.error("--output/-o requires --json")

    engine = ValidationEngine()

    files = find_files(args.paths, args.recursive, args.types)
    if not files:
        print("No files found matching requested types/paths")
        sys.exit(2)

    results = []
    for fpath, ftype in files:
        if ftype == "substitution":
            vr = engine.validate_substitution_file(str(fpath))
            results.append(json.loads(vr.to_json()))
        elif ftype == "archive":
            vr = engine.validate_archive_file(str(fpath))
            results.append(json.loads(vr.to_json()))
        elif ftype == "template":
            tr = validate_template_basic(fpath)
            results.append(tr)

    if args.json:
        json_text = json.dumps({"results": results}, indent=2)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json_text + "\n")
        else:
            print(json_text)
    else:
        # human readable summary
        for r in results:
            fp = r.get("file_path") or r.get("file") or "unknown"
            print(f"\nFile: {fp}")
            if "error" in r:
                print(f"  ERROR: {r['error']}")
                continue
            if "passed" in r:
                print(f"  PASSED: {r['passed']}")
                print(
                    f"  Total issues: {r.get('statistics', {}).get('total_issues', len(r.get('issues', [])))}"
                )
                print(
                    f"  Critical: {len([i for i in r.get('issues', []) if i.get('severity')=='critical'])}"
                )
                print(
                    f"  Warnings: {len([i for i in r.get('issues', []) if i.get('severity')=='warning'])}"
                )

            elif "sanity" in r:
                print(
                    f"  Template sanity check: contains 'record(': {r['contains_record']}"
                )
                for issue in r.get("issues", []):
                    print(f"   - {issue['severity']}: {issue['message']}")

    # exit code 0 if no critical issues found
    any_critical = False
    for r in results:
        if "issues" in r:
            for i in r["issues"]:
                if i.get("severity") == "critical":
                    any_critical = True
    sys.exit(1 if any_critical else 0)


if __name__ == "__main__":
    main()
