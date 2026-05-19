#!/usr/bin/env python3
"""
EPICS Template Analyzer
=======================
Analyze and compare EPICS .db, .vdb, and .template files to identify
duplicates, similarities, and consolidation opportunities.

Author: SLAC Cryoplant Team
Date: 2024
"""

import re
import os
import sys
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Set, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict
import difflib
import json
from datetime import datetime


@dataclass
class EPICSRecord:
    """Represents a single EPICS record"""
    record_type: str
    record_name: str
    fields: Dict[str, str]
    line_number: int
    raw_content: str

    def get_normalized_name(self) -> str:
        """Get record name without macros for comparison"""
        # Remove macro substitutions for comparison
        return re.sub(r'\$\([^)]+\)', '${MACRO}', self.record_name)

    def get_signature(self) -> str:
        """Generate a signature for comparison"""
        # Create signature from record type and field names (not values)
        field_names = sorted(self.fields.keys())
        return f"{self.record_type}:{','.join(field_names)}"


@dataclass
class TemplateFile:
    """Represents a complete template file"""
    filepath: Path
    records: List[EPICSRecord] = field(default_factory=list)
    macros: Set[str] = field(default_factory=set)
    includes: List[str] = field(default_factory=list)
    file_type: str = ""  # .db, .vdb, .template

    def get_record_signatures(self) -> Dict[str, int]:
        """Get count of each record signature"""
        signatures = defaultdict(int)
        for record in self.records:
            signatures[record.get_signature()] += 1
        return dict(signatures)

    def calculate_similarity(self, other: 'TemplateFile') -> float:
        """Calculate similarity score with another template (0-1)"""
        if not self.records or not other.records:
            return 0.0

        # Compare record signatures
        self_sigs = set(r.get_signature() for r in self.records)
        other_sigs = set(r.get_signature() for r in other.records)

        if not self_sigs and not other_sigs:
            return 1.0

        intersection = len(self_sigs & other_sigs)
        union = len(self_sigs | other_sigs)

        return intersection / union if union > 0 else 0.0


