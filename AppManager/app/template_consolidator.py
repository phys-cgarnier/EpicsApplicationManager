#!/usr/bin/env python3
"""
EPICS Template Consolidator
============================
Generate unified templates from similar .db files and create migration
strategies for consolidating duplicate template logic.

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
from collections import defaultdict, Counter
import difflib
import json
from datetime import datetime


@dataclass
class MacroMapping:
    """Represents macro substitutions needed for consolidation"""
    original_value: str
    macro_name: str
    description: str
    files_using: List[str] = field(default_factory=list)


@dataclass
class ConsolidationPlan:
    """Plan for consolidating multiple files into a template"""
    target_name: str
    source_files: List[str]
    common_records: List[Dict[str, Any]]
    variable_records: List[Dict[str, Any]]
    new_macros: List[MacroMapping]
    substitution_mappings: Dict[str, Dict[str, str]]  # file -> {macro: value}
    savings: Dict[str, Any]  # Statistics about consolidation benefits


class TemplateConsolidator:
    """Consolidate multiple similar EPICS database files into templates"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.file_contents: Dict[str, str] = {}
        self.parsed_records: Dict[str, List[Dict]] = {}

    def log(self, message: str):
        """Print message if verbose mode enabled"""
        if self.verbose:
            print(f"[INFO] {message}")

    def analyze_files(self, files: List[Path]) -> ConsolidationPlan:
        """Analyze files for consolidation potential"""
        self.log(f"Analyzing {len(files)} files for consolidation")

        # Read and parse all files
        for filepath in files:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                self.file_contents[str(filepath)] = content
                self.parsed_records[str(filepath)] = self._parse_records(content)

        # Find common structure
        common_records, variable_records = self._find_common_structure(files)

        # Identify needed macros
        new_macros = self._identify_macros(files, variable_records)

        # Create substitution mappings
        sub_mappings = self._create_substitution_mappings(files, new_macros)

        # Calculate savings
        savings = self._calculate_savings(files, common_records, variable_records)

        # Suggest target name
        target_name = self._suggest_template_name(files)

        return ConsolidationPlan(
            target_name=target_name,
            source_files=[str(f) for f in files],
            common_records=common_records,
            variable_records=variable_records,
            new_macros=new_macros,
            substitution_mappings=sub_mappings,
            savings=savings
        )

    def _parse_records(self, content: str) -> List[Dict]:
        """Parse EPICS records from file content"""
        records = []
        record_pattern = re.compile(r'record\s*\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)\s*{([^}]+)}', re.DOTALL)

        for match in record_pattern.finditer(content):
            record_type = match.group(1)
            record_name = match.group(2)
            record_body = match.group(3)

            # Parse fields
            field_pattern = re.compile(r'field\s*\(\s*(\w+)\s*,\s*"?([^")]+)"?\s*\)')
            fields = {}
            for field_match in field_pattern.finditer(record_body):
                fields[field_match.group(1)] = field_match.group(2)

            records.append({
                'type': record_type,
                'name': record_name,
                'fields': fields,
                'raw': match.group(0)
            })

        return records

    def _find_common_structure(self, files: List[Path]) -> Tuple[List[Dict], List[Dict]]:
        """Find records that are common vs variable across files"""
        all_records = []
        for filepath in files:
            all_records.extend(self.parsed_records[str(filepath)])

        # Group records by normalized structure
        record_groups = defaultdict(list)
        for i, record in enumerate(all_records):
            # Create a signature without specific values
            sig = self._get_record_signature(record)
            record_groups[sig].append((i, record, str(files[i % len(files)])))

        common_records = []
        variable_records = []

        for sig, group in record_groups.items():
            if len(group) == len(files):
                # Record appears in all files - candidate for common template
                common_records.append(group[0][1])  # Take first occurrence
            else:
                # Record varies between files
                for _, record, source in group:
                    record['source_file'] = source
                    variable_records.append(record)

        return common_records, variable_records

    def _get_record_signature(self, record: Dict) -> str:
        """Get normalized signature for record comparison"""
        # Create signature from type and field names (not values)
        field_names = sorted(record['fields'].keys())
        # Normalize the record name to ignore macro differences
        normalized_name = re.sub(r'\$\([^)]+\)', 'MACRO', record['name'])
        normalized_name = re.sub(r'[0-9]+', 'NUM', normalized_name)
        return f"{record['type']}:{normalized_name}:{','.join(field_names)}"

    def _identify_macros(self, files: List[Path], variable_records: List[Dict]) -> List[MacroMapping]:
        """Identify macros needed for consolidation"""
        macros = []
        macro_counter = 1

        # Analyze differences in record names
        name_variations = defaultdict(set)
        for record in variable_records:
            base_name = re.sub(r'[0-9]+', '', record['name'])
            base_name = re.sub(r'\$\([^)]+\)', '', base_name)
            name_variations[base_name].add(record['name'])

        # Create macros for varying parts
        for base_name, variations in name_variations.items():
            if len(variations) > 1:
                # Find the varying part
                varying_parts = set()
                for name in variations:
                    # Extract parts that vary
                    match = re.search(r'([0-9]+|[A-Z]+[0-9]+)', name)
                    if match:
                        varying_parts.add(match.group(1))

                if varying_parts:
                    macro = MacroMapping(
                        original_value=list(varying_parts)[0],
                        macro_name=f"VAR{macro_counter}",
                        description=f"Variable part for {base_name}",
                        files_using=[str(f) for f in files]
                    )
                    macros.append(macro)
                    macro_counter += 1

        # Analyze differences in field values
        field_variations = defaultdict(lambda: defaultdict(set))
        for record in variable_records:
            for field_name, field_value in record['fields'].items():
                if not re.match(r'\$\(', field_value):  # Not already a macro
                    field_variations[record['type']][field_name].add(field_value)

        # Create macros for varying field values
        for record_type, fields in field_variations.items():
            for field_name, values in fields.items():
                if len(values) > 1 and len(values) <= len(files):
                    macro = MacroMapping(
                        original_value=list(values)[0],
                        macro_name=f"{field_name.upper()}_VAR",
                        description=f"Variable {field_name} for {record_type}",
                        files_using=[str(f) for f in files]
                    )
                    macros.append(macro)

        return macros

    def _create_substitution_mappings(self, files: List[Path],
                                     macros: List[MacroMapping]) -> Dict[str, Dict[str, str]]:
        """Create macro substitution mappings for each file"""
        mappings = {}

        for filepath in files:
            file_mappings = {}
            file_str = str(filepath)

            # Extract identifying parts from filename
            filename = Path(filepath).stem
            parts = re.findall(r'(c[0-9]+|[0-9]+k|cb|kb)', filename)

            # Map macros based on file-specific values
            for macro in macros:
                # Determine value for this file
                if 'c1' in filename:
                    file_mappings[macro.macro_name] = macro.original_value.replace('c2', 'c1')
                elif 'c2' in filename:
                    file_mappings[macro.macro_name] = macro.original_value.replace('c1', 'c2')
                else:
                    file_mappings[macro.macro_name] = macro.original_value

            # Add standard macros
            file_mappings['SYSTEM'] = filename.split('_')[0] if '_' in filename else 'system'

            mappings[file_str] = file_mappings

        return mappings

    def _calculate_savings(self, files: List[Path], common_records: List[Dict],
                          variable_records: List[Dict]) -> Dict[str, Any]:
        """Calculate benefits of consolidation"""
        total_lines = sum(len(self.file_contents[str(f)].splitlines()) for f in files)
        template_lines = len(common_records) * 10  # Estimate lines per record

        return {
            'files_consolidated': len(files),
            'original_total_lines': total_lines,
            'template_lines': template_lines,
            'line_reduction': total_lines - template_lines,
            'reduction_percentage': ((total_lines - template_lines) / total_lines * 100) if total_lines > 0 else 0,
            'common_records': len(common_records),
            'variable_records': len(variable_records),
            'maintenance_factor': len(files)  # How many places need updates currently
        }

    def _suggest_template_name(self, files: List[Path]) -> str:
        """Suggest a name for the consolidated template"""
        names = [Path(f).stem for f in files]

        # Find common prefix
        common_prefix = os.path.commonprefix(names)
        if common_prefix:
            # Clean up prefix
            common_prefix = common_prefix.rstrip('_0123456789')
            return f"{common_prefix}_template.vdb"

        # Find common patterns
        patterns = []
        for name in names:
            pattern = re.sub(r'c[0-9]+_', '', name)  # Remove system prefix
            pattern = re.sub(r'[0-9]+', '', pattern)  # Remove numbers
            patterns.append(pattern)

        # Most common pattern
        if patterns:
            most_common = Counter(patterns).most_common(1)[0][0]
            return f"{most_common}_template.vdb"

        return "consolidated_template.vdb"

    def generate_template(self, plan: ConsolidationPlan) -> str:
        """Generate the consolidated template file"""
        template = []

        # Header
        template.append("#" + "=" * 79)
        template.append(f"# Consolidated Template: {plan.target_name}")
        template.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        template.append(f"# Source files: {len(plan.source_files)}")
        for source in plan.source_files[:5]:
            template.append(f"#   - {Path(source).name}")
        if len(plan.source_files) > 5:
            template.append(f"#   ... and {len(plan.source_files) - 5} more")
        template.append("#" + "=" * 79)
        template.append("")

        # Macro documentation
        if plan.new_macros:
            template.append("# Required Macros:")
            for macro in plan.new_macros:
                template.append(f"# {macro.macro_name:<20} - {macro.description}")
            template.append("")

        # Generate records
        for record in plan.common_records:
            template.append(f'record({record["type"]}, "{record["name"]}") {{')
            for field_name, field_value in record['fields'].items():
                # Apply macro substitutions where needed
                for macro in plan.new_macros:
                    if macro.original_value in field_value:
                        field_value = field_value.replace(macro.original_value, f"$({macro.macro_name})")

                template.append(f'  field({field_name}, "{field_value}")')
            template.append("}")
            template.append("")

        return '\n'.join(template)

    def generate_substitution_file(self, plan: ConsolidationPlan) -> str:
        """Generate substitution file for the consolidated template"""
        sub_file = []

        # Header
        sub_file.append("#" + "=" * 79)
        sub_file.append(f"# Substitution file for: {plan.target_name}")
        sub_file.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        sub_file.append("#" + "=" * 79)
        sub_file.append("")

        # File reference
        sub_file.append(f"file {plan.target_name}")
        sub_file.append("{")

        # Pattern line
        macro_names = sorted(set(m.macro_name for m in plan.new_macros))
        if macro_names:
            pattern_line = "    pattern { " + ", ".join(macro_names) + " }"
            sub_file.append(pattern_line)

            # Data lines for each original file
            for source_file, mappings in plan.substitution_mappings.items():
                values = []
                for macro in macro_names:
                    values.append(mappings.get(macro, "UNDEFINED"))
                data_line = "            { " + ", ".join(values) + " }"
                sub_file.append(data_line)

        sub_file.append("}")
        sub_file.append("")

        return '\n'.join(sub_file)

    def generate_migration_script(self, plan: ConsolidationPlan) -> str:
        """Generate a migration script for the consolidation"""
        script = []

        script.append("#!/bin/bash")
        script.append("#" + "=" * 79)
        script.append(f"# Migration script for consolidating to {plan.target_name}")
        script.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        script.append("#" + "=" * 79)
        script.append("")

        script.append("# This script will help migrate from individual .db files to")
        script.append("# a consolidated template with substitutions")
        script.append("")

        script.append("echo 'Starting template consolidation migration...'")
        script.append("")

        # Backup commands
        script.append("# Step 1: Create backups")
        script.append("echo 'Creating backups...'")
        for source in plan.source_files:
            filename = Path(source).name
            script.append(f"cp {filename} {filename}.backup 2>/dev/null || echo 'Warning: {filename} not found'")
        script.append("")

        # Create new template
        script.append("# Step 2: Create consolidated template")
        script.append(f"echo 'Creating {plan.target_name}...'")
        script.append(f"cat > {plan.target_name} << 'EOF'")
        script.append("# [Template content would be inserted here]")
        script.append("EOF")
        script.append("")

        # Create substitution file
        script.append("# Step 3: Create substitution file")
        sub_filename = plan.target_name.replace('.vdb', '.substitutions')
        script.append(f"echo 'Creating {sub_filename}...'")
        script.append(f"cat > {sub_filename} << 'EOF'")
        script.append("# [Substitution content would be inserted here]")
        script.append("EOF")
        script.append("")

        # Update references
        script.append("# Step 4: Update references in startup scripts")
        script.append("echo 'Updating references...'")
        script.append("# TODO: Update dbLoadRecords calls in st.cmd files")
        script.append("# Example:")
        for source in plan.source_files[:2]:
            filename = Path(source).name
            script.append(f"# Replace: dbLoadRecords(\"db/{filename}\")")
        script.append(f"# With: dbLoadTemplate(\"db/{sub_filename}\")")
        script.append("")

        # Validation
        script.append("# Step 5: Validate migration")
        script.append("echo 'Validating migration...'")
        script.append("# TODO: Add validation commands here")
        script.append("")

        script.append("echo 'Migration complete!'")
        script.append("echo 'Please review the changes and test before committing.'")

        return '\n'.join(script)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='EPICS Template Consolidator - Consolidate similar database files into templates',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze files for consolidation potential
  %(prog)s analyze file1.db file2.db file3.db

  # Generate consolidated template
  %(prog)s generate file1.db file2.db file3.db --output template.vdb

  # Create full consolidation package
  %(prog)s package file1.db file2.db file3.db --output-dir ./consolidated

  # Interactive consolidation wizard
  %(prog)s wizard /path/to/db/directory
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Analyze command
    analyze_parser = subparsers.add_parser('analyze',
                                          help='Analyze consolidation potential')
    analyze_parser.add_argument('files', nargs='+', help='Files to consolidate')

    # Generate command
    generate_parser = subparsers.add_parser('generate',
                                           help='Generate consolidated template')
    generate_parser.add_argument('files', nargs='+', help='Files to consolidate')
    generate_parser.add_argument('-o', '--output', help='Output template file')
    generate_parser.add_argument('--substitutions', action='store_true',
                                help='Also generate substitution file')

    # Package command
    package_parser = subparsers.add_parser('package',
                                          help='Create full consolidation package')
    package_parser.add_argument('files', nargs='+', help='Files to consolidate')
    package_parser.add_argument('--output-dir', default='./consolidated',
                               help='Output directory')

    # Wizard command
    wizard_parser = subparsers.add_parser('wizard',
                                         help='Interactive consolidation wizard')
    wizard_parser.add_argument('directory', help='Directory to analyze')

    # Add verbose flag to all subparsers
    for subparser in [analyze_parser, generate_parser, package_parser, wizard_parser]:
        subparser.add_argument('-v', '--verbose', action='store_true',
                             help='Verbose output')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    consolidator = TemplateConsolidator(verbose=args.verbose if hasattr(args, 'verbose') else False)

    if args.command == 'analyze':
        files = [Path(f) for f in args.files]
        plan = consolidator.analyze_files(files)

        print(f"Consolidation Analysis for {plan.target_name}")
        print("=" * 60)
        print(f"Source files: {len(plan.source_files)}")
        for source in plan.source_files:
            print(f"  - {Path(source).name}")
        print(f"\nCommon records: {len(plan.common_records)}")
        print(f"Variable records: {len(plan.variable_records)}")
        print(f"New macros needed: {len(plan.new_macros)}")

        if plan.new_macros:
            print("\nProposed macros:")
            for macro in plan.new_macros[:5]:
                print(f"  ${macro.macro_name}: {macro.description}")

        print(f"\nSavings:")
        savings = plan.savings
        print(f"  Line reduction: {savings['line_reduction']} ({savings['reduction_percentage']:.1f}%)")
        print(f"  Maintenance points: {savings['maintenance_factor']} -> 1")

    elif args.command == 'generate':
        files = [Path(f) for f in args.files]
        plan = consolidator.analyze_files(files)

        # Generate template
        template_content = consolidator.generate_template(plan)

        output_file = args.output or plan.target_name
        with open(output_file, 'w') as f:
            f.write(template_content)
        print(f"Generated template: {output_file}")

        if args.substitutions:
            # Generate substitution file
            sub_content = consolidator.generate_substitution_file(plan)
            sub_file = output_file.replace('.vdb', '.substitutions').replace('.db', '.substitutions')
            with open(sub_file, 'w') as f:
                f.write(sub_content)
            print(f"Generated substitutions: {sub_file}")

    elif args.command == 'package':
        files = [Path(f) for f in args.files]
        plan = consolidator.analyze_files(files)

        # Create output directory
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate all files
        template_file = output_dir / plan.target_name
        with open(template_file, 'w') as f:
            f.write(consolidator.generate_template(plan))

        sub_file = output_dir / plan.target_name.replace('.vdb', '.substitutions')
        with open(sub_file, 'w') as f:
            f.write(consolidator.generate_substitution_file(plan))

        migration_file = output_dir / 'migrate.sh'
        with open(migration_file, 'w') as f:
            f.write(consolidator.generate_migration_script(plan))

        # Save plan as JSON
        plan_file = output_dir / 'consolidation_plan.json'
        plan_dict = {
            'target_name': plan.target_name,
            'source_files': plan.source_files,
            'common_records_count': len(plan.common_records),
            'variable_records_count': len(plan.variable_records),
            'new_macros': [{'name': m.macro_name, 'description': m.description}
                          for m in plan.new_macros],
            'savings': plan.savings
        }
        with open(plan_file, 'w') as f:
            json.dump(plan_dict, f, indent=2)

        print(f"Consolidation package created in {output_dir}")
        print(f"  - Template: {template_file.name}")
        print(f"  - Substitutions: {sub_file.name}")
        print(f"  - Migration script: {migration_file.name}")
        print(f"  - Plan details: {plan_file.name}")

    elif args.command == 'wizard':
        # Interactive wizard for consolidation
        directory = Path(args.directory)

        print("EPICS Template Consolidation Wizard")
        print("=" * 40)
        print(f"Analyzing directory: {directory}")
        print()

        # Find all .db files
        db_files = list(directory.glob('*.db'))
        print(f"Found {len(db_files)} .db files")

        # Group similar files by name pattern
        groups = defaultdict(list)
        for filepath in db_files:
            # Extract base pattern
            name = filepath.stem
            pattern = re.sub(r'c[0-9]+_', '', name)  # Remove system prefix
            pattern = re.sub(r'[0-9]+', 'X', pattern)  # Replace numbers with X
            groups[pattern].append(filepath)

        # Show consolidation opportunities
        print("\nConsolidation opportunities:")
        consolidation_groups = []
        for i, (pattern, files) in enumerate(groups.items(), 1):
            if len(files) > 1:
                print(f"\n{i}. Pattern: {pattern}")
                print(f"   Files: {len(files)}")
                for f in files[:3]:
                    print(f"     - {f.name}")
                if len(files) > 3:
                    print(f"     ... and {len(files) - 3} more")
                consolidation_groups.append(files)

        if not consolidation_groups:
            print("No consolidation opportunities found.")
            return

        # Let user select
        print("\nEnter group number to consolidate (or 'q' to quit): ", end='')
        choice = input().strip()

        if choice.lower() == 'q':
            return

        try:
            group_index = int(choice) - 1
            if 0 <= group_index < len(consolidation_groups):
                selected_files = consolidation_groups[group_index]

                print(f"\nConsolidating {len(selected_files)} files...")
                plan = consolidator.analyze_files(selected_files)

                # Create output directory
                output_dir = directory / 'consolidated' / datetime.now().strftime('%Y%m%d_%H%M%S')
                output_dir.mkdir(parents=True, exist_ok=True)

                # Generate files
                template_file = output_dir / plan.target_name
                with open(template_file, 'w') as f:
                    f.write(consolidator.generate_template(plan))

                print(f"\nConsolidation complete!")
                print(f"Files saved to: {output_dir}")
                print(f"Template: {template_file.name}")
                print(f"Reduction: {plan.savings['reduction_percentage']:.1f}%")
            else:
                print("Invalid selection")
        except ValueError:
            print("Invalid input")


if __name__ == '__main__':
    main()