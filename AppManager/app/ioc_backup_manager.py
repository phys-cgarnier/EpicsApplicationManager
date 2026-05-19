#!/usr/bin/env python3
"""
IOC Backup Management System
============================
Intelligent backup system with retention policies, compression,
and quick restore capabilities.

Author: SLAC Cryoplant Team
Date: 2024
"""

import os
import shutil
import json
import gzip
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import hashlib
import difflib

@dataclass
class BackupMetadata:
    """Metadata for a backup file"""
    original_path: str
    backup_path: str
    timestamp: datetime
    file_hash: str
    file_size: int
    reason: str = "Manual backup"
    user: Optional[str] = None
    changes_made: Optional[Dict] = None

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'original_path': self.original_path,
            'backup_path': self.backup_path,
            'timestamp': self.timestamp.isoformat(),
            'file_hash': self.file_hash,
            'file_size': self.file_size,
            'reason': self.reason,
            'user': self.user or os.environ.get('USER', 'unknown'),
            'changes_made': self.changes_made
        }

@dataclass
class RetentionPolicy:
    """Backup retention policy configuration"""
    daily_keep_days: int = 7
    weekly_keep_weeks: int = 4
    monthly_keep_months: int = 12
    max_versions_per_file: int = 10
    compress_after_days: int = 3
    archive_after_days: int = 30

    @classmethod
    def from_json(cls, json_path: str) -> 'RetentionPolicy':
        """Load policy from JSON file"""
        try:
            with open(json_path, 'r') as f:
                data = json.load(f)
            return cls(**data)
        except Exception:
            return cls()  # Return default policy if can't load

