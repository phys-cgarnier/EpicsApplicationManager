#!/usr/bin/env python3
"""Emit GitHub Actions annotations from a validator JSON and optionally exit non-zero.

Usage:
  python3 emit_annotations_and_fail.py --report validation_report.json --no-fail
"""
import argparse
import json
import sys
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', default='validation_report.json')
    parser.add_argument('--fail', dest='fail', action='store_true', help='Exit non-zero when issues found')
    parser.add_argument('--no-fail', dest='fail', action='store_false', help="Don't fail job on issues")
    parser.set_defaults(fail=True)
    args = parser.parse_args()

    if not os.path.exists(args.report):
        print(f"Report not found: {args.report}")
        sys.exit(0)

    with open(args.report) as f:
        data = json.load(f)

    total = data.get('summary', {}).get('total_issues', 0)

    for res in data.get('results', []):
        path = res.get('file')
        for issue in res.get('issues', []):
            msg = issue.get('message') or issue.get('msg') or str(issue)
            line = issue.get('line_number') or issue.get('line') or 1
            level = (issue.get('severity') or issue.get('level') or 'warning').lower()
            cmd = 'error' if level == 'error' else 'warning'
            # GitHub annotation
            print(f"::{cmd} file={path},line={line}::{msg}")

    if args.fail and total > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
