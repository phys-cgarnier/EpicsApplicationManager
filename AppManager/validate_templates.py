#!/usr/bin/env python3
"""Validate EPICS template files (.db, .vdb, .template)

This script is a small wrapper around `AppManager.template_analyzer.TemplateAnalyzer`.
It can validate (parse) a single file or an entire directory, produce a report,
and output JSON for automated processing.

Usage examples:
  # Analyze a directory and print a human report
  python3 AppManager/tools/validate_templates.py /path/to/dbdir

  # Analyze and write JSON summary
  python3 AppManager/tools/validate_templates.py /path/to/dbdir --json --output summary.json

"""
import argparse
import json
import sys
from pathlib import Path

# Ensure AppManager modules can be imported when running from parent folder
appmanager_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(appmanager_dir))

try:
    from AppManager.tools.template_analyzer import TemplateAnalyzer
except Exception as e:
    print(f"Error importing TemplateAnalyzer: {e}", file=sys.stderr)
    sys.exit(2)


def analyze_path(target: Path, extensions: list, verbose: bool):
    analyzer = TemplateAnalyzer(verbose=verbose)

    if target.is_file():
        tpl = analyzer.parse_file(target)
        if tpl:
            analyzer.templates[str(target)] = tpl
    elif target.is_dir():
        analyzer.analyze_directory(target, extensions)
    else:
        print(f"Error: {target} is not a file or directory")
        sys.exit(2)

    return analyzer


def build_summary(analyzer: TemplateAnalyzer):
    files = {}
    for name, tpl in analyzer.templates.items():
        files[name] = {
            'records': len(tpl.records),
            'macros': sorted(list(tpl.macros))[:50],
            'includes': tpl.includes,
            'file_type': tpl.file_type,
        }

    duplicates = analyzer.find_duplicates(threshold=0.95)
    consolidation = analyzer.find_consolidation_candidates()

    summary = {
        'total_files': len(analyzer.templates),
        'total_records': sum(len(t.records) for t in analyzer.templates.values()),
        'files': files,
        'duplicates': duplicates,
        'consolidation_candidates': consolidation,
        'errors': analyzer.errors,
    }

    return summary


def main():
    parser = argparse.ArgumentParser(description='Validate and analyze EPICS template files')
    parser.add_argument('target', help='File or directory to analyze')
    parser.add_argument('--extensions', nargs='+', default=['.db', '.vdb', '.template'],
                        help='File extensions to analyze')
    parser.add_argument('--json', action='store_true', help='Output JSON summary')
    parser.add_argument('--output', help='Write JSON or text output to file')
    parser.add_argument('--compact-output', '-c', default=None,
                        help='Also write a compact summary JSON (small)')
    parser.add_argument('--compact-limit', type=int, default=10,
                        help='Max files to include in compact summary')
    parser.add_argument('--quiet', action='store_true', help='Minimize stdout (only final messages)')
    parser.add_argument('--threshold', type=float, default=0.95, help='Duplicate similarity threshold')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose logging')

    args = parser.parse_args()

    target = Path(args.target)
    analyzer = analyze_path(target, args.extensions, args.verbose)

    # Use analyzer to find duplicates with requested threshold
    duplicates = analyzer.find_duplicates(threshold=args.threshold)
    summary = build_summary(analyzer)
    summary['duplicates'] = duplicates

    if args.json:
        out = json.dumps(summary, indent=2)
        if args.output:
            with open(args.output, 'w') as f:
                f.write(out)
            if not args.quiet:
                print(f"Wrote JSON summary to {args.output}")
        else:
            if not args.quiet:
                print(out)
            else:
                # when quiet and printing to stdout, still print minimal summary
                print(json.dumps({'total_files': summary.get('total_files'), 'total_records': summary.get('total_records')}))
        # write compact if requested
        if args.compact_output:
            compact = {'total_files': summary.get('total_files'), 'total_records': summary.get('total_records')}
            # prefer files with errors, then largest by record count
            files = []
            for name, tpl in analyzer.templates.items():
                files.append({'file': name, 'records': len(tpl.records), 'errors': analyzer.errors.get(name)})
            files.sort(key=lambda x: ((0 if x.get('errors') else 1), -x['records']))
            compact['top_files'] = files[:args.compact_limit]
            with open(args.compact_output, 'w') as f:
                json.dump(compact, f, indent=2)
            if not args.quiet:
                print(f"Wrote compact summary to {args.compact_output}")
        return

    # Human-readable output
    if args.output:
        out_file = Path(args.output)
        with open(out_file, 'w') as f:
            f.write(analyzer.generate_report())
            f.write('\n')
            if duplicates:
                f.write('DUPLICATE PAIRS:\n')
                for a, b, score in duplicates:
                    f.write(f"  {a}  <->  {b}  ({score*100:.1f}% similar)\n")
        if not args.quiet:
            print(f"Wrote report to {args.output}")
    else:
        if not args.quiet:
            print(analyzer.generate_report())
            if duplicates:
                print('\nDUPLICATE PAIRS:')
                for a, b, score in duplicates:
                    print(f"  {a}  <->  {b}  ({score*100:.1f}% similar)")
        else:
            # quiet mode: print minimal summary
            print(json.dumps({'total_files': summary.get('total_files'), 'total_records': summary.get('total_records')}))

    # write compact summary when requested even for human report mode
    if args.compact_output:
        compact = {'total_files': summary.get('total_files'), 'total_records': summary.get('total_records')}
        files = []
        for name, tpl in analyzer.templates.items():
            files.append({'file': name, 'records': len(tpl.records), 'errors': analyzer.errors.get(name)})
        files.sort(key=lambda x: ((0 if x.get('errors') else 1), -x['records']))
        compact['top_files'] = files[:args.compact_limit]
        with open(args.compact_output, 'w') as f:
            json.dump(compact, f, indent=2)
        if not args.quiet:
            print(f"Wrote compact summary to {args.compact_output}")


if __name__ == '__main__':
    main()
