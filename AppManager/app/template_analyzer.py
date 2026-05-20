#!/usr/bin/env python3
"""
EPICS Template Analyzer
=======================
Analyze and compare EPICS .db, .vdb, and .template files to identify
duplicates, similarities, and consolidation opportunities.

EPICS database files define Process Variables (PVs) using a record-based syntax:
  - .db files: concrete database definitions with hardcoded or macro-substituted values
  - .vdb files: "virtual" database templates meant to be instantiated with macros
  - .template files: reusable templates loaded via substitutions files

This tool parses these files, extracts record structures, and performs
similarity analysis to find opportunities to reduce duplication.

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




# =============================================================================
# DATA CLASSES - In-memory representations of parsed EPICS database structures
# =============================================================================


@dataclass
class EPICSRecord:
    """Represents a single EPICS record (one PV definition).

    In an EPICS .db file, a record looks like:
        record(ai, "$(P):Temperature") {
            field(DESC, "Thermocouple reading")
            field(DTYP, "asynFloat64")
            field(INP,  "@asyn($(PORT),$(ADDR))AI")
            field(SCAN, "1 second")
            info(autosaveFields, "VAL HIHI HIGH LOW LOLO")
        }

    This dataclass stores the parsed components of such a record.
    """

    record_type: str  # e.g. "ai", "bo", "longin", "calc", "mbbi", etc.
    record_name: str  # e.g. "$(P):Temperature" - may contain macros
    fields: Dict[str, str]  # e.g. {"DESC": "Thermocouple reading", "DTYP": "asynFloat64", ...}
    line_number: int  # line number in the source file where this record starts
    raw_content: str  # the full text of the record block from the source file

    def get_normalized_name(self) -> str:
        """Get record name with all macros replaced by a generic placeholder.

        This allows comparing record names that differ only in their macro
        prefix. For example:
            "$(P):Temperature"  -> "${MACRO}:Temperature"
            "$(SYS):Temperature" -> "${MACRO}:Temperature"
        These would then be recognized as structurally equivalent.
        """
        return re.sub(r"\$\([^)]+\)", "${MACRO}", self.record_name)

    def get_signature(self) -> str:
        """Generate a structural signature for comparing records.

        The signature captures the record type and sorted field names,
        but NOT field values. Two records with the same type and same
        set of field names are considered structurally identical even if
        their values differ.

        Example: "ai:DESC,DTYP,EGU,HOPR,INP,LOPR,SCAN"
        """
        field_names = sorted(self.fields.keys())
        return f"{self.record_type}:{','.join(field_names)}"

    def validate(self) -> Tuple[bool, List[str]]:
        """Validate this EPICS record.

        Returns a tuple `(is_valid, issues)` where `is_valid` is True when no
        fatal errors were found and `issues` is a list of human-readable
        error/warning messages describing problems or recommendations.

        Validation rules implemented:
        - `record_type` must be a non-empty word
        - `record_name` must be non-empty and any macro occurrences must match
          the form `$(NAME)` (basic sanity check)
        - field names must be alphanumeric/underscore
        - a few record-type specific checks (lightweight):
            - `calc` records should have a `CALC` field
            - `ai` records should have an `INP` or `VAL` field
        """
        issues: List[str] = []

        # Validate record_type
        if not self.record_type or not re.match(r"^\w+$", self.record_type):
            issues.append(f"Invalid record type: '{self.record_type}'")

        # Validate record_name
        if not self.record_name:
            issues.append("Empty record name")
        else:
            # Check for well-formed macro patterns like $(MACRO)
            # Find any '$(' occurrences that are not closed properly
            if "$(" in self.record_name:
                malformed = False
                for m in re.finditer(r"\$\([^)]+\)", self.record_name):
                    pass
                # If no full matches but a '$(' exists, flag malformed
                if not re.search(r"\$\([^)]+\)", self.record_name):
                    malformed = True
                if malformed:
                    issues.append(f"Malformed macro in record name: '{self.record_name}'")

        # Validate field names
        for fname in self.fields.keys():
            if not re.match(r"^[A-Za-z0-9_]+$", fname):
                issues.append(f"Invalid field name: '{fname}'")

        # Lightweight, record-type specific checks (recommendations)
        rtype = (self.record_type or "").lower()
        if rtype == "calc":
            if "CALC" not in self.fields:
                issues.append("WARN: 'calc' record missing CALC field")
        if rtype == "ai":
            if "INP" not in self.fields and "VAL" not in self.fields:
                issues.append("WARN: 'ai' record missing INP or VAL field")

        # Determine validity: any issue that does not start with 'WARN' is fatal
        fatal_issues = [i for i in issues if not i.startswith("WARN")]
        is_valid = len(fatal_issues) == 0

        return is_valid, issues


@dataclass
class TemplateFile:
    """Represents a complete parsed template file (.db, .vdb, or .template).

    Aggregates all records, macros, and includes found in the file,
    and provides methods for comparing against other template files.
    """

    filepath: Path  # absolute path to the source file
    records: List[EPICSRecord] = field(default_factory=list)  # all records in the file
    macros: Set[str] = field(default_factory=set)  # all macro names used, e.g. {"P", "R", "PORT"}
    includes: List[str] = field(default_factory=list)  # included file references
    file_type: str = ""  # file extension: ".db", ".vdb", or ".template"

    def get_record_signatures(self) -> Dict[str, int]:
        """Get a frequency count of each unique record signature in this file.

        Useful for identifying files with many similar records (e.g., a file
        with 10 ai records that all have the same fields).
        """
        signatures = defaultdict(int)
        for record in self.records:
            signatures[record.get_signature()] += 1
        return dict(signatures)

    def calculate_similarity(self, other: "TemplateFile") -> float:
        """Calculate Jaccard similarity coefficient between two templates.

        Uses record signatures (type + field names) to compare structural
        similarity. Returns a value from 0.0 (completely different) to
        1.0 (identical structure).

        Jaccard index = |intersection| / |union| of the signature sets.
        """
        if not self.records or not other.records:
            return 0.0

        # Build sets of unique record signatures from each file
        self_sigs = set(r.get_signature() for r in self.records)
        other_sigs = set(r.get_signature() for r in other.records)

        if not self_sigs and not other_sigs:
            return 1.0  # both empty = identical

        intersection = len(self_sigs & other_sigs)
        union = len(self_sigs | other_sigs)

        return intersection / union if union > 0 else 0.0


# =============================================================================
# MAIN ANALYZER CLASS
# =============================================================================


class TemplateAnalyzer:
    """Main analyzer for EPICS template files.

    Provides functionality to:
    1. Parse .db/.vdb/.template files into structured data
    2. Find duplicate or near-duplicate files
    3. Identify records that appear across multiple files
    4. Compare two files in detail
    5. Map .vdb templates to their .db instantiations
    6. Suggest consolidation opportunities
    """

    # -------------------------------------------------------------------------
    # Regex patterns for parsing EPICS database file syntax
    # -------------------------------------------------------------------------

    # Matches a record declaration line and captures (type, name):
    #   record(ai, "$(P):Temperature") {
    #   record(bo, "$(P):OnOff") {
    #   record(longin, "$(SYS):Counter") {
    # Group 1: record type (ai, bo, longin, etc.)
    # Group 2: record name including macros ("$(P):Temperature")
    RECORD_PATTERN = re.compile(
        r'^record\s*\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)\s*{', re.MULTILINE
    )
    # start of any line, 'record' any amount of white space '(' any amount of white space, word character, white space ',' 
    # start after quote continue until next quote ([^"]+) match one or more things

    #match group (0) entire pattern, match group (1) first group , match group (2) second group
    

    # Matches a field definition inside a record block:
    #   field(DESC, "Motor position")     -> ("DESC", "Motor position")
    #   field(DTYP, "asynInt32")          -> ("DTYP", "asynInt32")
    #   field(SCAN, .1 second)            -> ("SCAN", ".1 second")
    #   field(VAL, 0)                     -> ("VAL", "0")
    # Group 1: field name (DESC, DTYP, SCAN, VAL, etc.)
    # Group 2: field value (quoted or unquoted)
    # Note: the "? makes quotes optional to handle both styles
    FIELD_PATTERN = re.compile(
        r'^\s*field\s*\(\s*(\w+)\s*,\s*"?([^")]+)"?\s*\)', re.MULTILINE
    )

    # Matches macro substitution references anywhere in the file:
    #   $(P)      -> "P"
    #   $(DEVICE) -> "DEVICE"
    #   $(PORT)   -> "PORT"
    # Group 1: macro name without the $() wrapper
    MACRO_PATTERN = re.compile(r"\$\(([^)]+)\)")

    # Matches include directives (typically in .dbd files or substitutions):
    #   include "base.dbd"    -> "base.dbd"
    #   include "asyn.dbd"    -> "asyn.dbd"
    # Group 1: included filename
    INCLUDE_PATTERN = re.compile(r'^include\s+"([^"]+)"', re.MULTILINE)

    # Matches info() tags inside record blocks (metadata annotations):
    #   info(autosaveFields, "VAL DESC")  -> ("autosaveFields", "VAL DESC")
    #   info(archive, "monitor")          -> ("archive", "monitor")
    #   info(Q, "$(Q)")                   -> ("Q", "$(Q)")
    # Group 1: info tag name
    # Group 2: info tag value
    # These are stored as "info_<name>" in the fields dict
    INFO_PATTERN = re.compile(
        r'^\s*info\s*\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)', re.MULTILINE
    )

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.templates: Dict[str, TemplateFile] = {}  # relative_path -> parsed template
        self.errors: List[str] = []  # accumulated parse/read errors

    def log(self, message: str):
        """Print message if verbose mode enabled"""
        if self.verbose:
            print(f"[INFO] {message}")

    # -------------------------------------------------------------------------
    # FILE PARSING
    # -------------------------------------------------------------------------

    def parse_file(self, filepath: Path) -> Optional[TemplateFile]:
        """Parse a single EPICS template/database file into a TemplateFile object.

        Reads the file, then uses the regex patterns to extract:
        - All include directives
        - All macro references (builds set of required macros)
        - All record blocks with their fields and info tags

        Returns None if the file cannot be read.
        """
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            self.errors.append(f"Error reading {filepath}: {e}")
            return None

        template = TemplateFile(filepath=filepath, file_type=filepath.suffix)

        # Extract all include directives (e.g., include "base.dbd")
        for match in self.INCLUDE_PATTERN.finditer(content):
            template.includes.append(match.group(1))

        # Extract all macro names used in the file (e.g., P, R, PORT)
        for match in self.MACRO_PATTERN.finditer(content):
            template.macros.add(match.group(1))

        # Find all record declarations to establish record boundaries
        record_matches = list(self.RECORD_PATTERN.finditer(content))

        for i, match in enumerate(record_matches):
            record_type = match.group(1)  # e.g. "ai"
            record_name = match.group(2)  # e.g. "$(P):Temperature"
            start_pos = match.start()

            # Determine the text span of this record block:
            # from this record's start to the next record's start (or EOF)
            if i < len(record_matches) - 1:
                end_pos = record_matches[i + 1].start()
            else:
                end_pos = len(content)

            record_content = content[start_pos:end_pos]

            # Parse all field() definitions within this record's text span
            fields = {}
            for field_match in self.FIELD_PATTERN.finditer(record_content):
                field_name = field_match.group(1)   # e.g. "DESC"
                field_value = field_match.group(2)  # e.g. "Motor position"
                fields[field_name] = field_value

            # Parse info() tags and store them with "info_" prefix to avoid
            # colliding with regular field names
            for info_match in self.INFO_PATTERN.finditer(record_content):
                info_name = f"info_{info_match.group(1)}"  # e.g. "info_autosaveFields"
                fields[info_name] = info_match.group(2)

            # Calculate line number by counting newlines before this position
            line_number = content[:start_pos].count("\n") + 1

            record = EPICSRecord(
                record_type=record_type,
                record_name=record_name,
                fields=fields,
                line_number=line_number,
                raw_content=record_content,
            )

            template.records.append(record)

        self.log(
            f"Parsed {filepath.name}: {len(template.records)} records, "
            f"{len(template.macros)} macros, {len(template.includes)} includes"
        )

        return template

    # -------------------------------------------------------------------------
    # DIRECTORY SCANNING
    # -------------------------------------------------------------------------

    def analyze_directory(
        self, directory: Path, extensions: List[str] = None
    ) -> Dict[str, TemplateFile]:
        """Recursively find and parse all EPICS template files in a directory.

        Walks the directory tree looking for files matching the given extensions
        (defaults to .db, .vdb, .template). Each file is parsed and stored
        in self.templates keyed by its relative path from the root directory.
        """
        if extensions is None:
            extensions = [".db", ".vdb", ".template"]

        templates = {}

        for ext in extensions:
            for filepath in directory.rglob(f"*{ext}"):
                self.log(f"Processing {filepath}")
                template = self.parse_file(filepath)
                if template:
                    rel_path = filepath.relative_to(directory)
                    templates[str(rel_path)] = template

        self.templates = templates
        return templates

    # -------------------------------------------------------------------------
    # DUPLICATE DETECTION
    # -------------------------------------------------------------------------

    def find_duplicates(self, threshold: float = 0.95) -> List[Tuple[str, str, float]]:
        """Find pairs of templates that exceed the similarity threshold.

        Performs an O(n^2) pairwise comparison of all loaded templates.
        Returns a sorted list of (file1, file2, similarity_score) tuples,
        ordered from most similar to least similar.

        Default threshold of 0.95 catches near-exact duplicates.
        """
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

    # -------------------------------------------------------------------------
    # CROSS-FILE RECORD ANALYSIS
    # -------------------------------------------------------------------------

    def find_similar_records(
        self, threshold: float = 0.8
    ) -> Dict[str, List[Tuple[str, str]]]:
        """Find record signatures that appear in multiple files.

        Groups records by their structural signature (type + field names)
        and reports which signatures appear across more than one file.
        This reveals copy-paste patterns and consolidation opportunities.

        Returns: {signature: [(filename, record_name), ...]}
        """
        record_map = defaultdict(list)

        # Build a reverse index: signature -> list of (file, record_name)
        for name, template in self.templates.items():
            for record in template.records:
                sig = record.get_signature()
                record_map[sig].append((name, record.record_name))

        # Filter to signatures that appear in more than one unique file
        similar = {}
        for sig, occurrences in record_map.items():
            unique_files = set(occ[0] for occ in occurrences)
            if len(unique_files) > 1:
                similar[sig] = occurrences

        return similar

    # -------------------------------------------------------------------------
    # DETAILED FILE COMPARISON
    # -------------------------------------------------------------------------

    def compare_files(self, file1: str, file2: str) -> Dict[str, Any]:
        """Perform a detailed structural and textual comparison of two files.

        Returns a dict containing:
        - similarity: Jaccard score between the two files
        - file1/file2: record counts, macros, and unique signatures for each
        - common: signatures and macros shared between both files
        - diff_preview: first 50 lines of a unified diff of raw file content
        """
        if file1 not in self.templates or file2 not in self.templates:
            return {"error": "One or both files not found in analyzed templates"}

        template1 = self.templates[file1]
        template2 = self.templates[file2]

        # Build signature sets for set-difference analysis
        sigs1 = set(r.get_signature() for r in template1.records)
        sigs2 = set(r.get_signature() for r in template2.records)

        # Macro sets for comparison
        macros1 = template1.macros
        macros2 = template2.macros

        # Generate a unified diff of the raw file contents for visual review
        with open(template1.filepath, "r") as f1, open(template2.filepath, "r") as f2:
            lines1 = f1.readlines()
            lines2 = f2.readlines()
            diff = list(
                difflib.unified_diff(
                    lines1, lines2, fromfile=file1, tofile=file2, lineterm=""
                )
            )

        return {
            "similarity": template1.calculate_similarity(template2),
            "file1": {
                "records": len(template1.records),
                "macros": sorted(macros1),
                "unique_signatures": sorted(sigs1 - sigs2),  # in file1 but not file2
            },
            "file2": {
                "records": len(template2.records),
                "macros": sorted(macros2),
                "unique_signatures": sorted(sigs2 - sigs1),  # in file2 but not file1
            },
            "common": {
                "signatures": sorted(sigs1 & sigs2),  # in both files
                "macros": sorted(macros1 & macros2),
            },
            "diff_preview": diff[:50] if diff else [],
        }

    # -------------------------------------------------------------------------
    # VDB-TO-DB TEMPLATE MAPPING
    # -------------------------------------------------------------------------

    def analyze_vdb_to_db_mapping(self) -> Dict[str, Any]:
        """Analyze how .vdb templates relate to .db instantiations.

        In EPICS, .vdb files are often parameterized templates that get
        instantiated into .db files via macro substitution. This method
        tries to identify which .db files were likely derived from which
        .vdb templates by:
        1. Checking if the .db filename contains the .vdb stem
        2. Verifying structural similarity > 50%

        Returns a report of identified template-to-instance mappings.
        """
        vdb_files = {k: v for k, v in self.templates.items() if v.file_type == ".vdb"}
        db_files = {k: v for k, v in self.templates.items() if v.file_type == ".db"}

        mappings = []

        for vdb_name, vdb_template in vdb_files.items():
            vdb_base = Path(vdb_name).stem  # e.g. "motor" from "motor.vdb"
            potential_matches = []

            for db_name, db_template in db_files.items():
                # Heuristic: db filename contains the vdb base name
                if vdb_base in db_name:
                    similarity = vdb_template.calculate_similarity(db_template)
                    if similarity > 0.5:
                        potential_matches.append(
                            {"db_file": db_name, "similarity": similarity}
                        )

            if potential_matches:
                mappings.append(
                    {
                        "vdb_template": vdb_name,
                        "derived_db_files": sorted(
                            potential_matches,
                            key=lambda x: x["similarity"],
                            reverse=True,
                        ),
                    }
                )

        return {
            "vdb_count": len(vdb_files),
            "db_count": len(db_files),
            "mappings": mappings,
        }

    # -------------------------------------------------------------------------
    # CONSOLIDATION ANALYSIS
    # -------------------------------------------------------------------------

    def find_consolidation_candidates(self) -> List[Dict[str, Any]]:
        """Find groups of highly similar files that could be merged into one template.

        Uses a greedy clustering approach:
        1. For each unprocessed file, find all other files with >80% similarity
        2. Group them together and track which record signatures are common to ALL
        3. Report the group with its common-record percentage

        This identifies cases where multiple .db files are nearly identical
        and could be replaced by a single parameterized .template file.
        """
        candidates = []
        processed = set()

        template_list = list(self.templates.items())

        for i, (name1, template1) in enumerate(template_list):
            if name1 in processed:
                continue

            group = [name1]
            group_signatures = set(r.get_signature() for r in template1.records)

            for j, (name2, template2) in enumerate(template_list[i + 1 :], i + 1):
                if name2 in processed:
                    continue

                similarity = template1.calculate_similarity(template2)
                if similarity > 0.8:
                    group.append(name2)
                    processed.add(name2)
                    # Intersect: keep only signatures common to ALL group members
                    sigs2 = set(r.get_signature() for r in template2.records)
                    group_signatures &= sigs2

            if len(group) > 1:
                # What fraction of the first file's records are shared by all?
                common_record_percentage = (
                    len(group_signatures)
                    / len(set(r.get_signature() for r in template1.records))
                    * 100
                )

                candidates.append(
                    {
                        "files": group,
                        "count": len(group),
                        "common_signatures": len(group_signatures),
                        "common_percentage": round(common_record_percentage, 1),
                        "potential_name": self._suggest_template_name(group),
                    }
                )

            processed.add(name1)

        return sorted(candidates, key=lambda x: x["count"], reverse=True)

    def _suggest_template_name(self, file_group: List[str]) -> str:
        """Suggest a template name for a group of similar files.

        Finds the longest common prefix of all filenames in the group,
        strips trailing numbers/underscores, and appends "_template".
        e.g. ["motor_1.db", "motor_2.db", "motor_3.db"] -> "motor_template"
        """
        if not file_group:
            return "template"

        names = [Path(f).stem for f in file_group]

        # Find common prefix across all filenames
        common_prefix = os.path.commonprefix(names)
        if common_prefix and not common_prefix.endswith("_"):
            # Strip trailing digits/underscores that are likely instance identifiers
            common_prefix = common_prefix.rstrip("_0123456789")

        return f"{common_prefix}_template" if common_prefix else "consolidated_template"

    # -------------------------------------------------------------------------
    # REPORT GENERATION
    # -------------------------------------------------------------------------

    def generate_report(self) -> str:
        """Generate a comprehensive human-readable analysis report.

        Covers:
        - Summary statistics (file counts, record counts, file types)
        - Macro usage frequency
        - Duplication analysis (>90% similar pairs)
        - Consolidation opportunities (groups of >80% similar files)
        - VDB-to-DB template mapping
        - Any errors encountered during parsing
        """
        report = []
        report.append("=" * 80)
        report.append("EPICS Template Analysis Report")
        report.append("=" * 80)
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        # --- Summary statistics ---
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

        # --- Macro usage across all files ---
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

            for macro, count in sorted(
                macro_count.items(), key=lambda x: x[1], reverse=True
            )[:10]:
                report.append(f"    ${macro}: used in {count} files")
        report.append("")

        # --- Duplication analysis ---
        duplicates = self.find_duplicates(threshold=0.9)
        report.append("DUPLICATION ANALYSIS:")
        if duplicates:
            report.append(
                f"  Found {len(duplicates)} potential duplicate pairs (>90% similar):"
            )
            for file1, file2, similarity in duplicates[:5]:
                report.append(f"    {file1}")
                report.append(f"    {file2}")
                report.append(f"    Similarity: {similarity*100:.1f}%")
                report.append("")
        else:
            report.append("  No significant duplicates found")
        report.append("")

        # --- Consolidation opportunities ---
        candidates = self.find_consolidation_candidates()
        report.append("CONSOLIDATION OPPORTUNITIES:")
        if candidates:
            report.append(
                f"  Found {len(candidates)} groups that could be consolidated:"
            )
            for candidate in candidates[:5]:
                report.append(f"    Group: {candidate['potential_name']}")
                report.append(f"      Files: {candidate['count']}")
                report.append(
                    f"      Common records: {candidate['common_percentage']}%"
                )
                for file in candidate["files"][:3]:
                    report.append(f"        - {file}")
                if len(candidate["files"]) > 3:
                    report.append(f"        ... and {len(candidate['files']) - 3} more")
                report.append("")
        else:
            report.append("  No consolidation opportunities found")

        report.append("")

        # --- VDB to DB template mapping ---
        mapping = self.analyze_vdb_to_db_mapping()
        if mapping["mappings"]:
            report.append("TEMPLATE TO INSTANCE MAPPING (.vdb -> .db):")
            report.append(f"  Templates (.vdb): {mapping['vdb_count']}")
            report.append(f"  Instances (.db): {mapping['db_count']}")
            report.append(f"  Identified mappings:")
            for m in mapping["mappings"][:5]:
                report.append(f"    {m['vdb_template']}:")
                for db in m["derived_db_files"][:3]:
                    report.append(
                        f"      -> {db['db_file']} ({db['similarity']*100:.1f}% similar)"
                    )
                report.append("")

        # --- Errors ---
        if self.errors:
            report.append("ERRORS ENCOUNTERED:")
            for error in self.errors[:10]:
                report.append(f"  - {error}")
            if len(self.errors) > 10:
                report.append(f"  ... and {len(self.errors) - 10} more")
            report.append("")

        report.append("=" * 80)

        return "\n".join(report)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================


def main():
    """Main entry point - parses CLI arguments and dispatches to analyzer methods.

    Subcommands:
      analyze     - Scan a directory and print summary stats
      compare     - Side-by-side comparison of two specific files
      duplicates  - Find near-duplicate file pairs above a similarity threshold
      consolidate - Identify groups of files that could be merged into templates
      mapping     - Show which .vdb templates map to which .db instances
      report      - Generate a full-text analysis report
    """
    parser = argparse.ArgumentParser(
        description="EPICS Template Analyzer - Analyze and compare .db, .vdb, and .template files",
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
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # --- "analyze" subcommand: scan directory and print stats ---
    analyze_parser = subparsers.add_parser(
        "analyze", help="Analyze templates in directory"
    )
    analyze_parser.add_argument("directory", help="Directory containing template files")
    analyze_parser.add_argument(
        "--extensions",
        nargs="+",
        default=[".db", ".vdb", ".template"],
        help="File extensions to analyze",
    )

    # --- "compare" subcommand: detailed comparison of two files ---
    compare_parser = subparsers.add_parser("compare", help="Compare two template files")
    compare_parser.add_argument("file1", help="First file to compare")
    compare_parser.add_argument("file2", help="Second file to compare")
    compare_parser.add_argument(
        "--detailed", action="store_true", help="Show detailed comparison"
    )

    # --- "duplicates" subcommand: find near-duplicate pairs ---
    dup_parser = subparsers.add_parser("duplicates", help="Find duplicate templates")
    dup_parser.add_argument("directory", help="Directory to analyze")
    dup_parser.add_argument(
        "--threshold", type=float, default=0.95, help="Similarity threshold (0-1)"
    )

    # --- "consolidate" subcommand: find merge-candidate groups ---
    consol_parser = subparsers.add_parser(
        "consolidate", help="Find consolidation opportunities"
    )
    consol_parser.add_argument("directory", help="Directory to analyze")

    # --- "mapping" subcommand: .vdb -> .db relationship analysis ---
    map_parser = subparsers.add_parser("mapping", help="Analyze VDB to DB mappings")
    map_parser.add_argument("directory", help="Directory to analyze")

    # --- "report" subcommand: full text report ---
    report_parser = subparsers.add_parser(
        "report", help="Generate full analysis report"
    )
    report_parser.add_argument("directory", help="Directory to analyze")
    report_parser.add_argument(
        "--output", help="Output file (default: print to stdout)"
    )

    # Add verbose flag to all subparsers
    for subparser in [
        analyze_parser,
        compare_parser,
        dup_parser,
        consol_parser,
        map_parser,
        report_parser,
    ]:
        subparser.add_argument(
            "-v", "--verbose", action="store_true", help="Verbose output"
        )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Instantiate the analyzer with verbosity setting
    analyzer = TemplateAnalyzer(
        verbose=args.verbose if hasattr(args, "verbose") else False
    )

    # --- Dispatch to the appropriate subcommand handler ---

    if args.command == "analyze":
        directory = Path(args.directory)
        if not directory.exists():
            print(f"Error: Directory {directory} does not exist")
            sys.exit(1)

        templates = analyzer.analyze_directory(directory, args.extensions)
        print(f"Analyzed {len(templates)} template files")

        # Print breakdown by file type
        file_types = defaultdict(int)
        total_records = 0
        for template in templates.values():
            file_types[template.file_type] += 1
            total_records += len(template.records)

        print(f"Total records: {total_records}")
        print("File types:")
        for ext, count in sorted(file_types.items()):
            print(f"  {ext}: {count} files")

    elif args.command == "compare":
        file1_path = Path(args.file1)
        file2_path = Path(args.file2)

        # Parse both files individually
        template1 = analyzer.parse_file(file1_path)
        template2 = analyzer.parse_file(file2_path)

        if template1 and template2:
            # Register them so compare_files() can look them up
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

            if args.detailed and comparison.get("diff_preview"):
                print("\nDiff preview (first 50 lines):")
                for line in comparison["diff_preview"]:
                    print(line.rstrip())

    elif args.command == "duplicates":
        directory = Path(args.directory)
        analyzer.analyze_directory(directory)

        duplicates = analyzer.find_duplicates(threshold=args.threshold)

        if duplicates:
            print(
                f"Found {len(duplicates)} potential duplicates (>{args.threshold*100:.0f}% similar):"
            )
            for file1, file2, similarity in duplicates:
                print(f"\n{similarity*100:.1f}% similar:")
                print(f"  - {file1}")
                print(f"  - {file2}")
        else:
            print("No significant duplicates found")

    elif args.command == "consolidate":
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
                for file in candidate["files"][:5]:
                    print(f"     - {file}")
                if len(candidate["files"]) > 5:
                    print(f"     ... and {len(candidate['files']) - 5} more")
                print()
        else:
            print("No consolidation opportunities found")

    elif args.command == "mapping":
        directory = Path(args.directory)
        analyzer.analyze_directory(directory)

        mapping = analyzer.analyze_vdb_to_db_mapping()

        print(f"Template to Instance Mapping Analysis:")
        print(f"Templates (.vdb): {mapping['vdb_count']}")
        print(f"Instances (.db): {mapping['db_count']}")

        if mapping["mappings"]:
            print(f"\nIdentified mappings:")
            for m in mapping["mappings"]:
                print(f"\n{m['vdb_template']}:")
                for db in m["derived_db_files"][:5]:
                    print(f"  -> {db['db_file']} ({db['similarity']*100:.1f}% similar)")
                if len(m["derived_db_files"]) > 5:
                    print(f"  ... and {len(m['derived_db_files']) - 5} more")
        else:
            print("\nNo clear VDB to DB mappings found")

    elif args.command == "report":
        directory = Path(args.directory)
        analyzer.analyze_directory(directory)

        report = analyzer.generate_report()

        if args.output:
            with open(args.output, "w") as f:
                f.write(report)
            print(f"Report saved to {args.output}")
        else:
            print(report)


if __name__ == "__main__":
    main()
