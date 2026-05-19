#!/usr/bin/env python3
"""
IOC Archive Management System
=============================
Intelligent archive synchronization with PV extraction, coverage analysis,
and optimized sampling rate generation.

Author: SLAC Cryoplant Team
Date: 2024
"""

import re
import os
import json
from pathlib import Path
from typing import List, Dict, Set, Tuple, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

@dataclass
class PVInfo:
    """Information about a Process Variable"""
    pv_name: str
    source_file: str
    template: str
    pv_type: str  # AI, AO, BI, BO, CALC, etc.
    description: Optional[str] = None
    engineering_units: Optional[str] = None
    expected_range: Optional[Tuple[float, float]] = None
    update_rate: Optional[float] = None
    macros: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'pv_name': self.pv_name,
            'source_file': self.source_file,
            'template': self.template,
            'pv_type': self.pv_type,
            'description': self.description,
            'engineering_units': self.engineering_units,
            'expected_range': self.expected_range,
            'update_rate': self.update_rate,
            'macros': self.macros
        }

@dataclass
class ArchiveEntry:
    """Represents an archive configuration entry"""
    pv_name: str
    sampling_period: int  # seconds
    sampling_method: str  # 'scan' or 'monitor'
    source_file: Optional[str] = None
    line_number: Optional[int] = None

    def to_string(self) -> str:
        """Convert to archive file format"""
        return f"{self.pv_name} {self.sampling_period} {self.sampling_method}"

class SamplingRateOptimizer:
    """Optimizes sampling rates based on PV type and characteristics"""

    # Default sampling rates by PV type (in seconds)
    DEFAULT_RATES = {
        # Analog inputs
        'temperature': 10,    # Temperature sensors - slow changing
        'pressure': 5,        # Pressure sensors - medium speed
        'flow': 1,           # Flow meters - fast changing
        'level': 5,          # Level sensors
        'voltage': 1,        # Voltage measurements
        'current': 1,        # Current measurements
        'power': 5,          # Power measurements

        # Digital inputs
        'valve_state': 1,    # Valve positions - use monitor
        'pump_state': 1,     # Pump status - use monitor
        'alarm': 1,          # Alarms - use monitor
        'interlock': 1,      # Interlocks - use monitor

        # Calculated values
        'calc': 5,           # Calculated values
        'statistics': 60,    # Statistical values
        'setpoint': 1,       # Setpoints - use monitor

        # System values
        'heartbeat': 60,     # Heartbeat/alive signals
        'status': 5,         # Status indicators
        'counter': 1,        # Counters - use monitor
    }

    # Patterns to identify PV types
    TYPE_PATTERNS = {
        'temperature': [r'.*T[TI]\d+', r'.*TEMP.*', r'.*_T$', r'.*:T$'],
        'pressure': [r'.*P[TI]\d+', r'.*PRESS.*', r'.*PDT\d+', r'.*:P$', r'.*:DP$'],
        'flow': [r'.*F[TI]\d+', r'.*FLOW.*', r'.*:FLOW$'],
        'level': [r'.*L[TI]\d+', r'.*LEVEL.*', r'.*:LVL$'],
        'valve_state': [r'.*VLV.*', r'.*VALVE.*', r'.*:STATE$'],
        'pump_state': [r'.*PUMP.*', r'.*:RUN$', r'.*:ON$'],
        'alarm': [r'.*ALARM.*', r'.*_ALM$', r'.*:ALM$'],
        'calc': [r'.*CALC.*', r'.*:AVG$', r'.*:SUM$', r'.*:RATE$'],
        'setpoint': [r'.*_SP$', r'.*:SP$', r'.*SETP.*'],
        'heartbeat': [r'.*HEARTBEAT.*', r'.*:HB$', r'.*ALIVE.*'],
        'status': [r'.*STATUS.*', r'.*:STS$', r'.*:STAT$'],
        'counter': [r'.*CNT\d+', r'.*COUNT.*', r'.*:CNT$']
    }

    @classmethod
    def determine_pv_type(cls, pv_name: str, pv_info: Optional[PVInfo] = None) -> str:
        """Determine the type of a PV from its name and info"""
        pv_upper = pv_name.upper()

        # Check against patterns
        for pv_type, patterns in cls.TYPE_PATTERNS.items():
            for pattern in patterns:
                if re.match(pattern, pv_upper):
                    return pv_type

        # Check description if available
        if pv_info and pv_info.description:
            desc_upper = pv_info.description.upper()
            if 'TEMP' in desc_upper:
                return 'temperature'
            elif 'PRESS' in desc_upper or 'PDT' in desc_upper:
                return 'pressure'
            elif 'FLOW' in desc_upper:
                return 'flow'
            elif 'VALVE' in desc_upper or 'VLV' in desc_upper:
                return 'valve_state'
            elif 'PUMP' in desc_upper:
                return 'pump_state'

        # Default based on record type if available
        if pv_info and pv_info.pv_type:
            if pv_info.pv_type in ['BI', 'BO', 'MBBI', 'MBBO']:
                return 'valve_state'  # Digital - use monitor
            elif pv_info.pv_type in ['AI', 'AO']:
                return 'pressure'  # Analog - medium rate

        return 'status'  # Default type

    @classmethod
    def get_sampling_config(cls, pv_name: str,
                           pv_info: Optional[PVInfo] = None) -> Tuple[int, str]:
        """Get optimal sampling period and method for a PV"""
        pv_type = cls.determine_pv_type(pv_name, pv_info)

        # Get base rate
        rate = cls.DEFAULT_RATES.get(pv_type, 5)

        # Determine method (scan vs monitor)
        # Use 'monitor' for state changes, 'scan' for continuous values
        if pv_type in ['valve_state', 'pump_state', 'alarm', 'interlock',
                      'setpoint', 'counter']:
            method = 'monitor'
            rate = 1  # For monitor mode, use 1 second deadband
        else:
            method = 'scan'

        # Adjust based on additional info
        if pv_info:
            # Fast-changing values need higher rates
            if pv_info.update_rate and pv_info.update_rate > 0:
                if pv_info.update_rate < rate:
                    rate = max(1, int(pv_info.update_rate))

        return rate, method

