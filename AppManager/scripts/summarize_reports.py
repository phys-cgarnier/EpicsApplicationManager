#!/usr/bin/env python3
"""Write a concise summary from compact validator JSONs to GITHUB_STEP_SUMMARY.

Usage:
  python3 summarize_reports.py --subs compact_subs.json --templates compact_templates.json

If `GITHUB_STEP_SUMMARY` is not set, prints to stdout.
"""
import argparse
import json
import os
import sys


def load_json(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def build_summary(subs, templates):
    s = ''
    if subs:
        s += f"### Substitutions — files: {subs['summary']['files_validated']}, issues: {subs['summary']['total_issues']}\n"
        for f in subs.get('top_files', [])[:20]:
            s += f"- {f['file']}: {f['issue_count']} issues\n"
    if templates:
        s += f"\n### Templates — files: {templates.get('total_files',0)}, records: {templates.get('total_records',0)}\n"
        for f in templates.get('top_files', [])[:20]:
            s += f"- {f['file']}: {f.get('records')} records\n"
    if not s:
        s = 'No validator summaries found.'
    return s


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--subs', default='compact_subs.json')
    parser.add_argument('--templates', default='compact_templates.json')
    args = parser.parse_args()

    subs = load_json(args.subs)
    templates = load_json(args.templates)

    summary = build_summary(subs, templates)

    step_summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if step_summary_path:
        try:
            with open(step_summary_path, 'a') as f:
                f.write(summary)
        except Exception as e:
            print(summary)
            print(f"Failed writing to GITHUB_STEP_SUMMARY: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        print(summary)


if __name__ == '__main__':
    main()
