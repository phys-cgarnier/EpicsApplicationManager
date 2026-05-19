#!/usr/bin/env python3
"""
EPICS IOC Validation Engine
===========================
Multi-stage validation system for IOC configuration files with intelligent
error detection, correction suggestions, and approval workflows.

Author: SLAC Cryoplant Team
Date: 2024
"""

import re
import os
import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any, Set
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import difflib
import shutil

class Severity(Enum):
    """Issue severity levels"""
    CRITICAL = "critical"  # Must fix - will break the system
    WARNING = "warning"    # Should fix - may cause issues
    SUGGESTION = "suggestion"  # Optional - improvements
    INFO = "info"         # Informational only

class QuoteRule(Enum):
    """Quote handling rules for different value types"""
    NO_QUOTES = "no_quotes"           # Never use quotes
    REQUIRED = "required"              # Always use quotes
    OPTIONAL = "optional"              # Either way is fine
    QUOTE_IF_SPACES = "quote_if_spaces"  # Quote only if contains spaces

@dataclass
class ValidationIssue:
    """Represents a single validation issue"""
    severity: Severity
    line_number: int
    column: Optional[int]
    message: str
    current_value: Optional[str] = None
    suggested_value: Optional[str] = None
    file_path: Optional[str] = None
    rule_id: Optional[str] = None
    auto_fixable: bool = False

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'severity': self.severity.value,
            'line_number': self.line_number,
            'column': self.column,
            'message': self.message,
            'current_value': self.current_value,
            'suggested_value': self.suggested_value,
            'file_path': self.file_path,
            'rule_id': self.rule_id,
            'auto_fixable': self.auto_fixable
        }

@dataclass
class ValidationResult:
    """Complete validation result for a file or system"""
    file_path: str
    timestamp: datetime = field(default_factory=datetime.now)
    passed: bool = True
    issues: List[ValidationIssue] = field(default_factory=list)
    statistics: Dict[str, Any] = field(default_factory=dict)

    def add_issue(self, issue: ValidationIssue):
        """Add an issue to the result"""
        self.issues.append(issue)
        if issue.severity == Severity.CRITICAL:
            self.passed = False

    def get_issues_by_severity(self, severity: Severity) -> List[ValidationIssue]:
        """Get all issues of a specific severity"""
        return [i for i in self.issues if i.severity == severity]

    def to_json(self) -> str:
        """Convert to JSON string"""
        data = {
            'file_path': self.file_path,
            'timestamp': self.timestamp.isoformat(),
            'passed': self.passed,
            'issues': [i.to_dict() for i in self.issues],
            'statistics': self.statistics
        }
        return json.dumps(data, indent=2)