class ArchiveManager:
    """Manages archive file generation and synchronization"""

    def __init__(self):
        """Initialize archive manager"""
        self.optimizer = SamplingRateOptimizer()
        self.pv_inventory: Dict[str, PVInfo] = {}
        self.archive_entries: Dict[str, List[ArchiveEntry]] = {}

    def extract_pvs_from_substitution(self, file_path: str) -> List[PVInfo]:
        """Extract PV information from a substitution file"""
        pvs = []

        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()

            current_template = None
            current_pattern = []
            file_regex = re.compile(r'^file\s+([A-Za-z0-9_\-\.]+)')
            pattern_regex = re.compile(r'^\s*pattern\s*{(.+)}', re.IGNORECASE)
            data_regex = re.compile(r'^\s*{(.+)}')

            for line in lines:
                line_stripped = line.strip()

                # Track current template
                file_match = file_regex.match(line_stripped)
                if file_match:
                    current_template = file_match.group(1)
                    current_pattern = []
                    continue

                # Track pattern columns
                pattern_match = pattern_regex.match(line_stripped)
                if pattern_match:
                    pattern_str = pattern_match.group(1)
                    current_pattern = [p.strip() for p in
                                     re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)',
                                            pattern_str)]
                    continue

                # Process data rows
                data_match = data_regex.match(line_stripped)
                if data_match and current_pattern and current_template:
                    data_str = data_match.group(1)
                    values = [v.strip().strip('"') for v in
                            re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', data_str)]

                    # Create PV info from the data
                    if len(values) >= len(current_pattern):
                        macros = {}
                        for i, col_name in enumerate(current_pattern):
                            if i < len(values):
                                macros[col_name] = values[i]

                        # Generate PV name based on common patterns
                        pv_name = None
                        description = None
                        egu = None

                        # Look for common PV name patterns
                        if 'PIDTAG' in macros:
                            # Build PV name from PIDTAG and other components
                            pidtag = macros['PIDTAG']
                            area = macros.get('AREA', '')
                            loca = macros.get('LOCA', '')

                            # Determine PV prefix based on template name
                            if 'cpdt' in current_template.lower():
                                prefix = 'CPDT'
                                pv_type = 'pressure'
                            elif 'cft' in current_template.lower() or 'flow' in current_template.lower():
                                prefix = 'CFT'
                                pv_type = 'flow'
                            elif 'ctt' in current_template.lower() or 'temp' in current_template.lower():
                                prefix = 'CTT'
                                pv_type = 'temperature'
                            elif 'valve' in current_template.lower():
                                prefix = 'CVL'
                                pv_type = 'valve_state'
                            else:
                                prefix = pidtag[:3].upper()
                                pv_type = 'status'

                            # Build PV name
                            if area and loca:
                                pv_name = f"{prefix}:{area}:{loca}"

                                # Add suffix based on template
                                if 'diff_pressure' in current_template:
                                    pv_name += ":DP"
                                elif 'flow' in current_template:
                                    pv_name += ":FLOW"
                                elif 'temp' in current_template:
                                    pv_name += ":TEMP"

                        if 'DESC' in macros:
                            description = macros['DESC']
                        elif 'DESCRIPTION' in macros:
                            description = macros['DESCRIPTION']

                        if 'EGU' in macros:
                            egu = macros['EGU']

                        if pv_name:
                            pv_info = PVInfo(
                                pv_name=pv_name,
                                source_file=file_path,
                                template=current_template,
                                pv_type=pv_type if 'pv_type' in locals() else 'status',
                                description=description,
                                engineering_units=egu,
                                macros=macros
                            )
                            pvs.append(pv_info)

                            # Add related PVs (calc parameters for flow, etc.)
                            if 'flow' in current_template.lower():
                                # Flow calculations have additional parameters
                                for suffix in ['PBAR', 'DPMBAR', 'TEMP', 'DENSITY',
                                             'Y', 'K', 'MW', 'BETA4', 'C', 'KS', 'Z']:
                                    related_pv = PVInfo(
                                        pv_name=f"{pv_name.replace(':FLOW', '')}:{suffix}",
                                        source_file=file_path,
                                        template=current_template,
                                        pv_type='calc',
                                        description=f"{description} - {suffix}" if description else suffix,
                                        macros=macros
                                    )
                                    pvs.append(related_pv)

        except Exception as e:
            print(f"Error extracting PVs from {file_path}: {e}")

        return pvs

    def extract_pvs_from_archive(self, file_path: str) -> List[ArchiveEntry]:
        """Extract archive entries from an archive file"""
        entries = []

        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()

            # Pattern for archive entries: PV_NAME PERIOD SCAN_TYPE
            archive_pattern = re.compile(r'^([A-Z][A-Z0-9_:]+)\s+(\d+)\s+(scan|monitor)')

            for i, line in enumerate(lines, 1):
                line_stripped = line.strip()

                # Skip comments and empty lines
                if not line_stripped or line_stripped.startswith('#'):
                    continue

                match = archive_pattern.match(line_stripped)
                if match:
                    pv_name, period, method = match.groups()
                    entry = ArchiveEntry(
                        pv_name=pv_name,
                        sampling_period=int(period),
                        sampling_method=method,
                        source_file=file_path,
                        line_number=i
                    )
                    entries.append(entry)

        except Exception as e:
            print(f"Error extracting archive entries from {file_path}: {e}")

        return entries

    def analyze_coverage(self, substitution_files: List[str],
                        archive_files: List[str]) -> Dict[str, Any]:
        """Analyze PV coverage between substitution and archive files"""
        # Extract PVs from all substitution files
        all_pvs = {}
        for sub_file in substitution_files:
            pvs = self.extract_pvs_from_substitution(sub_file)
            for pv in pvs:
                all_pvs[pv.pv_name] = pv

        # Extract archive entries
        archived_pvs = set()
        archive_entries = []
        for arch_file in archive_files:
            entries = self.extract_pvs_from_archive(arch_file)
            archive_entries.extend(entries)
            for entry in entries:
                archived_pvs.add(entry.pv_name)

        # Calculate coverage
        defined_pvs = set(all_pvs.keys())
        missing_pvs = defined_pvs - archived_pvs
        orphaned_pvs = archived_pvs - defined_pvs

        coverage_percent = 0
        if defined_pvs:
            coverage_percent = (len(archived_pvs & defined_pvs) / len(defined_pvs)) * 100

        return {
            'total_pvs': len(defined_pvs),
            'archived_pvs': len(archived_pvs),
            'missing_pvs': list(missing_pvs),
            'orphaned_pvs': list(orphaned_pvs),
            'coverage_percent': round(coverage_percent, 2),
            'pv_details': all_pvs,
            'archive_entries': archive_entries
        }

    def generate_missing_archive_entries(self, coverage_analysis: Dict) -> List[ArchiveEntry]:
        """Generate archive entries for missing PVs with optimized rates"""
        missing_entries = []

        for pv_name in coverage_analysis['missing_pvs']:
            pv_info = coverage_analysis['pv_details'].get(pv_name)

            # Get optimal sampling configuration
            period, method = self.optimizer.get_sampling_config(pv_name, pv_info)

            entry = ArchiveEntry(
                pv_name=pv_name,
                sampling_period=period,
                sampling_method=method
            )
            missing_entries.append(entry)

        # Sort by PV name for consistency
        missing_entries.sort(key=lambda x: x.pv_name)

        return missing_entries

    def generate_archive_file_content(self, entries: List[ArchiveEntry],
                                     header: Optional[str] = None) -> str:
        """Generate archive file content from entries"""
        lines = []

        # Add header if provided
        if header:
            lines.append(header)
        else:
            lines.append(f"# Archive configuration file")
            lines.append(f"# Generated by IOC Manager - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("#")

        # Group entries by type for better organization
        grouped = {}
        for entry in entries:
            pv_type = self.optimizer.determine_pv_type(entry.pv_name)
            if pv_type not in grouped:
                grouped[pv_type] = []
            grouped[pv_type].append(entry)

        # Write grouped entries
        for pv_type, type_entries in grouped.items():
            if type_entries:
                lines.append(f"#")
                lines.append(f"# {pv_type.replace('_', ' ').title()}")
                lines.append(f"#")

                for entry in type_entries:
                    lines.append(entry.to_string())

        return '\n'.join(lines)

    def optimize_existing_archive(self, archive_file: str) -> Tuple[str, Dict]:
        """Optimize an existing archive file's sampling rates"""
        entries = self.extract_pvs_from_archive(archive_file)
        optimized_entries = []
        changes = {
            'rate_changes': [],
            'method_changes': [],
            'total_optimizations': 0
        }

        for entry in entries:
            # Get optimal configuration
            optimal_period, optimal_method = self.optimizer.get_sampling_config(
                entry.pv_name
            )

            # Check if optimization needed
            changed = False
            if entry.sampling_period != optimal_period:
                changes['rate_changes'].append({
                    'pv': entry.pv_name,
                    'old_rate': entry.sampling_period,
                    'new_rate': optimal_period
                })
                changed = True

            if entry.sampling_method != optimal_method:
                changes['method_changes'].append({
                    'pv': entry.pv_name,
                    'old_method': entry.sampling_method,
                    'new_method': optimal_method
                })
                changed = True

            if changed:
                changes['total_optimizations'] += 1

            # Create optimized entry
            optimized_entry = ArchiveEntry(
                pv_name=entry.pv_name,
                sampling_period=optimal_period,
                sampling_method=optimal_method,
                source_file=entry.source_file
            )
            optimized_entries.append(optimized_entry)

        # Generate optimized content
        optimized_content = self.generate_archive_file_content(optimized_entries)

        return optimized_content, changes

    def estimate_storage_impact(self, entries: List[ArchiveEntry],
                               days: int = 30) -> Dict[str, Any]:
        """Estimate storage requirements for archive entries"""
        # Assumptions:
        # - Each archived value takes ~20 bytes (timestamp + value + metadata)
        # - Monitor mode generates 10% of scan mode data on average

        total_samples_per_day = 0

        for entry in entries:
            if entry.sampling_method == 'scan':
                # Scan mode: samples every N seconds
                samples_per_day = (24 * 3600) / entry.sampling_period
            else:
                # Monitor mode: assume 10% of scan rate due to deadband
                samples_per_day = ((24 * 3600) / entry.sampling_period) * 0.1

            total_samples_per_day += samples_per_day

        bytes_per_day = total_samples_per_day * 20
        bytes_total = bytes_per_day * days

        return {
            'pvs_count': len(entries),
            'samples_per_day': int(total_samples_per_day),
            'bytes_per_day': int(bytes_per_day),
            'mb_per_day': round(bytes_per_day / (1024 * 1024), 2),
            'gb_per_month': round((bytes_per_day * 30) / (1024 * 1024 * 1024), 2),
            'total_storage_mb': round(bytes_total / (1024 * 1024), 2)
        }

# Example usage
if __name__ == "__main__":
    # Create archive manager
    manager = ArchiveManager()

    # Example substitution files
    sub_files = [
        "C:/Users/mkeenan/Development/SLAC/Cryoplant/CryoplantApp/Db/2kcb/2kcb_AIs.substitutions"
    ]

    # Example archive files
    arch_files = [
        "C:/Users/mkeenan/Development/SLAC/Cryoplant/CryoplantApp/srcArchive/2kcb/2kcb_AIs.tpl-arch"
    ]

    # Analyze coverage
    print("Analyzing archive coverage...")
    coverage = manager.analyze_coverage(sub_files, arch_files)

    print(f"\nCoverage Analysis:")
    print(f"  Total PVs defined: {coverage['total_pvs']}")
    print(f"  PVs in archive: {coverage['archived_pvs']}")
    print(f"  Coverage: {coverage['coverage_percent']}%")
    print(f"  Missing PVs: {len(coverage['missing_pvs'])}")
    print(f"  Orphaned PVs: {len(coverage['orphaned_pvs'])}")

    # Generate missing entries
    if coverage['missing_pvs']:
        print(f"\nGenerating archive entries for {len(coverage['missing_pvs'])} missing PVs...")
        missing_entries = manager.generate_missing_archive_entries(coverage)

        # Show first few
        for entry in missing_entries[:5]:
            print(f"  {entry.to_string()}")

        # Estimate storage
        storage = manager.estimate_storage_impact(missing_entries)
        print(f"\nStorage impact for new entries:")
        print(f"  PVs to add: {storage['pvs_count']}")
        print(f"  Samples/day: {storage['samples_per_day']:,}")
        print(f"  Storage/day: {storage['mb_per_day']} MB")
        print(f"  Storage/month: {storage['gb_per_month']} GB")