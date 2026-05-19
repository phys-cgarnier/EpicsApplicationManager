#!/usr/bin/env python3
"""
EPICS IOC Substitution File Manager
====================================
A comprehensive tool for managing, formatting, validating, and analyzing
EPICS IOC substitution files.

Author: Enhanced version
Date: 2024
"""

import re
import os
import sys
import argparse
import shutil
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass
from collections import defaultdict
import json
from datetime import datetime


@dataclass
class SubstitutionBlock:
    """Represents a single substitution block in the file"""
    filename: str
    pattern: List[str]
    rows: List[List[str]]
    comments: List[Tuple[int, str]]  # (row_index, comment_text)
    line_number: int

    def validate(self) -> List[str]:
        """Validate the block structure"""
        errors = []
        if not self.pattern:
            errors.append(f"Block at line {self.line_number}: No pattern defined")

        for i, row in enumerate(self.rows):
            if len(row) != len(self.pattern):
                errors.append(f"Block at line {self.line_number}, row {i+1}: "
                            f"Column count mismatch (expected {len(self.pattern)}, got {len(row)})")

        return errors


class SubstitutionFileManager:
    """Main class for managing EPICS substitution files"""

    # Constants for formatting
    PATTERN_INDENT = "    pattern   "
    ROW_INDENT = "              "
    MIN_COLUMN_SPACING = 2

    # Regex patterns
    FILE_PATTERN = re.compile(r'^file\s+([A-Za-z0-9_\-\.]+)')
    PATTERN_LINE = re.compile(r'^\s*pattern\s*{(.+)}', re.IGNORECASE)
    DATA_LINE = re.compile(r'^\s*{(.+)}')
    COMMENT_LINE = re.compile(r'^\s*#')
    BLOCK_END = re.compile(r'^\s*}')

    # Regex for splitting CSV while preserving quotes
    COMMA_MATCHER = re.compile(r',(?=(?:[^"\']*["\'][^"\']*["\'])*[^"\']*$)')

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.blocks: List[SubstitutionBlock] = []
        self.global_comments: List[Tuple[int, str]] = []
        self.statistics: Dict[str, Any] = {}

    def log(self, message: str):
        """Print message if verbose mode is enabled"""
        if self.verbose:
            print(f"[INFO] {message}")

    def parse_file(self, filepath: str) -> bool:
        """Parse a substitution file into structured blocks"""
        self.blocks = []
        self.global_comments = []

        try:
            with open(filepath, 'r') as f:
                lines = f.readlines()
        except IOError as e:
            print(f"Error reading file {filepath}: {e}")
            return False

        i = 0
        while i < len(lines):
            line = lines[i]

            # Handle global comments
            if self.COMMENT_LINE.match(line) and not self._is_inside_block(i, lines):
                self.global_comments.append((i, line.rstrip()))
                i += 1
                continue

            # Check for file block start
            file_match = self.FILE_PATTERN.match(line)
            if file_match:
                block = self._parse_block(lines, i)
                if block:
                    self.blocks.append(block)
                    i = self._find_block_end(lines, i) + 1
                else:
                    i += 1
            else:
                i += 1

        self.log(f"Parsed {len(self.blocks)} blocks from {filepath}")
        return True

    def _is_inside_block(self, index: int, lines: List[str]) -> bool:
        """Check if a line index is inside a substitution block"""
        # Simple heuristic: look backwards for 'file' and forwards for '}'
        for i in range(max(0, index - 20), index):
            if self.FILE_PATTERN.match(lines[i]):
                # Look forward for closing brace
                for j in range(index, min(len(lines), index + 100)):
                    if self.BLOCK_END.match(lines[j]):
                        return True
        return False

    def _parse_block(self, lines: List[str], start_idx: int) -> Optional[SubstitutionBlock]:
        """Parse a single substitution block"""
        file_match = self.FILE_PATTERN.match(lines[start_idx])
        if not file_match:
            return None

        filename = file_match.group(1)
        pattern = []
        rows = []
        comments = []

        i = start_idx + 1
        while i < len(lines):
            line = lines[i]

            # Check for block end
            if self.BLOCK_END.match(line) and not self.PATTERN_LINE.match(line) and not self.DATA_LINE.match(line):
                break

            # Parse pattern line
            pattern_match = self.PATTERN_LINE.match(line)
            if pattern_match:
                pattern_str = pattern_match.group(1)
                pattern = self._parse_csv_line(pattern_str)

            # Parse data line
            elif self.DATA_LINE.match(line):
                data_str = self.DATA_LINE.match(line).group(1)
                row_data = self._parse_csv_line(data_str)
                rows.append(row_data)

            # Handle comments within block
            elif self.COMMENT_LINE.match(line):
                comments.append((len(rows), line.rstrip()))

            i += 1

        return SubstitutionBlock(
            filename=filename,
            pattern=pattern,
            rows=rows,
            comments=comments,
            line_number=start_idx + 1
        )

    def _find_block_end(self, lines: List[str], start_idx: int) -> int:
        """Find the end of a substitution block"""
        brace_count = 0
        i = start_idx + 1

        while i < len(lines):
            if '{' in lines[i]:
                brace_count += lines[i].count('{')
            if '}' in lines[i]:
                brace_count -= lines[i].count('}')
                if brace_count <= 0:
                    return i
            i += 1

        return len(lines) - 1

    def _parse_csv_line(self, line: str) -> List[str]:
        """Parse a CSV line preserving quotes"""
        # Split by comma, excluding commas within quotes
        parts = self.COMMA_MATCHER.split(line)
        # Clean up whitespace outside quotes
        cleaned = []
        for part in parts:
            cleaned.append(self._clean_value(part))
        return cleaned

    def _clean_value(self, value: str) -> str:
        """Clean a value, preserving quotes but removing external whitespace"""
        value = value.strip()
        # If the value is quoted, preserve the quotes but clean inside
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            return value
        else:
            # Remove all whitespace from unquoted values
            return re.sub(r'\s+', '', value)

    def format_blocks(self) -> str:
        """Format all blocks with aligned columns"""
        output = []

        # Add global comments at the beginning
        for _, comment in self.global_comments[:20]:  # First 20 lines of comments
            output.append(comment)

        # Format each block
        for block in self.blocks:
            output.append(self._format_block(block))

        # Add trailing comment if it existed
        output.append('#')

        return '\n'.join(output)

    def _format_block(self, block: SubstitutionBlock) -> str:
        """Format a single substitution block"""
        lines = []

        # Calculate column widths
        widths = self._calculate_column_widths(block)

        # Add file line
        lines.append(f"file {block.filename}")
        lines.append("{")

        # Add pattern line
        pattern_line = self.PATTERN_INDENT + "{ "
        for i, col in enumerate(block.pattern):
            if i == len(block.pattern) - 1:
                pattern_line += col.ljust(widths[i]) + "  }"
            else:
                pattern_line += col + "," + " " * (widths[i] - len(col) + self.MIN_COLUMN_SPACING)
        lines.append(pattern_line)

        # Add data rows with comments
        comment_dict = {idx: comment for idx, comment in block.comments}

        for row_idx, row in enumerate(block.rows):
            # Check for comment before this row
            if row_idx in comment_dict:
                lines.append(comment_dict[row_idx])

            # Format data row
            row_line = self.ROW_INDENT + "{ "
            for i, col in enumerate(row):
                if i == len(row) - 1:
                    row_line += col.ljust(widths[i]) + "  }"
                else:
                    row_line += col + "," + " " * (widths[i] - len(col) + self.MIN_COLUMN_SPACING)
            lines.append(row_line)

        lines.append("}")

        return '\n'.join(lines)

    def _calculate_column_widths(self, block: SubstitutionBlock) -> Dict[int, int]:
        """Calculate the maximum width for each column"""
        widths = {}

        # Check pattern widths
        for i, col in enumerate(block.pattern):
            widths[i] = len(col)

        # Check all row widths
        for row in block.rows:
            for i, col in enumerate(row):
                if i not in widths or len(col) > widths[i]:
                    widths[i] = len(col)

        return widths

    def validate(self) -> List[str]:
        """Validate all blocks in the file"""
        errors = []

        for block in self.blocks:
            block_errors = block.validate()
            errors.extend(block_errors)

        # Check for duplicate patterns within same file
        file_patterns = defaultdict(list)
        for block in self.blocks:
            key = (block.filename, tuple(block.pattern))
            file_patterns[key].append(block.line_number)

        for (filename, pattern), line_numbers in file_patterns.items():
            if len(line_numbers) > 1:
                errors.append(f"Duplicate pattern in file {filename} at lines: {', '.join(map(str, line_numbers))}")

        return errors

    def get_statistics(self) -> Dict[str, Any]:
        """Generate statistics about the substitution file"""
        stats = {
            'total_blocks': len(self.blocks),
            'total_rows': sum(len(b.rows) for b in self.blocks),
            'files': {},
            'macros': defaultdict(int),
            'unique_patterns': set(),
            'max_columns': 0,
            'total_comments': len(self.global_comments) + sum(len(b.comments) for b in self.blocks)
        }

        for block in self.blocks:
            # Count by file
            if block.filename not in stats['files']:
                stats['files'][block.filename] = {'count': 0, 'rows': 0}
            stats['files'][block.filename]['count'] += 1
            stats['files'][block.filename]['rows'] += len(block.rows)

            # Track unique patterns
            stats['unique_patterns'].add(tuple(block.pattern))

            # Track maximum columns
            if len(block.pattern) > stats['max_columns']:
                stats['max_columns'] = len(block.pattern)

            # Count macro usage
            for row in block.rows:
                for value in row:
                    macros = re.findall(r'\$\(([^)]+)\)', value)
                    for macro in macros:
                        stats['macros'][macro] += 1

        stats['unique_patterns'] = len(stats['unique_patterns'])
        stats['macros'] = dict(stats['macros'])

        return stats

    def find_pattern(self, search_term: str) -> List[Tuple[SubstitutionBlock, List[int]]]:
        """Find blocks and rows containing a search term"""
        results = []
        search_lower = search_term.lower()

        for block in self.blocks:
            matching_rows = []
            for i, row in enumerate(block.rows):
                for value in row:
                    if search_lower in value.lower():
                        matching_rows.append(i)
                        break

            if matching_rows:
                results.append((block, matching_rows))

        return results

    def merge_files(self, other_file: str) -> bool:
        """Merge another substitution file into this one"""
        other_manager = SubstitutionFileManager(self.verbose)
        if not other_manager.parse_file(other_file):
            return False

        self.blocks.extend(other_manager.blocks)
        self.log(f"Merged {len(other_manager.blocks)} blocks from {other_file}")
        return True

    def split_by_file(self, output_dir: str) -> Dict[str, str]:
        """Split substitutions into separate files based on template file"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        file_blocks = defaultdict(list)
        for block in self.blocks:
            file_blocks[block.filename].append(block)

        output_files = {}
        for filename, blocks in file_blocks.items():
            base_name = Path(filename).stem
            output_file = output_dir / f"{base_name}_split.substitutions"

            temp_manager = SubstitutionFileManager()
            temp_manager.blocks = blocks

            with open(output_file, 'w') as f:
                f.write(temp_manager.format_blocks())

            output_files[filename] = str(output_file)
            self.log(f"Created {output_file} with {len(blocks)} blocks")

        return output_files

    def export_to_json(self, output_file: str):
        """Export the substitution data to JSON format"""
        data = {
            'metadata': {
                'generated': datetime.now().isoformat(),
                'total_blocks': len(self.blocks)
            },
            'blocks': []
        }

        for block in self.blocks:
            block_data = {
                'file': block.filename,
                'pattern': block.pattern,
                'rows': block.rows,
                'line_number': block.line_number
            }
            data['blocks'].append(block_data)

        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)

        self.log(f"Exported to JSON: {output_file}")

    def generate_report(self) -> str:
        """Generate a detailed report about the substitution file"""
        stats = self.get_statistics()
        errors = self.validate()

        report = []
        report.append("=" * 80)
        report.append("EPICS Substitution File Analysis Report")
        report.append("=" * 80)
        report.append("")

        # Statistics section
        report.append("STATISTICS:")
        report.append(f"  Total blocks: {stats['total_blocks']}")
        report.append(f"  Total rows: {stats['total_rows']}")
        report.append(f"  Unique patterns: {stats['unique_patterns']}")
        report.append(f"  Maximum columns: {stats['max_columns']}")
        report.append(f"  Total comments: {stats['total_comments']}")
        report.append("")

        # Files section
        report.append("FILES USED:")
        for filename, info in sorted(stats['files'].items()):
            report.append(f"  {filename}: {info['count']} blocks, {info['rows']} rows")
        report.append("")

        # Macros section
        if stats['macros']:
            report.append("MACROS USED:")
            for macro, count in sorted(stats['macros'].items(), key=lambda x: x[1], reverse=True)[:20]:
                report.append(f"  ${macro}: {count} occurrences")
            report.append("")

        # Validation section
        report.append("VALIDATION:")
        if errors:
            report.append(f"  Found {len(errors)} issues:")
            for error in errors[:10]:  # Show first 10 errors
                report.append(f"    - {error}")
            if len(errors) > 10:
                report.append(f"    ... and {len(errors) - 10} more")
        else:
            report.append("  No validation errors found")
        report.append("")

        report.append("=" * 80)

        return '\n'.join(report)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='EPICS IOC Substitution File Manager - Format, validate, and analyze substitution files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Format a file (creates backup)
  %(prog)s format input.substitutions

  # Format in-place without backup
  %(prog)s format input.substitutions --in-place

  # Validate a file
  %(prog)s validate input.substitutions

  # Generate statistics report
  %(prog)s stats input.substitutions

  # Search for a pattern
  %(prog)s search input.substitutions "PLCTAG"

  # Merge multiple files
  %(prog)s merge output.substitutions input1.substitutions input2.substitutions

  # Split by template file
  %(prog)s split input.substitutions --output-dir ./split_files

  # Export to JSON
  %(prog)s export input.substitutions --output data.json
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Format command
    format_parser = subparsers.add_parser('format', help='Format substitution file')
    format_parser.add_argument('input', help='Input substitution file')
    format_parser.add_argument('-o', '--output', help='Output file (default: input_formatted.substitutions)')
    format_parser.add_argument('-i', '--in-place', action='store_true', help='Modify file in-place')
    format_parser.add_argument('--no-backup', action='store_true', help='Do not create backup when using in-place')

    # Validate command
    validate_parser = subparsers.add_parser('validate', help='Validate substitution file')
    validate_parser.add_argument('input', help='Input substitution file')

    # Statistics command
    stats_parser = subparsers.add_parser('stats', help='Generate statistics report')
    stats_parser.add_argument('input', help='Input substitution file')
    stats_parser.add_argument('--json', action='store_true', help='Output as JSON')

    # Search command
    search_parser = subparsers.add_parser('search', help='Search for patterns or values')
    search_parser.add_argument('input', help='Input substitution file')
    search_parser.add_argument('term', help='Search term')

    # Merge command
    merge_parser = subparsers.add_parser('merge', help='Merge multiple substitution files')
    merge_parser.add_argument('output', help='Output file')
    merge_parser.add_argument('inputs', nargs='+', help='Input files to merge')

    # Split command
    split_parser = subparsers.add_parser('split', help='Split by template file')
    split_parser.add_argument('input', help='Input substitution file')
    split_parser.add_argument('--output-dir', default='./split', help='Output directory')

    # Export command
    export_parser = subparsers.add_parser('export', help='Export to JSON')
    export_parser.add_argument('input', help='Input substitution file')
    export_parser.add_argument('-o', '--output', help='Output JSON file')

    # Add verbose flag to all subparsers
    for subparser in [format_parser, validate_parser, stats_parser, search_parser,
                      merge_parser, split_parser, export_parser]:
        subparser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Create manager instance
    manager = SubstitutionFileManager(verbose=args.verbose if hasattr(args, 'verbose') else False)

    # Execute commands
    if args.command == 'format':
        if not manager.parse_file(args.input):
            sys.exit(1)

        formatted = manager.format_blocks()

        if args.in_place:
            if not args.no_backup:
                backup_file = args.input + '.backup'
                shutil.copy2(args.input, backup_file)
                print(f"Backup created: {backup_file}")

            with open(args.input, 'w') as f:
                f.write(formatted)
            print(f"Formatted file in-place: {args.input}")
        else:
            output_file = args.output or args.input.replace('.substitutions', '_formatted.substitutions')
            with open(output_file, 'w') as f:
                f.write(formatted)
            print(f"Formatted file saved to: {output_file}")

    elif args.command == 'validate':
        if not manager.parse_file(args.input):
            sys.exit(1)

        errors = manager.validate()
        if errors:
            print(f"Validation found {len(errors)} issues:")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)
        else:
            print("Validation successful - no issues found")

    elif args.command == 'stats':
        if not manager.parse_file(args.input):
            sys.exit(1)

        if args.json:
            stats = manager.get_statistics()
            print(json.dumps(stats, indent=2))
        else:
            print(manager.generate_report())

    elif args.command == 'search':
        if not manager.parse_file(args.input):
            sys.exit(1)

        results = manager.find_pattern(args.term)
        if results:
            print(f"Found '{args.term}' in {len(results)} blocks:")
            for block, rows in results:
                print(f"\n  File: {block.filename} (line {block.line_number})")
                print(f"  Matching rows: {', '.join(map(lambda x: str(x+1), rows))}")
                for row_idx in rows[:3]:  # Show first 3 matching rows
                    print(f"    Row {row_idx+1}: {block.rows[row_idx]}")
        else:
            print(f"No matches found for '{args.term}'")

    elif args.command == 'merge':
        # Start with first input file
        if not manager.parse_file(args.inputs[0]):
            sys.exit(1)

        # Merge additional files
        for input_file in args.inputs[1:]:
            if not manager.merge_files(input_file):
                print(f"Error merging {input_file}")
                sys.exit(1)

        # Write merged output
        formatted = manager.format_blocks()
        with open(args.output, 'w') as f:
            f.write(formatted)
        print(f"Merged {len(args.inputs)} files into {args.output}")
        print(f"Total blocks: {len(manager.blocks)}")

    elif args.command == 'split':
        if not manager.parse_file(args.input):
            sys.exit(1)

        output_files = manager.split_by_file(args.output_dir)
        print(f"Split into {len(output_files)} files:")
        for template, output in output_files.items():
            print(f"  {template} -> {output}")

    elif args.command == 'export':
        if not manager.parse_file(args.input):
            sys.exit(1)

        output_file = args.output or args.input.replace('.substitutions', '.json')
        manager.export_to_json(output_file)
        print(f"Exported to: {output_file}")


if __name__ == '__main__':
    main()