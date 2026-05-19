#!/usr/bin/env python3
"""
IOC Workflow Manager
====================
Handles preview, approval, and execution workflows for IOC configuration changes.

Author: SLAC Cryoplant Team
Date: 2024
"""

import os
import json
import difflib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import tempfile
import shutil

from ioc_validation_engine import ValidationEngine, ValidationResult, ValidationIssue, Severity
from ioc_backup_manager import BackupManager

class WorkflowStatus(Enum):
    """Status of a workflow execution"""
    PENDING = "pending"
    IN_PREVIEW = "in_preview"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"

class ChangeType(Enum):
    """Type of change being made"""
    FORMAT = "format"
    VALIDATION_FIX = "validation_fix"
    QUOTE_CORRECTION = "quote_correction"
    ARCHIVE_UPDATE = "archive_update"
    BUILD_FIX = "build_fix"
    MANUAL_EDIT = "manual_edit"
    BATCH_UPDATE = "batch_update"

@dataclass
class Change:
    """Represents a single change to be made"""
    file_path: str
    line_number: int
    original_value: str
    new_value: str
    change_type: ChangeType
    description: str
    auto_approved: bool = False

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'file_path': self.file_path,
            'line_number': self.line_number,
            'original_value': self.original_value,
            'new_value': self.new_value,
            'change_type': self.change_type.value,
            'description': self.description,
            'auto_approved': self.auto_approved
        }

@dataclass
class WorkflowExecution:
    """Represents a complete workflow execution"""
    workflow_id: str
    workflow_type: str
    status: WorkflowStatus
    created_at: datetime
    updated_at: datetime
    user: str
    files: List[str]
    changes: List[Change] = field(default_factory=list)
    validation_results: Dict[str, ValidationResult] = field(default_factory=dict)
    approval_info: Optional[Dict] = None
    execution_result: Optional[Dict] = None
    rollback_info: Optional[Dict] = None

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'workflow_id': self.workflow_id,
            'workflow_type': self.workflow_type,
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'user': self.user,
            'files': self.files,
            'changes': [c.to_dict() for c in self.changes],
            'approval_info': self.approval_info,
            'execution_result': self.execution_result,
            'rollback_info': self.rollback_info
        }

