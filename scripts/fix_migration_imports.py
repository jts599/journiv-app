#!/usr/bin/env python3
"""
Fix migration files by replacing sqlmodel.sql.sqltypes.AutoString with sa.String
and ensuring proper imports.
"""
import os
import re
from pathlib import Path


def fix_migration_file(file_path: Path) -> bool:
    """Fix a single migration file."""
    print(f"Processing {file_path}")

    with open(file_path, 'r') as f:
        content = f.read()

    # Check if file needs fixing
    if 'sqlmodel.sql.sqltypes.AutoString' not in content:
        print(f"  ✓ No AutoString found, skipping")
        return False

    # Add sqlmodel import if not present
    if 'import sqlmodel' not in content:
        # Find the import section and add sqlmodel import
        import_pattern = r'(from alembic import op\nimport sqlalchemy as sa)'
        replacement = r'\1\nimport sqlmodel'
        content = re.sub(import_pattern, replacement, content)
        print(f"  ✓ Added sqlmodel import")

    # Replace sqlmodel.sql.sqltypes.AutoString with sa.String
    # Handle AutoString with length parameter
    content = re.sub(
        r'sqlmodel\.sql\.sqltypes\.AutoString\(length=(\d+)\)',
        r'sa.String(length=\1)',
        content
    )

    # Handle AutoString without length parameter
    content = re.sub(
        r'sqlmodel\.sql\.sqltypes\.AutoString\(\)',
        r'sa.String()',
        content
    )

    # Write the fixed content back
    with open(file_path, 'w') as f:
        f.write(content)

    print(f"  ✓ Fixed AutoString references")
    return True


def main():
    """Fix all migration files in the versions directory."""
    versions_dir = Path("alembic/versions")

    if not versions_dir.exists():
        print(f"Error: {versions_dir} does not exist")
        return 1

    fixed_count = 0
    for file_path in versions_dir.glob("*.py"):
        if file_path.name == "__init__.py":
            continue

        if fix_migration_file(file_path):
            fixed_count += 1

    print(f"\n✓ Fixed {fixed_count} migration files")
    return 0


if __name__ == "__main__":
    exit(main())