class TemplateAnalyzer:
    """Main analyzer for EPICS template files"""

    # Regex patterns for parsing
    RECORD_PATTERN = re.compile(r'^record\s*\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)\s*{', re.MULTILINE)
    FIELD_PATTERN = re.compile(r'^\s*field\s*\(\s*(\w+)\s*,\s*"?([^")]+)"?\s*\)', re.MULTILINE)
    MACRO_PATTERN = re.compile(r'\$\(([^)]+)\)')
    INCLUDE_PATTERN = re.compile(r'^include\s+"([^"]+)"', re.MULTILINE)
    INFO_PATTERN = re.compile(r'^\s*info\s*\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)', re.MULTILINE)

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.templates: Dict[str, TemplateFile] = {}
        self.errors: List[str] = []

    def log(self, message: str):
        """Print message if verbose mode enabled"""
        if self.verbose:
            print(f"[INFO] {message}")

    def parse_file(self, filepath: Path) -> Optional[TemplateFile]:
        """Parse a single template file"""
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception as e:
            self.errors.append(f"Error reading {filepath}: {e}")
            return None

        template = TemplateFile(
            filepath=filepath,
            file_type=filepath.suffix
        )

        # Find all includes
        for match in self.INCLUDE_PATTERN.finditer(content):
            template.includes.append(match.group(1))

        # Find all macros
        for match in self.MACRO_PATTERN.finditer(content):
            template.macros.add(match.group(1))

        # Parse records
        record_matches = list(self.RECORD_PATTERN.finditer(content))

        for i, match in enumerate(record_matches):
            record_type = match.group(1)
            record_name = match.group(2)
            start_pos = match.start()

            # Find the end of this record (next record or end of file)
            if i < len(record_matches) - 1:
                end_pos = record_matches[i + 1].start()
            else:
                end_pos = len(content)

            record_content = content[start_pos:end_pos]

            # Parse fields within this record
            fields = {}
            for field_match in self.FIELD_PATTERN.finditer(record_content):
                field_name = field_match.group(1)
                field_value = field_match.group(2)
                fields[field_name] = field_value

            # Parse info fields
            for info_match in self.INFO_PATTERN.finditer(record_content):
                info_name = f"info_{info_match.group(1)}"
                fields[info_name] = info_match.group(2)

            # Get line number
            line_number = content[:start_pos].count('\n') + 1

            record = EPICSRecord(
                record_type=record_type,
                record_name=record_name,
                fields=fields,
                line_number=line_number,
                raw_content=record_content
            )

            template.records.append(record)

        self.log(f"Parsed {filepath.name}: {len(template.records)} records, "
                f"{len(template.macros)} macros, {len(template.includes)} includes")

        return template

    def analyze_directory(self, directory: Path, extensions: List[str] = None) -> Dict[str, TemplateFile]:
        """Analyze all template files in a directory"""
        if extensions is None:
            extensions = ['.db', '.vdb', '.template']

        templates = {}

        for ext in extensions:
            for filepath in directory.rglob(f'*{ext}'):
                self.log(f"Processing {filepath}")
                template = self.parse_file(filepath)
                if template:
                    rel_path = filepath.relative_to(directory)
                    templates[str(rel_path)] = template

        self.templates = templates
        return templates

    def find_duplicates(self, threshold: float = 0.95) -> List[Tuple[str, str, float]]:
        """Find duplicate or very similar templates"""
        duplicates = []
        template_list = list(self.templates.items())

        for i in range(len(template_list)):
            for j in range(i + 1, len(template_list)):
                name1, template1 = template_list[i]
                name2, template2 = template_list[j]

                similarity = template1.calculate_similarity(template2)
                if similarity >= threshold:
                    duplicates.append((name1, name2, similarity))

        return sorted(duplicates, key=lambda x: x[2], reverse=True)

    def find_similar_records(self, threshold: float = 0.8) -> Dict[str, List[Tuple[str, str]]]:
        """Find similar record definitions across files"""
        record_map = defaultdict(list)

        # Build map of record signatures to files
        for name, template in self.templates.items():
            for record in template.records:
                sig = record.get_signature()
                record_map[sig].append((name, record.record_name))

        # Find signatures that appear in multiple files
        similar = {}
        for sig, occurrences in record_map.items():
            unique_files = set(occ[0] for occ in occurrences)
            if len(unique_files) > 1:
                similar[sig] = occurrences

        return similar

    def compare_files(self, file1: str, file2: str) -> Dict[str, Any]:
        """Detailed comparison of two template files"""
        if file1 not in self.templates or file2 not in self.templates:
            return {"error": "One or both files not found in analyzed templates"}

        template1 = self.templates[file1]
        template2 = self.templates[file2]

        # Record signatures comparison
        sigs1 = set(r.get_signature() for r in template1.records)
        sigs2 = set(r.get_signature() for r in template2.records)

        # Macro comparison
        macros1 = template1.macros
        macros2 = template2.macros

        # Generate unified diff of raw content
        with open(template1.filepath, 'r') as f1, open(template2.filepath, 'r') as f2:
            lines1 = f1.readlines()
            lines2 = f2.readlines()
            diff = list(difflib.unified_diff(lines1, lines2,
                                            fromfile=file1, tofile=file2, lineterm=''))

        return {
            "similarity": template1.calculate_similarity(template2),
            "file1": {
                "records": len(template1.records),
                "macros": sorted(macros1),
                "unique_signatures": sorted(sigs1 - sigs2)
            },
            "file2": {
                "records": len(template2.records),
                "macros": sorted(macros2),
                "unique_signatures": sorted(sigs2 - sigs1)
            },
            "common": {
                "signatures": sorted(sigs1 & sigs2),
                "macros": sorted(macros1 & macros2)
            },
            "diff_preview": diff[:50] if diff else []
        }

    def analyze_vdb_to_db_mapping(self) -> Dict[str, Any]:
        """Analyze relationship between .vdb templates and .db instances"""
        vdb_files = {k: v for k, v in self.templates.items() if v.file_type == '.vdb'}
        db_files = {k: v for k, v in self.templates.items() if v.file_type == '.db'}

        mappings = []

        for vdb_name, vdb_template in vdb_files.items():
            vdb_base = Path(vdb_name).stem
            potential_matches = []

            for db_name, db_template in db_files.items():
                # Check if db file name contains vdb base name
                if vdb_base in db_name:
                    similarity = vdb_template.calculate_similarity(db_template)
                    if similarity > 0.5:  # Reasonable threshold
                        potential_matches.append({
                            "db_file": db_name,
                            "similarity": similarity
                        })

            if potential_matches:
                mappings.append({
                    "vdb_template": vdb_name,
                    "derived_db_files": sorted(potential_matches,
                                             key=lambda x: x['similarity'],
                                             reverse=True)
                })

        return {
            "vdb_count": len(vdb_files),
            "db_count": len(db_files),
            "mappings": mappings
        }

    def find_consolidation_candidates(self) -> List[Dict[str, Any]]:
        """Find groups of files that could potentially be consolidated into templates"""
        candidates = []
        processed = set()

        template_list = list(self.templates.items())

        for i, (name1, template1) in enumerate(template_list):
            if name1 in processed:
                continue

            group = [name1]
            group_signatures = set(r.get_signature() for r in template1.records)

            for j, (name2, template2) in enumerate(template_list[i+1:], i+1):
                if name2 in processed:
                    continue

                similarity = template1.calculate_similarity(template2)
                if similarity > 0.8:  # High similarity threshold
                    group.append(name2)
                    processed.add(name2)
                    # Update group signatures
                    sigs2 = set(r.get_signature() for r in template2.records)
                    group_signatures &= sigs2  # Keep common signatures

            if len(group) > 1:
                # Calculate what percentage of records are common
                common_record_percentage = len(group_signatures) / len(set(r.get_signature()
                                         for r in template1.records)) * 100

                candidates.append({
                    "files": group,
                    "count": len(group),
                    "common_signatures": len(group_signatures),
                    "common_percentage": round(common_record_percentage, 1),
                    "potential_name": self._suggest_template_name(group)
                })

            processed.add(name1)

        return sorted(candidates, key=lambda x: x['count'], reverse=True)

    def _suggest_template_name(self, file_group: List[str]) -> str:
        """Suggest a template name for a group of similar files"""
        # Extract common parts from filenames
        if not file_group:
            return "template"

        names = [Path(f).stem for f in file_group]

        # Find common prefix
        common_prefix = os.path.commonprefix(names)
        if common_prefix and not common_prefix.endswith('_'):
            # Clean up the prefix
            common_prefix = common_prefix.rstrip('_0123456789')

        return f"{common_prefix}_template" if common_prefix else "consolidated_template"

    def generate_report(self) -> str:
        """Generate comprehensive analysis report"""
        report = []
        report.append("=" * 80)
        report.append("EPICS Template Analysis Report")
        report.append("=" * 80)
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        # Summary statistics
        report.append("SUMMARY STATISTICS:")
        total_records = sum(len(t.records) for t in self.templates.values())
        report.append(f"  Total files analyzed: {len(self.templates)}")
        report.append(f"  Total records: {total_records}")

        file_types = defaultdict(int)
        for template in self.templates.values():
            file_types[template.file_type] += 1

        report.append(f"  File types:")
        for ext, count in sorted(file_types.items()):
            report.append(f"    {ext}: {count} files")
        report.append("")

        # Macro usage
        all_macros = set()
        for template in self.templates.values():
            all_macros.update(template.macros)

        report.append(f"MACRO USAGE:")
        report.append(f"  Total unique macros: {len(all_macros)}")
        if all_macros:
            report.append(f"  Most common macros:")
            macro_count = defaultdict(int)
            for template in self.templates.values():
                for macro in template.macros:
                    macro_count[macro] += 1

            for macro, count in sorted(macro_count.items(), key=lambda x: x[1], reverse=True)[:10]:
                report.append(f"    ${macro}: used in {count} files")
        report.append("")

        # Duplication analysis
        duplicates = self.find_duplicates(threshold=0.9)
        report.append("DUPLICATION ANALYSIS:")
        if duplicates:
            report.append(f"  Found {len(duplicates)} potential duplicate pairs (>90% similar):")
            for file1, file2, similarity in duplicates[:5]:
                report.append(f"    {file1}")
                report.append(f"    {file2}")
                report.append(f"    Similarity: {similarity*100:.1f}%")
                report.append("")
        else:
            report.append("  No significant duplicates found")
        report.append("")

        # Consolidation opportunities
        candidates = self.find_consolidation_candidates()
        report.append("CONSOLIDATION OPPORTUNITIES:")
        if candidates:
            report.append(f"  Found {len(candidates)} groups that could be consolidated:")
            for candidate in candidates[:5]:
                report.append(f"    Group: {candidate['potential_name']}")
                report.append(f"      Files: {candidate['count']}")
                report.append(f"      Common records: {candidate['common_percentage']}%")
                for file in candidate['files'][:3]:
                    report.append(f"        - {file}")
                if len(candidate['files']) > 3:
                    report.append(f"        ... and {len(candidate['files']) - 3} more")
                report.append("")
        else:
            report.append("  No consolidation opportunities found")

        report.append("")

        # VDB to DB mapping
        mapping = self.analyze_vdb_to_db_mapping()
        if mapping['mappings']:
            report.append("TEMPLATE TO INSTANCE MAPPING (.vdb -> .db):")
            report.append(f"  Templates (.vdb): {mapping['vdb_count']}")
            report.append(f"  Instances (.db): {mapping['db_count']}")
            report.append(f"  Identified mappings:")
            for m in mapping['mappings'][:5]:
                report.append(f"    {m['vdb_template']}:")
                for db in m['derived_db_files'][:3]:
                    report.append(f"      -> {db['db_file']} ({db['similarity']*100:.1f}% similar)")
                report.append("")

        # Errors
        if self.errors:
            report.append("ERRORS ENCOUNTERED:")
            for error in self.errors[:10]:
                report.append(f"  - {error}")
            if len(self.errors) > 10:
                report.append(f"  ... and {len(self.errors) - 10} more")
            report.append("")

        report.append("=" * 80)

        return '\n'.join(report)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='EPICS Template Analyzer - Analyze and compare .db, .vdb, and .template files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze all templates in a directory
  %(prog)s analyze /path/to/db/directory

  # Compare two specific files
  %(prog)s compare file1.db file2.db

  # Find duplicates with custom threshold
  %(prog)s duplicates /path/to/db --threshold 0.8

  # Find consolidation opportunities
  %(prog)s consolidate /path/to/db

  # Analyze VDB to DB relationships
  %(prog)s mapping /path/to/db

  # Generate full report
  %(prog)s report /path/to/db --output report.txt
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Analyze command
    analyze_parser = subparsers.add_parser('analyze', help='Analyze templates in directory')
    analyze_parser.add_argument('directory', help='Directory containing template files')
    analyze_parser.add_argument('--extensions', nargs='+',
                               default=['.db', '.vdb', '.template'],
                               help='File extensions to analyze')

    # Compare command
    compare_parser = subparsers.add_parser('compare', help='Compare two template files')
    compare_parser.add_argument('file1', help='First file to compare')
    compare_parser.add_argument('file2', help='Second file to compare')
    compare_parser.add_argument('--detailed', action='store_true',
                               help='Show detailed comparison')

    # Duplicates command
    dup_parser = subparsers.add_parser('duplicates', help='Find duplicate templates')
    dup_parser.add_argument('directory', help='Directory to analyze')
    dup_parser.add_argument('--threshold', type=float, default=0.95,
                          help='Similarity threshold (0-1)')

    # Consolidate command
    consol_parser = subparsers.add_parser('consolidate',
                                         help='Find consolidation opportunities')
    consol_parser.add_argument('directory', help='Directory to analyze')

    # Mapping command
    map_parser = subparsers.add_parser('mapping', help='Analyze VDB to DB mappings')
    map_parser.add_argument('directory', help='Directory to analyze')

    # Report command
    report_parser = subparsers.add_parser('report', help='Generate full analysis report')
    report_parser.add_argument('directory', help='Directory to analyze')
    report_parser.add_argument('--output', help='Output file (default: print to stdout)')

    # Add verbose flag to all subparsers
    for subparser in [analyze_parser, compare_parser, dup_parser,
                     consol_parser, map_parser, report_parser]:
        subparser.add_argument('-v', '--verbose', action='store_true',
                             help='Verbose output')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    analyzer = TemplateAnalyzer(verbose=args.verbose if hasattr(args, 'verbose') else False)

    if args.command == 'analyze':
        directory = Path(args.directory)
        if not directory.exists():
            print(f"Error: Directory {directory} does not exist")
            sys.exit(1)

        templates = analyzer.analyze_directory(directory, args.extensions)
        print(f"Analyzed {len(templates)} template files")

        # Print summary
        file_types = defaultdict(int)
        total_records = 0
        for template in templates.values():
            file_types[template.file_type] += 1
            total_records += len(template.records)

        print(f"Total records: {total_records}")
        print("File types:")
        for ext, count in sorted(file_types.items()):
            print(f"  {ext}: {count} files")

    elif args.command == 'compare':
        # First analyze the directory containing these files
        file1_path = Path(args.file1)
        file2_path = Path(args.file2)

        # Analyze both files
        template1 = analyzer.parse_file(file1_path)
        template2 = analyzer.parse_file(file2_path)

        if template1 and template2:
            analyzer.templates[args.file1] = template1
            analyzer.templates[args.file2] = template2

            comparison = analyzer.compare_files(args.file1, args.file2)

            print(f"Comparison of {args.file1} and {args.file2}:")
            print(f"Similarity: {comparison['similarity']*100:.1f}%")
            print(f"\nFile 1: {comparison['file1']['records']} records")
            print(f"  Macros: {', '.join(comparison['file1']['macros'][:5])}")
            print(f"\nFile 2: {comparison['file2']['records']} records")
            print(f"  Macros: {', '.join(comparison['file2']['macros'][:5])}")
            print(f"\nCommon signatures: {len(comparison['common']['signatures'])}")

            if args.detailed and comparison.get('diff_preview'):
                print("\nDiff preview (first 50 lines):")
                for line in comparison['diff_preview']:
                    print(line.rstrip())

    elif args.command == 'duplicates':
        directory = Path(args.directory)
        analyzer.analyze_directory(directory)

        duplicates = analyzer.find_duplicates(threshold=args.threshold)

        if duplicates:
            print(f"Found {len(duplicates)} potential duplicates (>{args.threshold*100:.0f}% similar):")
            for file1, file2, similarity in duplicates:
                print(f"\n{similarity*100:.1f}% similar:")
                print(f"  - {file1}")
                print(f"  - {file2}")
        else:
            print("No significant duplicates found")

    elif args.command == 'consolidate':
        directory = Path(args.directory)
        analyzer.analyze_directory(directory)

        candidates = analyzer.find_consolidation_candidates()

        if candidates:
            print(f"Found {len(candidates)} consolidation opportunities:\n")
            for i, candidate in enumerate(candidates, 1):
                print(f"{i}. Suggested name: {candidate['potential_name']}")
                print(f"   Files to consolidate: {candidate['count']}")
                print(f"   Common records: {candidate['common_percentage']}%")
                print(f"   Files:")
                for file in candidate['files'][:5]:
                    print(f"     - {file}")
                if len(candidate['files']) > 5:
                    print(f"     ... and {len(candidate['files']) - 5} more")
                print()
        else:
            print("No consolidation opportunities found")

    elif args.command == 'mapping':
        directory = Path(args.directory)
        analyzer.analyze_directory(directory)

        mapping = analyzer.analyze_vdb_to_db_mapping()

        print(f"Template to Instance Mapping Analysis:")
        print(f"Templates (.vdb): {mapping['vdb_count']}")
        print(f"Instances (.db): {mapping['db_count']}")

        if mapping['mappings']:
            print(f"\nIdentified mappings:")
            for m in mapping['mappings']:
                print(f"\n{m['vdb_template']}:")
                for db in m['derived_db_files'][:5]:
                    print(f"  -> {db['db_file']} ({db['similarity']*100:.1f}% similar)")
                if len(m['derived_db_files']) > 5:
                    print(f"  ... and {len(m['derived_db_files']) - 5} more")
        else:
            print("\nNo clear VDB to DB mappings found")

    elif args.command == 'report':
        directory = Path(args.directory)
        analyzer.analyze_directory(directory)

        report = analyzer.generate_report()

        if args.output:
            with open(args.output, 'w') as f:
                f.write(report)
            print(f"Report saved to {args.output}")
        else:
            print(report)


if __name__ == '__main__':
    main()