#!/usr/bin/env python3
"""Validate EPICS IOC startup scripts (st.cmd files) for an application."""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from AppManager.tools.validation_engine import ValidationEngine, Severity


def find_startup_scripts(app_dir: Path) -> list:
    """Given an application directory, find all st.cmd files under iocBoot."""
    ioc_boot = app_dir / "iocBoot"
    if not ioc_boot.is_dir():
        return []

    st_cmds = []
    for subdir in sorted(ioc_boot.iterdir()):
        if not subdir.is_dir():
            continue
        st_cmd = subdir / "st.cmd"
        if st_cmd.exists():
            st_cmds.append(st_cmd)
    return st_cmds


def print_result(result, filepath, ioc_name):
    print(f"\n{'='*60}")
    print(f"IOC: {ioc_name}")
    print(f"File: {filepath}")
    print(f"{'='*60}")

    if result.passed:
        print("PASSED")
    else:
        print("FAILED")

    for severity in Severity:
        issues = result.get_issues_by_severity(severity)
        if issues:
            print(f"\n  [{severity.value.upper()}] ({len(issues)})")
            for issue in issues:
                loc = f"line {issue.line_number}" if issue.line_number else ""
                print(f"    {loc}: {issue.message}")
                if issue.suggested_value:
                    print(f"      suggestion: {issue.suggested_value}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate EPICS IOC startup scripts for an application"
    )
    parser.add_argument(
        "app_dir", help="Path to the EPICS application directory"
    )
    parser.add_argument(
        "--config", "-c", default=None, help="Path to validation config JSON"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )
    args = parser.parse_args()

    app_path = Path(args.app_dir)
    if not app_path.is_dir():
        print(f"Error: Not a directory: {args.app_dir}", file=sys.stderr)
        sys.exit(1)

    st_cmds = find_startup_scripts(app_path)
    if not st_cmds:
        print(f"Error: No st.cmd files found under {app_path / 'iocBoot'}",
              file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(st_cmds)} IOC startup script(s) in {app_path.name}")

    engine = ValidationEngine(config_path=args.config)
    exit_code = 0

    for st_cmd in st_cmds:
        ioc_name = st_cmd.parent.name
        result = engine.validate_startup_script(str(st_cmd))

        if args.json:
            print(result.to_json())
        else:
            print_result(result, st_cmd, ioc_name)

        if not result.passed:
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
