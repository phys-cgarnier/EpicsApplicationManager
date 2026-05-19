#!/usr/bin/env python3
"""
EPICS IOC Master Management Tool
=================================
Unified tool for managing all aspects of EPICS IOCs including:
- Substitution files (.substitutions)
- Template files (.db, .vdb, .template)
- Archive files (.archive, .tpl-arch)
- Makefiles
- Cross-validation and auditing

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
import json
from datetime import datetime
import subprocess


@dataclass
class IOCComponent:
    """Represents a component of the IOC system"""
    name: str
    path: Path
    component_type: str  # 'substitution', 'template', 'archive', 'makefile'
    content: str = ""
    dependencies: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)


@dataclass
class IOCSystem:
    """Represents complete IOC system"""
    name: str
    base_path: Path
    db_files: List[IOCComponent] = field(default_factory=list)
    substitution_files: List[IOCComponent] = field(default_factory=list)
    archive_files: List[IOCComponent] = field(default_factory=list)
    makefiles: List[IOCComponent] = field(default_factory=list)
    validation_results: Dict[str, Any] = field(default_factory=dict)


class IOCMasterManager:
    """Master manager for all IOC components"""

    def __init__(self, base_path: Path, verbose: bool = False):
        self.base_path = base_path
        self.verbose = verbose
        self.systems: Dict[str, IOCSystem] = {}
        self.issues: List[str] = []

    def log(self, message: str):
        """Print message if verbose mode enabled"""
        if self.verbose:
            print(f"[INFO] {message}")

    def scan_directory(self) -> Dict[str, IOCSystem]:
        """Scan directory structure and identify all IOC systems"""
        self.log(f"Scanning {self.base_path}")

        # Scan for all IOC directories in Db/
        db_base = self.base_path / 'Db'

        # First, scan all actual directories in Db/
        if db_base.exists():
            for ioc_dir in db_base.iterdir():
                if ioc_dir.is_dir():
                    pattern = ioc_dir.name
                    system = IOCSystem(name=pattern, base_path=self.base_path)

                    # Find DB files
                    db_path = ioc_dir
                    for db_file in db_path.glob('*.db'):
                        component = IOCComponent(
                            name=db_file.stem,
                            path=db_file,
                            component_type='template'
                        )
                        system.db_files.append(component)
                        self.log(f"Found DB: {db_file.name}")

                    # Find vdb files
                    for vdb_file in db_path.glob('*.vdb'):
                        component = IOCComponent(
                            name=vdb_file.stem,
                            path=vdb_file,
                            component_type='template'
                        )
                        system.db_files.append(component)
                        self.log(f"Found VDB: {vdb_file.name}")

                    # Find substitution files
                    for sub_file in db_path.glob('*.substitutions'):
                        component = IOCComponent(
                            name=sub_file.stem,
                            path=sub_file,
                            component_type='substitution'
                        )
                        system.substitution_files.append(component)
                        self.log(f"Found substitution: {sub_file.name}")

                    # Find archive files
                    archive_path = self.base_path / 'srcArchive' / pattern
                    if archive_path.exists():
                        for archive_file in archive_path.glob('*.archive'):
                            component = IOCComponent(
                                name=archive_file.stem,
                                path=archive_file,
                                component_type='archive'
                            )
                            system.archive_files.append(component)
                            self.log(f"Found archive: {archive_file.name}")

                        for tpl_file in archive_path.glob('*.tpl-arch'):
                            component = IOCComponent(
                                name=tpl_file.stem,
                                path=tpl_file,
                                component_type='archive'
                            )
                            system.archive_files.append(component)
                            self.log(f"Found template archive: {tpl_file.name}")

                        # Find Makefile
                        makefile = archive_path / 'Makefile'
                        if makefile.exists():
                            component = IOCComponent(
                                name='Makefile',
                                path=makefile,
                                component_type='makefile'
                            )
                            system.makefiles.append(component)
                            self.log(f"Found Makefile for {pattern}")

                    if any([system.db_files, system.substitution_files,
                           system.archive_files, system.makefiles]):
                        self.systems[pattern] = system

        # Also scan top-level files in Db/ (not in subdirectories)
        if db_base.exists():
            loose_db_files = []
            loose_sub_files = []

            for item in db_base.glob('*.db'):
                if item.is_file():
                    loose_db_files.append(item)

            for item in db_base.glob('*.vdb'):
                if item.is_file():
                    loose_db_files.append(item)

            for item in db_base.glob('*.substitutions'):
                if item.is_file():
                    loose_sub_files.append(item)

            if loose_db_files or loose_sub_files:
                system = IOCSystem(name='_root_db_files', base_path=self.base_path)

                for db_file in loose_db_files:
                    component = IOCComponent(
                        name=db_file.stem,
                        path=db_file,
                        component_type='template'
                    )
                    system.db_files.append(component)
                    self.log(f"Found loose DB: {db_file.name}")

                for sub_file in loose_sub_files:
                    component = IOCComponent(
                        name=sub_file.stem,
                        path=sub_file,
                        component_type='substitution'
                    )
                    system.substitution_files.append(component)
                    self.log(f"Found loose substitution: {sub_file.name}")

                self.systems['_root_db_files'] = system

        return self.systems

    def validate_makefile(self, makefile_path: Path) -> Dict[str, Any]:
        """Validate and analyze a Makefile"""
        results = {
            'valid': True,
            'archives': [],
            'missing_archives': [],
            'issues': []
        }

        try:
            with open(makefile_path, 'r') as f:
                content = f.read()

            # Extract ARCHIVE definitions
            archive_pattern = re.compile(r'ARCHIVE\s*\+=\s*(.+)')
            for match in archive_pattern.finditer(content):
                archives = match.group(1).strip().split()
                results['archives'].extend(archives)

            # Check if referenced archives exist
            archive_dir = makefile_path.parent
            for archive_name in results['archives']:
                archive_path = archive_dir / archive_name
                if not archive_path.exists():
                    results['missing_archives'].append(archive_name)
                    results['issues'].append(f"Missing archive: {archive_name}")
                    results['valid'] = False

            # Check for common Makefile issues
            if 'include $(TOP)/configure/CONFIG' not in content:
                results['issues'].append("Missing CONFIG include")
                results['valid'] = False

            if 'include $(TOP)/configure/RULES' not in content:
                results['issues'].append("Missing RULES include")
                results['valid'] = False

        except Exception as e:
            results['valid'] = False
            results['issues'].append(f"Error reading Makefile: {e}")

        return results

    def audit_archiver(self, system: IOCSystem) -> Dict[str, Any]:
        """Audit archiver configuration against substitution files"""
        results = {
            'coverage': {},
            'missing_pvs': [],
            'extra_pvs': [],
            'issues': []
        }

        # Extract PVs from substitution files
        sub_pvs = set()
        for sub_file in system.substitution_files:
            pvs = self._extract_pvs_from_substitution(sub_file.path)
            sub_pvs.update(pvs)

        # Extract PVs from archive files
        arch_pvs = set()
        for arch_file in system.archive_files:
            if arch_file.path.suffix in ['.archive', '.tpl-arch']:
                pvs = self._extract_pvs_from_archive(arch_file.path)
                arch_pvs.update(pvs)

        # Compare
        results['missing_pvs'] = list(sub_pvs - arch_pvs)
        results['extra_pvs'] = list(arch_pvs - sub_pvs)

        if sub_pvs:
            results['coverage']['percentage'] = len(arch_pvs & sub_pvs) / len(sub_pvs) * 100
        else:
            results['coverage']['percentage'] = 0

        results['coverage']['substitution_pvs'] = len(sub_pvs)
        results['coverage']['archived_pvs'] = len(arch_pvs)
        results['coverage']['common_pvs'] = len(arch_pvs & sub_pvs)

        # Flag issues
        if results['coverage']['percentage'] < 80:
            results['issues'].append(f"Low archive coverage: {results['coverage']['percentage']:.1f}%")

        if len(results['missing_pvs']) > 10:
            results['issues'].append(f"{len(results['missing_pvs'])} PVs not archived")

        return results

    def _extract_pvs_from_substitution(self, sub_path: Path) -> Set[str]:
        """Extract PV names from substitution file"""
        pvs = set()
        try:
            with open(sub_path, 'r') as f:
                content = f.read()

            # Parse substitution patterns
            pattern = re.compile(r'\{([^}]+)\}')
            for match in pattern.finditer(content):
                fields = match.group(1).split(',')
                # Typically PV name is in one of the first few fields
                for field in fields[:5]:
                    field = field.strip()
                    if field and not field.startswith('$('):
                        # Clean up the PV name
                        pv = re.sub(r'"', '', field)
                        if ':' in pv:  # Likely a PV name
                            pvs.add(pv)
        except Exception as e:
            self.log(f"Error parsing {sub_path}: {e}")

        return pvs

    def _extract_pvs_from_archive(self, arch_path: Path) -> Set[str]:
        """Extract PV names from archive file"""
        pvs = set()
        try:
            with open(arch_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Archive files typically have PV names directly
                        parts = line.split()
                        if parts:
                            pv = parts[0]
                            if ':' in pv:  # Likely a PV name
                                pvs.add(pv)
        except Exception as e:
            self.log(f"Error parsing {arch_path}: {e}")

        return pvs

    def cross_validate(self, system: IOCSystem) -> Dict[str, Any]:
        """Cross-validate all components of an IOC system"""
        results = {
            'system': system.name,
            'valid': True,
            'issues': [],
            'statistics': {}
        }

        # Validate Makefiles
        for makefile in system.makefiles:
            make_results = self.validate_makefile(makefile.path)
            if not make_results['valid']:
                results['valid'] = False
                results['issues'].extend(make_results['issues'])

        # Audit archiver coverage
        arch_results = self.audit_archiver(system)
        results['archiver_audit'] = arch_results
        if arch_results['issues']:
            results['issues'].extend(arch_results['issues'])

        # Check for orphaned files
        db_names = {f.name for f in system.db_files}
        sub_names = {f.name for f in system.substitution_files}
        arch_names = {f.name for f in system.archive_files}

        # Files referenced in substitutions but not present
        for sub_file in system.substitution_files:
            referenced = self._get_referenced_db_files(sub_file.path)
            for ref in referenced:
                if ref not in db_names:
                    results['issues'].append(f"Missing DB file: {ref} (referenced in {sub_file.name})")

        # Statistics
        results['statistics'] = {
            'db_files': len(system.db_files),
            'substitution_files': len(system.substitution_files),
            'archive_files': len(system.archive_files),
            'makefiles': len(system.makefiles),
            'total_issues': len(results['issues'])
        }

        return results

    def _get_referenced_db_files(self, sub_path: Path) -> Set[str]:
        """Get DB files referenced in a substitution file"""
        referenced = set()
        try:
            with open(sub_path, 'r') as f:
                content = f.read()

            # Find file references
            file_pattern = re.compile(r'^file\s+([A-Za-z0-9_\-\.]+)', re.MULTILINE)
            for match in file_pattern.finditer(content):
                referenced.add(match.group(1))
        except Exception as e:
            self.log(f"Error parsing {sub_path}: {e}")

        return referenced

    def generate_dependency_graph(self, system: IOCSystem) -> Dict[str, List[str]]:
        """Generate dependency graph for IOC system"""
        graph = defaultdict(list)

        # DB to substitution dependencies
        for sub_file in system.substitution_files:
            referenced = self._get_referenced_db_files(sub_file.path)
            for ref in referenced:
                graph[ref].append(sub_file.name)

        # Makefile to archive dependencies
        for makefile in system.makefiles:
            make_results = self.validate_makefile(makefile.path)
            for archive in make_results['archives']:
                graph[makefile.name].append(archive)

        return dict(graph)

    def fix_archive_coverage(self, system: IOCSystem, output_dir: Path) -> Dict[str, Any]:
        """Generate updated archive files with missing PVs"""
        results = {
            'files_created': [],
            'pvs_added': 0
        }

        audit = self.audit_archiver(system)

        if audit['missing_pvs']:
            # Group missing PVs by pattern
            pv_groups = defaultdict(list)
            for pv in audit['missing_pvs']:
                # Extract base pattern from PV name
                base = pv.split(':')[0] if ':' in pv else 'misc'
                pv_groups[base].append(pv)

            # Create new archive files
            output_dir.mkdir(parents=True, exist_ok=True)

            for group_name, pvs in pv_groups.items():
                archive_file = output_dir / f"{system.name}_{group_name}_missing.archive"

                with open(archive_file, 'w') as f:
                    f.write(f"# Missing PVs for {system.name} - {group_name}\n")
                    f.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"# Total PVs: {len(pvs)}\n\n")

                    for pv in sorted(pvs):
                        f.write(f"{pv} 1 Monitor\n")

                results['files_created'].append(str(archive_file))
                results['pvs_added'] += len(pvs)

        return results

    def generate_report(self) -> str:
        """Generate comprehensive IOC system report"""
        report = []
        report.append("=" * 80)
        report.append("EPICS IOC System Analysis Report")
        report.append("=" * 80)
        report.append(f"Base path: {self.base_path}")
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        # System summary
        report.append(f"SYSTEMS FOUND: {len(self.systems)}")
        for name, system in sorted(self.systems.items()):
            report.append(f"  {name}:")
            report.append(f"    DB files: {len(system.db_files)}")
            report.append(f"    Substitutions: {len(system.substitution_files)}")
            report.append(f"    Archives: {len(system.archive_files)}")
            report.append(f"    Makefiles: {len(system.makefiles)}")
        report.append("")

        # Validation results
        total_issues = 0
        for name, system in sorted(self.systems.items()):
            validation = self.cross_validate(system)
            if validation['issues']:
                report.append(f"{name} VALIDATION ISSUES:")
                for issue in validation['issues'][:5]:
                    report.append(f"  - {issue}")
                if len(validation['issues']) > 5:
                    report.append(f"  ... and {len(validation['issues']) - 5} more")
                report.append("")
                total_issues += len(validation['issues'])

            # Archiver coverage
            if 'archiver_audit' in validation:
                audit = validation['archiver_audit']
                report.append(f"{name} ARCHIVER COVERAGE:")
                report.append(f"  Coverage: {audit['coverage']['percentage']:.1f}%")
                report.append(f"  Substitution PVs: {audit['coverage']['substitution_pvs']}")
                report.append(f"  Archived PVs: {audit['coverage']['archived_pvs']}")
                if audit['missing_pvs']:
                    report.append(f"  Missing PVs: {len(audit['missing_pvs'])}")
                report.append("")

        # Summary
        report.append("SUMMARY:")
        report.append(f"  Total systems: {len(self.systems)}")
        report.append(f"  Total issues: {total_issues}")

        # Calculate totals
        total_db = sum(len(s.db_files) for s in self.systems.values())
        total_sub = sum(len(s.substitution_files) for s in self.systems.values())
        total_arch = sum(len(s.archive_files) for s in self.systems.values())

        report.append(f"  Total DB files: {total_db}")
        report.append(f"  Total substitution files: {total_sub}")
        report.append(f"  Total archive files: {total_arch}")

        report.append("")
        report.append("=" * 80)

        return '\n'.join(report)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='EPICS IOC Master Management Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan and analyze entire IOC system
  %(prog)s scan /path/to/CryoplantApp

  # Validate specific system
  %(prog)s validate /path/to/CryoplantApp --system c1_2kcb

  # Audit archiver coverage
  %(prog)s audit-archiver /path/to/CryoplantApp --system c1_2kcb

  # Fix missing archive entries
  %(prog)s fix-archives /path/to/CryoplantApp --system c1_2kcb --output ./fixed

  # Generate full system report
  %(prog)s report /path/to/CryoplantApp --output report.txt

  # Check Makefile validity
  %(prog)s check-makefile /path/to/srcArchive/c1_2kcb/Makefile

  # Generate dependency graph
  %(prog)s dependencies /path/to/CryoplantApp --system c1_2kcb
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Scan IOC system')
    scan_parser.add_argument('path', help='Base path to IOC system')

    # Validate command
    validate_parser = subparsers.add_parser('validate', help='Validate IOC system')
    validate_parser.add_argument('path', help='Base path to IOC system')
    validate_parser.add_argument('--system', help='Specific system to validate')

    # Audit archiver command
    audit_parser = subparsers.add_parser('audit-archiver', help='Audit archiver coverage')
    audit_parser.add_argument('path', help='Base path to IOC system')
    audit_parser.add_argument('--system', required=True, help='System to audit')

    # Fix archives command
    fix_parser = subparsers.add_parser('fix-archives', help='Fix missing archive entries')
    fix_parser.add_argument('path', help='Base path to IOC system')
    fix_parser.add_argument('--system', required=True, help='System to fix')
    fix_parser.add_argument('--output', default='./fixed_archives', help='Output directory')

    # Report command
    report_parser = subparsers.add_parser('report', help='Generate full report')
    report_parser.add_argument('path', help='Base path to IOC system')
    report_parser.add_argument('--output', help='Output file')

    # Check makefile command
    makefile_parser = subparsers.add_parser('check-makefile', help='Check Makefile validity')
    makefile_parser.add_argument('makefile', help='Path to Makefile')

    # Dependencies command
    deps_parser = subparsers.add_parser('dependencies', help='Generate dependency graph')
    deps_parser.add_argument('path', help='Base path to IOC system')
    deps_parser.add_argument('--system', help='Specific system')
    deps_parser.add_argument('--format', choices=['text', 'json', 'dot'], default='text',
                            help='Output format')

    # Add verbose flag to all
    for subparser in [scan_parser, validate_parser, audit_parser, fix_parser,
                     report_parser, makefile_parser, deps_parser]:
        subparser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'scan':
        path = Path(args.path)
        manager = IOCMasterManager(path, verbose=args.verbose)
        systems = manager.scan_directory()

        print(f"Found {len(systems)} IOC systems:")
        for name, system in sorted(systems.items()):
            print(f"\n{name}:")
            print(f"  DB files: {len(system.db_files)}")
            print(f"  Substitutions: {len(system.substitution_files)}")
            print(f"  Archives: {len(system.archive_files)}")
            print(f"  Makefiles: {len(system.makefiles)}")

    elif args.command == 'validate':
        path = Path(args.path)
        manager = IOCMasterManager(path, verbose=args.verbose)
        manager.scan_directory()

        if args.system:
            if args.system not in manager.systems:
                print(f"System {args.system} not found")
                sys.exit(1)
            systems = {args.system: manager.systems[args.system]}
        else:
            systems = manager.systems

        for name, system in sorted(systems.items()):
            results = manager.cross_validate(system)
            print(f"\n{name} Validation:")
            print(f"  Valid: {results['valid']}")
            if results['issues']:
                print(f"  Issues ({len(results['issues'])}):")
                for issue in results['issues'][:5]:
                    print(f"    - {issue}")
                if len(results['issues']) > 5:
                    print(f"    ... and {len(results['issues']) - 5} more")

    elif args.command == 'audit-archiver':
        path = Path(args.path)
        manager = IOCMasterManager(path, verbose=args.verbose)
        manager.scan_directory()

        if args.system not in manager.systems:
            print(f"System {args.system} not found")
            sys.exit(1)

        system = manager.systems[args.system]
        results = manager.audit_archiver(system)

        print(f"Archiver Audit for {args.system}:")
        print(f"  Coverage: {results['coverage']['percentage']:.1f}%")
        print(f"  Substitution PVs: {results['coverage']['substitution_pvs']}")
        print(f"  Archived PVs: {results['coverage']['archived_pvs']}")
        print(f"  Common PVs: {results['coverage']['common_pvs']}")

        if results['missing_pvs']:
            print(f"\n  Missing PVs ({len(results['missing_pvs'])}):")
            for pv in results['missing_pvs'][:10]:
                print(f"    - {pv}")
            if len(results['missing_pvs']) > 10:
                print(f"    ... and {len(results['missing_pvs']) - 10} more")

        if results['extra_pvs']:
            print(f"\n  Extra PVs in archives ({len(results['extra_pvs'])}):")
            for pv in results['extra_pvs'][:10]:
                print(f"    - {pv}")

    elif args.command == 'fix-archives':
        path = Path(args.path)
        manager = IOCMasterManager(path, verbose=args.verbose)
        manager.scan_directory()

        if args.system not in manager.systems:
            print(f"System {args.system} not found")
            sys.exit(1)

        system = manager.systems[args.system]
        output_dir = Path(args.output)
        results = manager.fix_archive_coverage(system, output_dir)

        if results['files_created']:
            print(f"Created {len(results['files_created'])} archive files:")
            for file in results['files_created']:
                print(f"  - {file}")
            print(f"Total PVs added: {results['pvs_added']}")
        else:
            print("No missing PVs found - archive coverage is complete")

    elif args.command == 'report':
        path = Path(args.path)
        manager = IOCMasterManager(path, verbose=args.verbose)
        manager.scan_directory()

        report = manager.generate_report()

        if args.output:
            with open(args.output, 'w') as f:
                f.write(report)
            print(f"Report saved to {args.output}")
        else:
            print(report)

    elif args.command == 'check-makefile':
        makefile = Path(args.makefile)
        if not makefile.exists():
            print(f"Makefile not found: {makefile}")
            sys.exit(1)

        manager = IOCMasterManager(makefile.parent.parent, verbose=args.verbose)
        results = manager.validate_makefile(makefile)

        print(f"Makefile Validation: {makefile}")
        print(f"  Valid: {results['valid']}")
        print(f"  Archives defined: {len(results['archives'])}")

        if results['archives']:
            print(f"  Archive files:")
            for archive in results['archives']:
                print(f"    - {archive}")

        if results['missing_archives']:
            print(f"  Missing archives:")
            for archive in results['missing_archives']:
                print(f"    - {archive}")

        if results['issues']:
            print(f"  Issues:")
            for issue in results['issues']:
                print(f"    - {issue}")

    elif args.command == 'dependencies':
        path = Path(args.path)
        manager = IOCMasterManager(path, verbose=args.verbose)
        manager.scan_directory()

        if args.system:
            if args.system not in manager.systems:
                print(f"System {args.system} not found")
                sys.exit(1)
            systems = {args.system: manager.systems[args.system]}
        else:
            systems = manager.systems

        for name, system in sorted(systems.items()):
            graph = manager.generate_dependency_graph(system)

            if args.format == 'json':
                print(json.dumps({name: graph}, indent=2))
            elif args.format == 'dot':
                print(f"digraph {name} {{")
                for source, targets in graph.items():
                    for target in targets:
                        print(f'  "{source}" -> "{target}";')
                print("}")
            else:
                print(f"\nDependencies for {name}:")
                for source, targets in sorted(graph.items()):
                    print(f"  {source}:")
                    for target in targets:
                        print(f"    -> {target}")


if __name__ == '__main__':
    main()