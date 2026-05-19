#!/usr/bin/env python3
"""
Test script for validation engine and preview dialog
"""

import sys
import os
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from ioc_validation_engine import ValidationEngine, Severity

def test_validation():
    """Test the validation engine with sample data"""

    # Create a test substitution file with issues
    test_content = """#==============================================================================
# Test substitution file with various issues
#==============================================================================

file cpdt_diff_pressure.vdb
{
    pattern   { PIDTAG,      PLC_NAME,     PLCTAG,      AREA,  LOCA,  EGU,   DESC                          )
              { C1PDT51011,  $(PLC_NAME),  D1PDT51011,  CP15,  1011,  mbar,  D1-IB Line A Warm Flow PDT   }
              { C1PDT51013,  $(PLC_NAME),  D1PDT51013,  CP15,  1013,  mbar,  "D1-IB Line C Warm Flow PDT"  }
              { C1PDT51015,  $(PLC_NAME),  D1PDT51015,  CP15,  1015,  "mbar",  D1-IB Line E Warm Flow PDT  }
}

file cft_flow.vdb
{
    pattern   { PIDTAG,     PLC_NAME,     PLCTAG,     AREA,  LOCA,  DESC                                 }
              { C1FT51011,  $(PLC_NAME),  D1FT51011,  CP15,  1011,  D1 Interface Box Line A Warm Flow   )
              { C1FT51013,  $(PLC_NAME),  D1FT51013,  CP15,  1013,  "D1 Interface Box Line C Warm Flow"  }
              { C1FT51015,  $(PLC_NAME),  D1FT51015,  CP15,  D1 Interface Box Line E Warm Flow }
}

file test.vdb
{
    pattern   { TAG,     VALUE,    DESC  }
              { TEST1,   123,      "Test value"  }
              { TEST2,   "456",    Test value    }
              { TEST3,   ,         "Empty value" }
}
"""

    # Save to temporary file
    test_file = Path("test_substitution.tmp")
    with open(test_file, 'w') as f:
        f.write(test_content)

    # Run validation
    engine = ValidationEngine()
    result = engine.validate_substitution_file(str(test_file))

    print("=" * 60)
    print("VALIDATION TEST RESULTS")
    print("=" * 60)
    print(f"\nFile: {test_file}")
    print(f"Validation Result: {'PASSED' if result.passed else 'FAILED'}")
    print(f"\nTotal Issues: {len(result.issues)}")
    print(f"Critical Issues: {len(result.get_issues_by_severity(Severity.CRITICAL))}")
    print(f"Warnings: {len(result.get_issues_by_severity(Severity.WARNING))}")
    print(f"Suggestions: {len(result.get_issues_by_severity(Severity.SUGGESTION))}")
    print(f"Auto-fixable: {sum(1 for i in result.issues if i.auto_fixable)}")

    print("\n" + "=" * 60)
    print("DETAILED ISSUES:")
    print("=" * 60)

    # Group issues by severity
    for severity in [Severity.CRITICAL, Severity.WARNING, Severity.SUGGESTION]:
        issues = result.get_issues_by_severity(severity)
        if issues:
            print(f"\n{severity.value.upper()} ({len(issues)} issues):")
            print("-" * 40)
            for issue in issues:
                print(f"\nLine {issue.line_number}: {issue.message}")
                if issue.current_value is not None:
                    print(f"  Current: '{issue.current_value}'")
                if issue.suggested_value is not None:
                    print(f"  Suggested: '{issue.suggested_value}'")
                print(f"  Auto-fixable: {'Yes' if issue.auto_fixable else 'No'}")
                print(f"  Rule: {issue.rule_id}")

    print("\n" + "=" * 60)
    print("STATISTICS:")
    print("=" * 60)
    for key, value in result.statistics.items():
        print(f"{key}: {value}")

    # Clean up
    test_file.unlink()

    print("\n" + "=" * 60)
    print("SPECIFIC TESTS:")
    print("=" * 60)

    # Test 1: ) vs } detection
    print("\n1. Testing ')' vs '}' detection at end of columns:")

    test1 = """
    pattern   { A, B, C )
    pattern   { A, B, C }
    { value1, value2, value3 )
    { value1, value2, $(MACRO) }
    """

    test1_file = Path("test1.tmp")
    with open(test1_file, 'w') as f:
        f.write(test1)

    result1 = engine.validate_substitution_file(str(test1_file))

    for issue in result1.issues:
        if 'BRACE' in issue.rule_id:
            print(f"  Line {issue.line_number}: {issue.message}")

    test1_file.unlink()

    # Test 2: Quote consistency
    print("\n2. Testing quote consistency rules:")

    test2 = """
file test.vdb
{
    pattern   { TAG, VALUE, DESC, EGU }
              { TAG1, 123, Test Description, mbar }
              { TAG2, "456", "Test Description", "mbar" }
              { TAG3, $(MACRO), Another Desc, "" }
}
"""

    test2_file = Path("test2.tmp")
    with open(test2_file, 'w') as f:
        f.write(test2)

    result2 = engine.validate_substitution_file(str(test2_file))

    for issue in result2.issues:
        if 'QUOTE' in issue.rule_id:
            print(f"  Line {issue.line_number}: {issue.message}")
            if issue.current_value and issue.suggested_value:
                print(f"    '{issue.current_value}' → '{issue.suggested_value}'")

    test2_file.unlink()

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    test_validation()