class WorkflowManager:
    """Manages end-to-end workflows for IOC configuration changes"""

    def __init__(self, backup_dir: str = None):
        """Initialize workflow manager"""
        self.validation_engine = ValidationEngine()
        self.backup_manager = BackupManager(backup_dir)
        self.workflows: Dict[str, WorkflowExecution] = {}
        self.workflow_history: List[WorkflowExecution] = []
        self.temp_dir = Path(tempfile.gettempdir()) / 'ioc_workflows'
        self.temp_dir.mkdir(exist_ok=True)

    def create_workflow(self, workflow_type: str, files: List[str],
                       user: str = None) -> str:
        """Create a new workflow execution"""
        import uuid
        workflow_id = str(uuid.uuid4())[:8]

        workflow = WorkflowExecution(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            status=WorkflowStatus.PENDING,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            user=user or os.environ.get('USER', 'unknown'),
            files=files
        )

        self.workflows[workflow_id] = workflow
        return workflow_id

    def analyze_files(self, workflow_id: str) -> Dict[str, Any]:
        """Analyze files and identify needed changes"""
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            return {'error': 'Workflow not found'}

        workflow.status = WorkflowStatus.IN_PREVIEW
        workflow.updated_at = datetime.now()

        analysis_results = {
            'workflow_id': workflow_id,
            'files_analyzed': len(workflow.files),
            'total_issues': 0,
            'critical_issues': 0,
            'warnings': 0,
            'suggestions': 0,
            'auto_fixable': 0,
            'file_results': {}
        }

        # Analyze each file
        for file_path in workflow.files:
            if not os.path.exists(file_path):
                analysis_results['file_results'][file_path] = {
                    'error': 'File not found'
                }
                continue

            # Run validation
            validation_result = self.validation_engine.validate_substitution_file(file_path)
            workflow.validation_results[file_path] = validation_result

            # Generate changes from validation results
            changes = self._generate_changes_from_validation(file_path, validation_result)
            workflow.changes.extend(changes)

            # Collect statistics
            analysis_results['total_issues'] += len(validation_result.issues)
            analysis_results['critical_issues'] += len(validation_result.get_issues_by_severity(Severity.CRITICAL))
            analysis_results['warnings'] += len(validation_result.get_issues_by_severity(Severity.WARNING))
            analysis_results['suggestions'] += len(validation_result.get_issues_by_severity(Severity.SUGGESTION))
            analysis_results['auto_fixable'] += sum(1 for i in validation_result.issues if i.auto_fixable)

            # Store per-file results
            analysis_results['file_results'][file_path] = {
                'passed': validation_result.passed,
                'total_issues': len(validation_result.issues),
                'statistics': validation_result.statistics
            }

        workflow.status = WorkflowStatus.AWAITING_APPROVAL
        return analysis_results

    def _generate_changes_from_validation(self, file_path: str,
                                         validation_result: ValidationResult) -> List[Change]:
        """Generate change objects from validation results"""
        changes = []

        for issue in validation_result.issues:
            if issue.auto_fixable and issue.suggested_value is not None:
                # Determine change type based on rule ID
                change_type = ChangeType.VALIDATION_FIX
                if 'QUOTE' in issue.rule_id:
                    change_type = ChangeType.QUOTE_CORRECTION
                elif 'FORMAT' in issue.rule_id:
                    change_type = ChangeType.FORMAT

                change = Change(
                    file_path=file_path,
                    line_number=issue.line_number,
                    original_value=issue.current_value or "",
                    new_value=issue.suggested_value,
                    change_type=change_type,
                    description=issue.message,
                    auto_approved=(issue.severity != Severity.CRITICAL)
                )
                changes.append(change)

        return changes

    def generate_preview(self, workflow_id: str,
                        selected_changes: Optional[List[int]] = None) -> Dict[str, Any]:
        """Generate preview of changes to be made"""
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            return {'error': 'Workflow not found'}

        # Filter changes if specific ones selected
        changes_to_apply = workflow.changes
        if selected_changes is not None:
            changes_to_apply = [workflow.changes[i] for i in selected_changes
                              if i < len(workflow.changes)]

        preview = {
            'workflow_id': workflow_id,
            'total_changes': len(changes_to_apply),
            'files_affected': set(),
            'file_previews': {}
        }

        # Group changes by file
        changes_by_file = {}
        for change in changes_to_apply:
            if change.file_path not in changes_by_file:
                changes_by_file[change.file_path] = []
            changes_by_file[change.file_path].append(change)
            preview['files_affected'].add(change.file_path)

        # Generate preview for each file
        for file_path, file_changes in changes_by_file.items():
            preview['file_previews'][file_path] = self._generate_file_preview(
                file_path, file_changes
            )

        preview['files_affected'] = list(preview['files_affected'])
        return preview

    def _generate_file_preview(self, file_path: str,
                              changes: List[Change]) -> Dict[str, Any]:
        """Generate preview for a single file"""
        try:
            with open(file_path, 'r') as f:
                original_lines = f.readlines()

            # Apply changes to create modified version
            modified_lines = original_lines.copy()

            # Sort changes by line number (reverse) to avoid index shifting
            changes.sort(key=lambda x: x.line_number, reverse=True)

            for change in changes:
                line_idx = change.line_number - 1
                if 0 <= line_idx < len(modified_lines):
                    line = modified_lines[line_idx]
                    modified_line = line.replace(change.original_value, change.new_value)
                    modified_lines[line_idx] = modified_line

            # Generate unified diff
            diff = difflib.unified_diff(
                original_lines,
                modified_lines,
                fromfile=f"Original: {Path(file_path).name}",
                tofile=f"Modified: {Path(file_path).name}",
                lineterm=''
            )

            # Generate side-by-side diff
            htmldiff = difflib.HtmlDiff()
            html_diff = htmldiff.make_table(
                original_lines,
                modified_lines,
                fromdesc='Original',
                todesc='Modified',
                context=True,
                numlines=3
            )

            return {
                'unified_diff': list(diff),
                'html_diff': html_diff,
                'changes_count': len(changes),
                'changes': [c.to_dict() for c in changes]
            }

        except Exception as e:
            return {'error': str(e)}

    def approve_workflow(self, workflow_id: str,
                        approver: str = None,
                        selected_changes: Optional[List[int]] = None,
                        comments: str = None) -> bool:
        """Approve a workflow for execution"""
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            return False

        if workflow.status != WorkflowStatus.AWAITING_APPROVAL:
            return False

        # Filter changes if specific ones selected
        if selected_changes is not None:
            workflow.changes = [workflow.changes[i] for i in selected_changes
                              if i < len(workflow.changes)]

        workflow.status = WorkflowStatus.APPROVED
        workflow.updated_at = datetime.now()
        workflow.approval_info = {
            'approver': approver or os.environ.get('USER', 'unknown'),
            'approved_at': datetime.now().isoformat(),
            'changes_approved': len(workflow.changes),
            'comments': comments
        }

        return True

    def reject_workflow(self, workflow_id: str,
                       reason: str = None) -> bool:
        """Reject a workflow"""
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            return False

        workflow.status = WorkflowStatus.REJECTED
        workflow.updated_at = datetime.now()
        workflow.approval_info = {
            'rejected_at': datetime.now().isoformat(),
            'reason': reason
        }

        return True

    def execute_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Execute an approved workflow"""
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            return {'error': 'Workflow not found'}

        if workflow.status != WorkflowStatus.APPROVED:
            return {'error': 'Workflow not approved'}

        workflow.status = WorkflowStatus.EXECUTING
        workflow.updated_at = datetime.now()

        execution_result = {
            'workflow_id': workflow_id,
            'started_at': datetime.now().isoformat(),
            'files_processed': [],
            'changes_applied': 0,
            'backups_created': [],
            'errors': []
        }

        try:
            # Group changes by file
            changes_by_file = {}
            for change in workflow.changes:
                if change.file_path not in changes_by_file:
                    changes_by_file[change.file_path] = []
                changes_by_file[change.file_path].append(change)

            # Process each file
            for file_path, file_changes in changes_by_file.items():
                try:
                    # Create backup before making changes
                    backup_metadata = self.backup_manager.create_backup(
                        file_path,
                        reason=f"Workflow {workflow_id}: {workflow.workflow_type}",
                        changes={'changes_count': len(file_changes)}
                    )

                    if backup_metadata:
                        execution_result['backups_created'].append(
                            backup_metadata.backup_path
                        )

                    # Apply changes
                    success = self._apply_changes_to_file(file_path, file_changes)

                    if success:
                        execution_result['files_processed'].append(file_path)
                        execution_result['changes_applied'] += len(file_changes)
                    else:
                        execution_result['errors'].append(
                            f"Failed to apply changes to {file_path}"
                        )

                except Exception as e:
                    execution_result['errors'].append(
                        f"Error processing {file_path}: {str(e)}"
                    )

            # Update workflow status
            if execution_result['errors']:
                workflow.status = WorkflowStatus.FAILED
            else:
                workflow.status = WorkflowStatus.COMPLETED

            workflow.updated_at = datetime.now()
            workflow.execution_result = execution_result
            execution_result['completed_at'] = datetime.now().isoformat()

            # Move to history
            self.workflow_history.append(workflow)

        except Exception as e:
            workflow.status = WorkflowStatus.FAILED
            execution_result['errors'].append(f"Workflow execution failed: {str(e)}")

        return execution_result

    def _apply_changes_to_file(self, file_path: str,
                              changes: List[Change]) -> bool:
        """Apply changes to a single file"""
        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()

            # Sort changes by line number (reverse) to avoid index shifting
            changes.sort(key=lambda x: x.line_number, reverse=True)

            for change in changes:
                line_idx = change.line_number - 1
                if 0 <= line_idx < len(lines):
                    line = lines[line_idx]
                    modified_line = line.replace(change.original_value, change.new_value)
                    lines[line_idx] = modified_line

            # Write modified content back to file
            with open(file_path, 'w') as f:
                f.writelines(lines)

            return True

        except Exception as e:
            print(f"Error applying changes to {file_path}: {e}")
            return False

    def rollback_workflow(self, workflow_id: str,
                         reason: str = None) -> Dict[str, Any]:
        """Rollback changes made by a workflow"""
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            # Check history
            for w in self.workflow_history:
                if w.workflow_id == workflow_id:
                    workflow = w
                    break

        if not workflow:
            return {'error': 'Workflow not found'}

        if not workflow.execution_result:
            return {'error': 'No execution result to rollback'}

        rollback_result = {
            'workflow_id': workflow_id,
            'started_at': datetime.now().isoformat(),
            'files_restored': [],
            'errors': []
        }

        # Restore from backups
        backups = workflow.execution_result.get('backups_created', [])
        for backup_path in backups:
            try:
                success = self.backup_manager.restore_backup(backup_path)
                if success:
                    rollback_result['files_restored'].append(backup_path)
                else:
                    rollback_result['errors'].append(
                        f"Failed to restore from {backup_path}"
                    )
            except Exception as e:
                rollback_result['errors'].append(
                    f"Error restoring {backup_path}: {str(e)}"
                )

        workflow.status = WorkflowStatus.ROLLED_BACK
        workflow.updated_at = datetime.now()
        workflow.rollback_info = {
            'rolled_back_at': datetime.now().isoformat(),
            'reason': reason,
            'result': rollback_result
        }

        rollback_result['completed_at'] = datetime.now().isoformat()
        return rollback_result

    def get_workflow_status(self, workflow_id: str) -> Optional[Dict]:
        """Get status of a workflow"""
        workflow = self.workflows.get(workflow_id)
        if workflow:
            return workflow.to_dict()

        # Check history
        for w in self.workflow_history:
            if w.workflow_id == workflow_id:
                return w.to_dict()

        return None

    def format_substitution_workflow(self, file_paths: List[str],
                                    user: str = None) -> str:
        """High-level workflow for formatting substitution files"""
        # Create workflow
        workflow_id = self.create_workflow('format_substitutions', file_paths, user)

        # Analyze files
        analysis = self.analyze_files(workflow_id)

        # Auto-approve non-critical changes
        workflow = self.workflows[workflow_id]
        auto_approved = []
        for i, change in enumerate(workflow.changes):
            if change.auto_approved:
                auto_approved.append(i)

        # If there are auto-approved changes, mark them
        if auto_approved:
            # This would typically be handled by UI, but we can simulate
            pass

        return workflow_id

    def batch_update_workflow(self, file_paths: List[str],
                            update_function: Callable,
                            user: str = None) -> str:
        """Workflow for batch updates across multiple files"""
        # Create workflow
        workflow_id = self.create_workflow('batch_update', file_paths, user)

        workflow = self.workflows[workflow_id]

        # Apply update function to generate changes
        for file_path in file_paths:
            try:
                with open(file_path, 'r') as f:
                    content = f.read()

                # Apply user function
                modified_content = update_function(content)

                if content != modified_content:
                    change = Change(
                        file_path=file_path,
                        line_number=0,
                        original_value=content[:100],  # First 100 chars for preview
                        new_value=modified_content[:100],
                        change_type=ChangeType.BATCH_UPDATE,
                        description="Batch update applied",
                        auto_approved=False
                    )
                    workflow.changes.append(change)

            except Exception as e:
                print(f"Error processing {file_path}: {e}")

        workflow.status = WorkflowStatus.AWAITING_APPROVAL
        return workflow_id

# Example usage
if __name__ == "__main__":
    # Create workflow manager
    manager = WorkflowManager()

    # Example: Format substitution files
    test_files = [
        "C:/Users/mkeenan/Development/SLAC/Cryoplant/CryoplantApp/Db/2kcb/2kcb_AIs.substitutions"
    ]

    if all(os.path.exists(f) for f in test_files):
        print("Creating formatting workflow...")
        workflow_id = manager.format_substitution_workflow(test_files)

        print(f"Workflow created: {workflow_id}")

        # Analyze files
        analysis = manager.analyze_files(workflow_id)
        print(f"\nAnalysis complete:")
        print(f"  Total issues: {analysis['total_issues']}")
        print(f"  Critical: {analysis['critical_issues']}")
        print(f"  Warnings: {analysis['warnings']}")
        print(f"  Auto-fixable: {analysis['auto_fixable']}")

        # Generate preview
        preview = manager.generate_preview(workflow_id)
        print(f"\nPreview generated:")
        print(f"  Files affected: {len(preview['files_affected'])}")
        print(f"  Total changes: {preview['total_changes']}")

        # Simulate approval
        if analysis['auto_fixable'] > 0:
            print("\nApproving workflow...")
            manager.approve_workflow(workflow_id, comments="Auto-approved fixable issues")

            # Execute
            print("Executing workflow...")
            result = manager.execute_workflow(workflow_id)
            print(f"\nExecution result:")
            print(f"  Files processed: {len(result['files_processed'])}")
            print(f"  Changes applied: {result['changes_applied']}")
            print(f"  Backups created: {len(result['backups_created'])}")

            if result['errors']:
                print(f"  Errors: {result['errors']}")