import json
from pathlib import Path
from validation_engine import Severity

SEVERITY_ORDER = {
    Severity.INFO.value: 1,
    Severity.WARNING.value: 2,
    Severity.CRITICAL.value: 3,
}


def write_json_report(payload: dict, output_path: str | None, min_severity: str | None = None):
    if min_severity and SEVERITY_ORDER.get(min_severity, 0) > 1:
        min_level = SEVERITY_ORDER[min_severity]
        payload["results"] = [
            result for result in payload.get("results", [])
            if any(
                SEVERITY_ORDER.get(issue.get("severity"), 0) >= min_level
                for issue in result.get("issues", [])
            )
        ]

    json_text = json.dumps(payload, indent=2)

    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_text + "\n")
    else:
        print(json_text)


def print_human_summary(results: list[dict]):
    for result in results:
        fp = result.get("file_path") or result.get("file") or "unknown"
        file_type = result.get("file_type", "unknown")

        print(f"\nFile: {fp}")
        print(f"  Type: {file_type}")

        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue

        if "passed" in result:
            print(f"  PASSED: {result['passed']}")

        if file_type == "template":
            print(f"  Records: {result.get('records', 0)}")
            print(f"  Macros: {len(result.get('macros', []))}")
            print(f"  Includes: {len(result.get('includes', []))}")

        issues = result.get("issues", [])

        print(f"  Total issues: {len(issues)}")

        critical_count = len(
            [i for i in issues if i.get("severity") == Severity.CRITICAL.value]
        )
        warning_count = len(
            [i for i in issues if i.get("severity") == Severity.WARNING.value]
        )

        print(f"  Critical: {critical_count}")
        print(f"  Warnings: {warning_count}")

        for issue in issues:
            print(f"   - {issue.get('severity')}: {issue.get('message')}")