class BackupManager:
    """Manages backup creation, organization, and restoration"""

    def __init__(self, base_backup_dir: str = None, policy: RetentionPolicy = None):
        """Initialize backup manager"""
        if base_backup_dir is None:
            base_backup_dir = os.path.join(os.path.dirname(__file__), 'backups')

        self.base_backup_dir = Path(base_backup_dir)
        self.policy = policy or RetentionPolicy()
        self.manifest_file = self.base_backup_dir / 'manifest.json'

        # Create base backup directory if it doesn't exist
        self.base_backup_dir.mkdir(parents=True, exist_ok=True)

        # Load or create manifest
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> Dict:
        """Load backup manifest from disk"""
        if self.manifest_file.exists():
            try:
                with open(self.manifest_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {'backups': [], 'statistics': {}}

    def _save_manifest(self):
        """Save backup manifest to disk"""
        try:
            with open(self.manifest_file, 'w') as f:
                json.dump(self.manifest, f, indent=2, default=str)
        except Exception as e:
            print(f"Warning: Could not save manifest: {e}")

    def _calculate_file_hash(self, file_path: str) -> str:
        """Calculate SHA256 hash of a file"""
        sha256_hash = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception:
            return ""

    def _get_backup_path(self, original_path: str, timestamp: datetime = None) -> Path:
        """Generate backup path based on date hierarchy"""
        if timestamp is None:
            timestamp = datetime.now()

        # Create date-based directory structure
        date_path = self.base_backup_dir / str(timestamp.year) / f"{timestamp.month:02d}" / f"{timestamp.day:02d}"
        date_path.mkdir(parents=True, exist_ok=True)

        # Generate backup filename
        original_name = Path(original_path).name
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")
        backup_name = f"{original_name}.{timestamp_str}.backup"

        return date_path / backup_name

    def create_backup(self, file_path: str, reason: str = "Manual backup",
                     changes: Dict = None) -> Optional[BackupMetadata]:
        """Create a backup of a file"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                print(f"Error: File does not exist: {file_path}")
                return None

            # Calculate file hash to check if backup is needed
            current_hash = self._calculate_file_hash(str(file_path))

            # Check if file has changed since last backup
            if self._is_duplicate_backup(str(file_path), current_hash):
                print(f"Skipping backup - file unchanged: {file_path.name}")
                return self._get_latest_backup_metadata(str(file_path))

            # Generate backup path
            timestamp = datetime.now()
            backup_path = self._get_backup_path(str(file_path), timestamp)

            # Copy file to backup location
            shutil.copy2(file_path, backup_path)

            # Create metadata
            metadata = BackupMetadata(
                original_path=str(file_path),
                backup_path=str(backup_path),
                timestamp=timestamp,
                file_hash=current_hash,
                file_size=file_path.stat().st_size,
                reason=reason,
                user=os.environ.get('USER', 'unknown'),
                changes_made=changes
            )

            # Update manifest
            self.manifest['backups'].append(metadata.to_dict())
            self._save_manifest()

            # Apply retention policy
            self._apply_retention_policy()

            print(f"Backup created: {backup_path.name}")
            return metadata

        except Exception as e:
            print(f"Error creating backup: {e}")
            return None

    def _is_duplicate_backup(self, file_path: str, file_hash: str) -> bool:
        """Check if this would be a duplicate backup"""
        # Get most recent backup for this file
        file_backups = [b for b in self.manifest['backups']
                       if b['original_path'] == file_path]

        if not file_backups:
            return False

        # Sort by timestamp and get most recent
        file_backups.sort(key=lambda x: x['timestamp'], reverse=True)
        latest = file_backups[0]

        return latest.get('file_hash') == file_hash

    def _get_latest_backup_metadata(self, file_path: str) -> Optional[BackupMetadata]:
        """Get metadata for the most recent backup of a file"""
        file_backups = [b for b in self.manifest['backups']
                       if b['original_path'] == file_path]

        if not file_backups:
            return None

        file_backups.sort(key=lambda x: x['timestamp'], reverse=True)
        latest = file_backups[0]

        return BackupMetadata(
            original_path=latest['original_path'],
            backup_path=latest['backup_path'],
            timestamp=datetime.fromisoformat(latest['timestamp']),
            file_hash=latest['file_hash'],
            file_size=latest['file_size'],
            reason=latest.get('reason', 'Unknown'),
            user=latest.get('user'),
            changes_made=latest.get('changes_made')
        )

    def restore_backup(self, backup_path: str, target_path: str = None,
                      preview: bool = False) -> bool:
        """Restore a backup file"""
        try:
            backup_path = Path(backup_path)
            if not backup_path.exists():
                print(f"Error: Backup file does not exist: {backup_path}")
                return False

            # Find original path from manifest if not provided
            if target_path is None:
                for backup in self.manifest['backups']:
                    if backup['backup_path'] == str(backup_path):
                        target_path = backup['original_path']
                        break

            if target_path is None:
                print(f"Error: Could not determine target path for backup")
                return False

            target_path = Path(target_path)

            # Preview mode - show diff
            if preview:
                return self._preview_restore(backup_path, target_path)

            # Create backup of current file before restoring
            if target_path.exists():
                self.create_backup(str(target_path), reason="Pre-restore backup")

            # Restore the file
            shutil.copy2(backup_path, target_path)
            print(f"Restored: {backup_path} -> {target_path}")
            return True

        except Exception as e:
            print(f"Error restoring backup: {e}")
            return False

    def _preview_restore(self, backup_path: Path, target_path: Path) -> bool:
        """Preview what would be restored"""
        try:
            print(f"\nRestore Preview:")
            print(f"From: {backup_path}")
            print(f"To: {target_path}")

            if not target_path.exists():
                print("Target file does not exist - will be created")
                return True

            # Show diff between current and backup
            with open(target_path, 'r') as f:
                current_lines = f.readlines()
            with open(backup_path, 'r') as f:
                backup_lines = f.readlines()

            diff = difflib.unified_diff(
                current_lines,
                backup_lines,
                fromfile=f"Current: {target_path.name}",
                tofile=f"Backup: {backup_path.name}",
                lineterm=''
            )

            diff_output = list(diff)
            if diff_output:
                print("\nChanges that will be applied:")
                for line in diff_output[:50]:  # Show first 50 lines of diff
                    print(line)
                if len(diff_output) > 50:
                    print(f"... and {len(diff_output) - 50} more lines")
            else:
                print("No differences - files are identical")

            return True

        except Exception as e:
            print(f"Error generating preview: {e}")
            return False

    def list_backups(self, file_path: str = None, days: int = 7) -> List[BackupMetadata]:
        """List backups for a specific file or all files"""
        backups = []
        cutoff_date = datetime.now() - timedelta(days=days)

        for backup_dict in self.manifest['backups']:
            backup_date = datetime.fromisoformat(backup_dict['timestamp'])

            # Filter by date
            if backup_date < cutoff_date:
                continue

            # Filter by file if specified
            if file_path and backup_dict['original_path'] != file_path:
                continue

            backups.append(BackupMetadata(
                original_path=backup_dict['original_path'],
                backup_path=backup_dict['backup_path'],
                timestamp=backup_date,
                file_hash=backup_dict['file_hash'],
                file_size=backup_dict['file_size'],
                reason=backup_dict.get('reason', 'Unknown'),
                user=backup_dict.get('user'),
                changes_made=backup_dict.get('changes_made')
            ))

        # Sort by timestamp (newest first)
        backups.sort(key=lambda x: x.timestamp, reverse=True)
        return backups

    def _apply_retention_policy(self):
        """Apply retention policy to manage backup storage"""
        now = datetime.now()
        files_to_compress = []
        files_to_archive = []
        files_to_delete = []

        # Group backups by original file
        file_groups = {}
        for backup in self.manifest['backups']:
            orig_path = backup['original_path']
            if orig_path not in file_groups:
                file_groups[orig_path] = []
            file_groups[orig_path].append(backup)

        # Apply per-file policies
        for file_path, backups in file_groups.items():
            # Sort by timestamp (newest first)
            backups.sort(key=lambda x: x['timestamp'], reverse=True)

            # Keep only max versions per file
            if len(backups) > self.policy.max_versions_per_file:
                for backup in backups[self.policy.max_versions_per_file:]:
                    files_to_delete.append(backup['backup_path'])

            # Check age-based policies
            for backup in backups:
                backup_date = datetime.fromisoformat(backup['timestamp'])
                age_days = (now - backup_date).days

                # Compress old backups
                if (age_days > self.policy.compress_after_days and
                    not backup['backup_path'].endswith('.gz')):
                    files_to_compress.append(backup['backup_path'])

                # Archive very old backups
                if age_days > self.policy.archive_after_days:
                    files_to_archive.append(backup)

        # Execute retention actions
        self._compress_backups(files_to_compress)
        self._archive_old_backups(files_to_archive)
        self._delete_backups(files_to_delete)

    def _compress_backups(self, file_paths: List[str]):
        """Compress old backup files"""
        for file_path in file_paths:
            try:
                if os.path.exists(file_path):
                    # Compress with gzip
                    compressed_path = f"{file_path}.gz"
                    with open(file_path, 'rb') as f_in:
                        with gzip.open(compressed_path, 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)

                    # Remove original
                    os.remove(file_path)

                    # Update manifest
                    for backup in self.manifest['backups']:
                        if backup['backup_path'] == file_path:
                            backup['backup_path'] = compressed_path
                            break

                    print(f"Compressed backup: {Path(file_path).name}")
            except Exception as e:
                print(f"Error compressing {file_path}: {e}")

    def _archive_old_backups(self, backups: List[Dict]):
        """Archive old backups into tar archives by month"""
        # Group by year-month
        month_groups = {}
        for backup in backups:
            backup_date = datetime.fromisoformat(backup['timestamp'])
            month_key = f"{backup_date.year}-{backup_date.month:02d}"

            if month_key not in month_groups:
                month_groups[month_key] = []
            month_groups[month_key].append(backup)

        # Create monthly archives
        archive_dir = self.base_backup_dir / 'archives'
        archive_dir.mkdir(exist_ok=True)

        for month_key, month_backups in month_groups.items():
            archive_path = archive_dir / f"backup_{month_key}.tar.gz"

            try:
                with tarfile.open(archive_path, 'w:gz') as tar:
                    for backup in month_backups:
                        if os.path.exists(backup['backup_path']):
                            tar.add(backup['backup_path'],
                                  arcname=Path(backup['backup_path']).name)
                            # Remove original after archiving
                            os.remove(backup['backup_path'])

                print(f"Created archive: {archive_path.name}")

                # Update manifest
                for backup in month_backups:
                    backup['archived'] = True
                    backup['archive_path'] = str(archive_path)

            except Exception as e:
                print(f"Error creating archive {archive_path}: {e}")

    def _delete_backups(self, file_paths: List[str]):
        """Delete old backup files"""
        for file_path in file_paths:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"Deleted old backup: {Path(file_path).name}")

                # Remove from manifest
                self.manifest['backups'] = [b for b in self.manifest['backups']
                                           if b['backup_path'] != file_path]
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")

        self._save_manifest()

    def get_backup_statistics(self) -> Dict:
        """Get statistics about backups"""
        stats = {
            'total_backups': len(self.manifest['backups']),
            'total_size': 0,
            'files_backed_up': set(),
            'oldest_backup': None,
            'newest_backup': None,
            'backups_by_month': {},
            'compressed_count': 0,
            'archived_count': 0
        }

        for backup in self.manifest['backups']:
            # Count unique files
            stats['files_backed_up'].add(backup['original_path'])

            # Calculate total size
            if 'file_size' in backup:
                stats['total_size'] += backup['file_size']

            # Track dates
            backup_date = datetime.fromisoformat(backup['timestamp'])
            if stats['oldest_backup'] is None or backup_date < stats['oldest_backup']:
                stats['oldest_backup'] = backup_date
            if stats['newest_backup'] is None or backup_date > stats['newest_backup']:
                stats['newest_backup'] = backup_date

            # Count by month
            month_key = f"{backup_date.year}-{backup_date.month:02d}"
            if month_key not in stats['backups_by_month']:
                stats['backups_by_month'][month_key] = 0
            stats['backups_by_month'][month_key] += 1

            # Count compressed and archived
            if backup['backup_path'].endswith('.gz'):
                stats['compressed_count'] += 1
            if backup.get('archived', False):
                stats['archived_count'] += 1

        stats['files_backed_up'] = len(stats['files_backed_up'])
        stats['total_size_mb'] = round(stats['total_size'] / (1024 * 1024), 2)

        return stats

# Example usage and testing
if __name__ == "__main__":
    # Create backup manager
    manager = BackupManager()

    # Example: Create a backup
    test_file = "C:/Users/mkeenan/Development/SLAC/Cryoplant/CryoplantApp/Db/2kcb/2kcb_AIs.substitutions"
    if os.path.exists(test_file):
        print(f"Creating backup of: {test_file}")
        metadata = manager.create_backup(
            test_file,
            reason="Testing backup system",
            changes={'test': 'Added test change'}
        )

        if metadata:
            print(f"Backup created at: {metadata.backup_path}")

    # List recent backups
    print("\nRecent backups:")
    backups = manager.list_backups(days=30)
    for backup in backups[:5]:
        print(f"  - {Path(backup.backup_path).name} ({backup.reason})")

    # Show statistics
    stats = manager.get_backup_statistics()
    print(f"\nBackup Statistics:")
    print(f"  Total backups: {stats['total_backups']}")
    print(f"  Files backed up: {stats['files_backed_up']}")
    print(f"  Total size: {stats['total_size_mb']} MB")
    print(f"  Compressed: {stats['compressed_count']}")
    print(f"  Archived: {stats['archived_count']}")