class QuoteManager:
    """Manages quote rules and enforcement"""

    # Define quote rules for different contexts
    RULES = {
        'numeric': QuoteRule.NO_QUOTES,          # 123, 45.6, -7.8
        'boolean': QuoteRule.NO_QUOTES,          # true, false, TRUE, FALSE
        'macro': QuoteRule.NO_QUOTES,            # $(MACRO_NAME)
        'empty': QuoteRule.REQUIRED,             # ""
        'description': QuoteRule.REQUIRED,       # Always quote descriptions
        'path': QuoteRule.QUOTE_IF_SPACES,       # Quote if contains spaces
        'identifier': QuoteRule.NO_QUOTES,       # PV names, tag names
        'string': QuoteRule.QUOTE_IF_SPACES      # General strings
    }

    # Patterns for identifying value types
    PATTERNS = {
        'numeric': re.compile(r'^-?\d+\.?\d*$'),
        'boolean': re.compile(r'^(true|false|TRUE|FALSE)$'),
        'macro': re.compile(r'^\$\([^)]+\)$'),
        'empty': re.compile(r'^$'),
        'path': re.compile(r'^[/\\]|\.{1,2}[/\\]'),
        'identifier': re.compile(r'^[A-Z][A-Z0-9_:]+$')
    }

    # Context-specific rules (column names that have special rules)
    CONTEXT_RULES = {
        'DESC': QuoteRule.REQUIRED,
        'DESCRIPTION': QuoteRule.REQUIRED,
        'COMMENT': QuoteRule.REQUIRED,
        'EGU': QuoteRule.QUOTE_IF_SPACES,
        'PIDTAG': QuoteRule.NO_QUOTES,
        'PLCTAG': QuoteRule.NO_QUOTES,
        'PLC_NAME': QuoteRule.NO_QUOTES,
        'AREA': QuoteRule.NO_QUOTES,
        'LOCA': QuoteRule.NO_QUOTES
    }

    @classmethod
    def get_value_type(cls, value: str) -> str:
        """Determine the type of a value"""
        value = value.strip()

        # Remove existing quotes for analysis
        unquoted = cls.remove_quotes(value)

        # Check against patterns
        for value_type, pattern in cls.PATTERNS.items():
            if pattern.match(unquoted):
                return value_type

        # Default to string
        return 'string'

    @classmethod
    def remove_quotes(cls, value: str) -> str:
        """Remove quotes from a value"""
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        return value

    @classmethod
    def should_quote(cls, value: str, context: Optional[str] = None) -> bool:
        """Determine if a value should be quoted based on rules"""
        # Check context-specific rules first
        if context and context.upper() in cls.CONTEXT_RULES:
            rule = cls.CONTEXT_RULES[context.upper()]
        else:
            # Determine rule based on value type
            value_type = cls.get_value_type(value)
            rule = cls.RULES.get(value_type, QuoteRule.QUOTE_IF_SPACES)

        # Apply the rule
        unquoted = cls.remove_quotes(value)

        if rule == QuoteRule.REQUIRED:
            return True
        elif rule == QuoteRule.NO_QUOTES:
            return False
        elif rule == QuoteRule.QUOTE_IF_SPACES:
            return ' ' in unquoted or '\t' in unquoted
        else:  # OPTIONAL
            return False  # Default to no quotes for optional

    @classmethod
    def fix_quotes(cls, value: str, context: Optional[str] = None) -> str:
        """Fix quotes on a value according to rules"""
        unquoted = cls.remove_quotes(value)

        if cls.should_quote(value, context):
            return f'"{unquoted}"'
        else:
            return unquoted

class ValidationEngine:
    """Main validation engine for IOC configuration files"""

    def __init__(self, config_path: Optional[str] = None):
        """Initialize validation engine with optional configuration"""
        self.quote_manager = QuoteManager()
        self.config = self._load_config(config_path) if config_path else {}
        self.custom_rules = []

    def _load_config(self, config_path: str) -> Dict:
        """Load validation configuration from file"""
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load config from {config_path}: {e}")
            return {}

    def validate_substitution_file(self, file_path: str) -> ValidationResult:
        """Perform comprehensive validation on a substitution file"""
        result = ValidationResult(file_path=file_path)

        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()
        except Exception as e:
            result.add_issue(ValidationIssue(
                severity=Severity.CRITICAL,
                line_number=0,
                column=None,
                message=f"Cannot read file: {e}",
                rule_id="FILE_READ_ERROR"
            ))
            return result

        # Stage 1: Syntax validation
        self._validate_syntax(lines, result)

        # Stage 2: Structure validation
        self._validate_structure(lines, result)

        # Stage 3: Quote consistency
        self._validate_quotes(lines, result)

        # Stage 4: Macro validation
        self._validate_macros(lines, result)

        # Stage 5: Cross-reference validation
        self._validate_cross_references(lines, result, file_path)

        # Calculate statistics
        result.statistics = self._calculate_statistics(lines, result)

        return result

    def _validate_syntax(self, lines: List[str], result: ValidationResult):
        """Stage 1: Basic syntax validation"""
        brace_stack = []
        pattern_regex = re.compile(r'^\s*pattern\s*{(.+)}', re.IGNORECASE)
        data_regex = re.compile(r'^\s*{(.+)}')

        for i, line in enumerate(lines, 1):
            line_stripped = line.strip()

            # Skip comments and empty lines
            if not line_stripped or line_stripped.startswith('#'):
                continue

            # Check for brace matching
            for j, char in enumerate(line):
                if char == '{':
                    brace_stack.append((i, j))
                elif char == '}':
                    if not brace_stack:
                        result.add_issue(ValidationIssue(
                            severity=Severity.CRITICAL,
                            line_number=i,
                            column=j,
                            message="Unmatched closing brace '}'",
                            rule_id="UNMATCHED_BRACE",
                            auto_fixable=False
                        ))
                    else:
                        brace_stack.pop()

            # Check for mistyped closing brace - only at the end of pattern or data lines
            if pattern_regex.match(line_stripped) or data_regex.match(line_stripped):
                # Check if line ends with ) instead of }
                # Look for the last non-whitespace character
                line_content = line.rstrip()
                if line_content and line_content[-1] == ')':
                    # Check if this is likely meant to be a closing brace
                    # Count opening and closing braces/parens in the line
                    open_braces = line_content.count('{')
                    close_braces = line_content.count('}')
                    open_parens = line_content.count('(')
                    close_parens = line_content.count(')')

                    # If we have an unmatched opening brace and the line ends with )
                    # it's likely a typo (unless it's inside a macro like $(SOMETHING))
                    if open_braces > close_braces:
                        # Make sure it's not part of a macro
                        last_paren_idx = line_content.rfind(')')
                        if last_paren_idx > 0:
                            # Check if there's a $( before this )
                            before_paren = line_content[:last_paren_idx]
                            if not ('$(' in before_paren and before_paren.count('$(') > before_paren.count(')')):
                                result.add_issue(ValidationIssue(
                                    severity=Severity.CRITICAL,
                                    line_number=i,
                                    column=len(line_content) - 1,
                                    message="Column/pattern ending with ')' should end with '}'",
                                    current_value=")",
                                    suggested_value="}",
                                    rule_id="BRACE_TYPO",
                                    auto_fixable=True
                                ))

        # Check for unclosed braces
        for line_num, col in brace_stack:
            result.add_issue(ValidationIssue(
                severity=Severity.CRITICAL,
                line_number=line_num,
                column=col,
                message="Unclosed opening brace '{'",
                rule_id="UNCLOSED_BRACE",
                auto_fixable=False
            ))

    def _validate_structure(self, lines: List[str], result: ValidationResult):
        """Stage 2: Structure validation (column counts, patterns)"""
        current_pattern = []
        pattern_line = 0
        in_block = False

        pattern_regex = re.compile(r'^\s*pattern\s*{(.+)}', re.IGNORECASE)
        data_regex = re.compile(r'^\s*{(.+)}')
        file_regex = re.compile(r'^file\s+([A-Za-z0-9_\-\.]+)')

        for i, line in enumerate(lines, 1):
            line_stripped = line.strip()

            # Check for file block start
            if file_regex.match(line_stripped):
                in_block = True
                current_pattern = []
                continue

            if not in_block:
                continue

            # Check for pattern definition
            pattern_match = pattern_regex.match(line_stripped)
            if pattern_match:
                pattern_str = pattern_match.group(1)
                current_pattern = [p.strip() for p in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', pattern_str)]
                pattern_line = i
                continue

            # Check for data row
            data_match = data_regex.match(line_stripped)
            if data_match and current_pattern:
                data_str = data_match.group(1)
                data_values = [v.strip() for v in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', data_str)]

                # Check column count
                if len(data_values) != len(current_pattern):
                    result.add_issue(ValidationIssue(
                        severity=Severity.CRITICAL,
                        line_number=i,
                        column=None,
                        message=f"Column count mismatch: expected {len(current_pattern)}, got {len(data_values)}",
                        rule_id="COLUMN_MISMATCH",
                        auto_fixable=False
                    ))

                # Check for empty values that should be quoted
                for j, value in enumerate(data_values):
                    # Only flag truly empty values (not just whitespace between commas)
                    if value == '' and j < len(current_pattern):
                        # This is an empty value between commas
                        result.add_issue(ValidationIssue(
                            severity=Severity.WARNING,
                            line_number=i,
                            column=j,
                            message=f"Empty value in column '{current_pattern[j]}' should be quoted as \"\"",
                            current_value=None,  # Don't try to replace empty string
                            suggested_value=None,  # Don't auto-fix empty values for now
                            rule_id="EMPTY_VALUE",
                            auto_fixable=False  # Disable auto-fix for empty values
                        ))

    def _validate_quotes(self, lines: List[str], result: ValidationResult):
        """Stage 3: Quote consistency validation"""
        data_regex = re.compile(r'^\s*{(.+)}')
        pattern_regex = re.compile(r'^\s*pattern\s*{(.+)}', re.IGNORECASE)
        current_pattern = []

        for i, line in enumerate(lines, 1):
            line_stripped = line.strip()

            # Track pattern columns
            pattern_match = pattern_regex.match(line_stripped)
            if pattern_match:
                pattern_str = pattern_match.group(1)
                current_pattern = [p.strip() for p in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', pattern_str)]
                continue

            # Check data rows
            data_match = data_regex.match(line_stripped)
            if data_match and current_pattern:
                data_str = data_match.group(1)
                data_values = [v.strip() for v in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', data_str)]

                for j, value in enumerate(data_values):
                    if j >= len(current_pattern):
                        continue

                    context = current_pattern[j]
                    fixed_value = self.quote_manager.fix_quotes(value, context)

                    if value != fixed_value:
                        severity = Severity.WARNING
                        message = f"Inconsistent quoting in column '{context}'"

                        # Determine specific issue
                        if '"' in value and '"' not in fixed_value:
                            message = f"Value in '{context}' should not be quoted"
                        elif '"' not in value and '"' in fixed_value:
                            message = f"Value in '{context}' should be quoted"

                        result.add_issue(ValidationIssue(
                            severity=severity,
                            line_number=i,
                            column=j,
                            message=message,
                            current_value=value,
                            suggested_value=fixed_value,
                            rule_id="QUOTE_CONSISTENCY",
                            auto_fixable=True
                        ))

    def _validate_macros(self, lines: List[str], result: ValidationResult):
        """Stage 4: Macro validation"""
        macro_pattern = re.compile(r'\$\(([^)]+)\)')
        defined_macros = set()
        used_macros = set()

        # First pass: collect all macro definitions and uses
        for i, line in enumerate(lines, 1):
            # Find all macros in the line
            macros = macro_pattern.findall(line)
            for macro in macros:
                used_macros.add(macro)

                # Check if this is a macro definition context
                if 'epicsEnvSet' in line or 'PLC_NAME' in macro:
                    defined_macros.add(macro)

        # Common system macros that are always defined
        system_macros = {'PLC_NAME', 'IOC_NAME', 'IOC_NODE', 'TOP', 'IOC', 'PWD'}
        defined_macros.update(system_macros)

        # Check for undefined macros
        undefined = used_macros - defined_macros
        for macro in undefined:
            # Find where it's used
            for i, line in enumerate(lines, 1):
                if f'$({macro})' in line:
                    result.add_issue(ValidationIssue(
                        severity=Severity.WARNING,
                        line_number=i,
                        column=line.find(f'$({macro})'),
                        message=f"Macro '$({macro})' is used but not defined",
                        current_value=f'$({macro})',
                        rule_id="UNDEFINED_MACRO",
                        auto_fixable=False
                    ))
                    break

    def _validate_cross_references(self, lines: List[str], result: ValidationResult,
                                  file_path: str):
        """Stage 5: Cross-reference validation (check referenced files exist)"""
        file_regex = re.compile(r'^file\s+([A-Za-z0-9_\-\.]+)')
        base_dir = os.path.dirname(file_path)

        for i, line in enumerate(lines, 1):
            file_match = file_regex.match(line.strip())
            if file_match:
                template_name = file_match.group(1)

                # Check multiple possible locations for the template
                possible_paths = [
                    os.path.join(base_dir, template_name),
                    os.path.join(base_dir, '..', template_name),
                    os.path.join(base_dir, '..', '..', 'db', template_name),
                    os.path.join(base_dir, '..', '..', 'Db', template_name)
                ]

                found = False
                for path in possible_paths:
                    if os.path.exists(path):
                        found = True
                        break

                if not found:
                    result.add_issue(ValidationIssue(
                        severity=Severity.WARNING,
                        line_number=i,
                        column=None,
                        message=f"Referenced template file '{template_name}' not found",
                        current_value=template_name,
                        rule_id="MISSING_TEMPLATE",
                        auto_fixable=False
                    ))

    def _calculate_statistics(self, lines: List[str], result: ValidationResult) -> Dict:
        """Calculate statistics about the validation"""
        stats = {
            'total_lines': len(lines),
            'total_issues': len(result.issues),
            'critical_issues': len(result.get_issues_by_severity(Severity.CRITICAL)),
            'warnings': len(result.get_issues_by_severity(Severity.WARNING)),
            'suggestions': len(result.get_issues_by_severity(Severity.SUGGESTION)),
            'auto_fixable': sum(1 for i in result.issues if i.auto_fixable),
            'blocks': 0,
            'patterns': 0,
            'data_rows': 0
        }

        # Count structures
        for line in lines:
            if line.strip().startswith('file '):
                stats['blocks'] += 1
            elif 'pattern' in line.lower() and '{' in line:
                stats['patterns'] += 1
            elif line.strip().startswith('{') and 'pattern' not in line.lower():
                stats['data_rows'] += 1

        return stats

    def validate_archive_file(self, file_path: str) -> ValidationResult:
        """Validate an archive configuration file"""
        result = ValidationResult(file_path=file_path)

        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()
        except Exception as e:
            result.add_issue(ValidationIssue(
                severity=Severity.CRITICAL,
                line_number=0,
                column=None,
                message=f"Cannot read file: {e}",
                rule_id="FILE_READ_ERROR"
            ))
            return result

        # Pattern for archive entries: PV_NAME PERIOD SCAN_TYPE
        archive_pattern = re.compile(r'^([A-Z][A-Z0-9_:]+)\s+(\d+)\s+(scan|monitor)$')
        pv_names = set()

        for i, line in enumerate(lines, 1):
            line_stripped = line.strip()

            # Skip comments and empty lines
            if not line_stripped or line_stripped.startswith('#'):
                continue

            # Validate archive entry format
            match = archive_pattern.match(line_stripped)
            if not match:
                result.add_issue(ValidationIssue(
                    severity=Severity.WARNING,
                    line_number=i,
                    column=None,
                    message=f"Invalid archive entry format",
                    current_value=line_stripped,
                    rule_id="INVALID_ARCHIVE_FORMAT",
                    auto_fixable=False
                ))
                continue

            pv_name, period, scan_type = match.groups()

            # Check for duplicate PVs
            if pv_name in pv_names:
                result.add_issue(ValidationIssue(
                    severity=Severity.WARNING,
                    line_number=i,
                    column=None,
                    message=f"Duplicate PV in archive: {pv_name}",
                    current_value=pv_name,
                    rule_id="DUPLICATE_ARCHIVE_PV",
                    auto_fixable=False
                ))
            pv_names.add(pv_name)

            # Validate sampling period
            period_int = int(period)
            if period_int <= 0:
                result.add_issue(ValidationIssue(
                    severity=Severity.WARNING,
                    line_number=i,
                    column=None,
                    message=f"Invalid sampling period: {period}",
                    current_value=period,
                    suggested_value="1",
                    rule_id="INVALID_PERIOD",
                    auto_fixable=True
                ))

        result.statistics = {
            'total_pvs': len(pv_names),
            'total_lines': len(lines),
            'total_issues': len(result.issues)
        }

        return result

    def validate_startup_script(self, file_path: str) -> ValidationResult:
        """Validate an IOC startup script (st.cmd)"""
        result = ValidationResult(file_path=file_path)

        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()
        except Exception as e:
            result.add_issue(ValidationIssue(
                severity=Severity.CRITICAL,
                line_number=0,
                column=None,
                message=f"Cannot read file: {e}",
                rule_id="FILE_READ_ERROR"
            ))
            return result

        # Patterns for common st.cmd elements
        dbload_pattern = re.compile(r'dbLoadRecords\s*\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?\s*\)')
        envset_pattern = re.compile(r'epicsEnvSet\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)')

        base_dir = os.path.dirname(file_path)
        referenced_files = []
        environment_vars = {}

        for i, line in enumerate(lines, 1):
            line_stripped = line.strip()

            # Skip comments
            if line_stripped.startswith('#'):
                continue

            # Check dbLoadRecords
            dbload_match = dbload_pattern.search(line)
            if dbload_match:
                db_file = dbload_match.group(1)
                referenced_files.append((i, db_file))

                # Check if file exists
                possible_paths = [
                    os.path.join(base_dir, db_file),
                    os.path.join(base_dir, '..', '..', db_file),
                    os.path.join(base_dir, '..', '..', 'db', os.path.basename(db_file))
                ]

                found = False
                for path in possible_paths:
                    if os.path.exists(path):
                        found = True
                        break

                if not found:
                    result.add_issue(ValidationIssue(
                        severity=Severity.WARNING,
                        line_number=i,
                        column=None,
                        message=f"Referenced database file not found: {db_file}",
                        current_value=db_file,
                        rule_id="MISSING_DB_FILE",
                        auto_fixable=False
                    ))

            # Check epicsEnvSet
            envset_match = envset_pattern.search(line)
            if envset_match:
                var_name = envset_match.group(1)
                var_value = envset_match.group(2)
                environment_vars[var_name] = var_value

        # Check for required environment variables
        required_vars = ['IOC_NAME', 'IOC_NODE']
        for var in required_vars:
            if var not in environment_vars:
                result.add_issue(ValidationIssue(
                    severity=Severity.WARNING,
                    line_number=0,
                    column=None,
                    message=f"Required environment variable '{var}' not set",
                    rule_id="MISSING_ENV_VAR",
                    auto_fixable=False
                ))

        result.statistics = {
            'total_lines': len(lines),
            'referenced_files': len(referenced_files),
            'environment_vars': len(environment_vars),
            'total_issues': len(result.issues)
        }

        return result

    def generate_diff(self, original_lines: List[str],
                     fixed_lines: List[str]) -> str:
        """Generate a unified diff between original and fixed content"""
        diff = difflib.unified_diff(
            original_lines,
            fixed_lines,
            fromfile='Original',
            tofile='Fixed',
            lineterm=''
        )
        return '\n'.join(diff)

    def auto_fix_issues(self, file_path: str,
                       validation_result: ValidationResult) -> Tuple[List[str], int]:
        """Automatically fix auto-fixable issues in a file"""
        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()
        except Exception:
            return [], 0

        fixed_lines = lines.copy()
        fixes_applied = 0

        # Sort issues by line number (reverse) to avoid index shifting
        auto_fixable = [i for i in validation_result.issues if i.auto_fixable]
        auto_fixable.sort(key=lambda x: x.line_number, reverse=True)

        for issue in auto_fixable:
            if issue.suggested_value is not None:
                line_idx = issue.line_number - 1
                if 0 <= line_idx < len(fixed_lines):
                    line = fixed_lines[line_idx]

                    # Apply the fix carefully
                    if issue.current_value is not None:
                        # Special handling for empty values
                        if issue.current_value == "" and issue.suggested_value == '""':
                            # Find the empty field between commas
                            # This is tricky - we need to find the right empty spot
                            # For now, skip auto-fixing empty values
                            continue
                        else:
                            # Make sure we're replacing the exact match, not partial matches
                            # Use word boundaries or exact position matching
                            fixed_line = line.replace(issue.current_value,
                                                    issue.suggested_value, 1)  # Only replace first occurrence
                            if fixed_line != line:
                                fixed_lines[line_idx] = fixed_line
                                fixes_applied += 1

        return fixed_lines, fixes_applied

# Example usage and testing
if __name__ == "__main__":
    # Create validation engine
    engine = ValidationEngine()

    # Example: Validate a substitution file
    test_file = "C:/Users/mkeenan/Development/SLAC/Cryoplant/CryoplantApp/Db/2kcb/2kcb_AIs.substitutions"
    if os.path.exists(test_file):
        print(f"Validating: {test_file}")
        result = engine.validate_substitution_file(test_file)

        print(f"\nValidation Result: {'PASSED' if result.passed else 'FAILED'}")
        print(f"Total Issues: {len(result.issues)}")
        print(f"Critical: {len(result.get_issues_by_severity(Severity.CRITICAL))}")
        print(f"Warnings: {len(result.get_issues_by_severity(Severity.WARNING))}")
        print(f"Auto-fixable: {sum(1 for i in result.issues if i.auto_fixable)}")

        # Show first few issues
        for issue in result.issues[:5]:
            print(f"\n[{issue.severity.value.upper()}] Line {issue.line_number}: {issue.message}")
            if issue.suggested_value:
                print(f"  Current: {issue.current_value}")
                print(f"  Suggested: {issue.suggested_value}")