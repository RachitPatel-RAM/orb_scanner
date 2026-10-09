import os
import zipfile
from pathlib import Path

BASE_DIR = Path(r"D:\micro tool\paper_lab")

# 1. Build audit_package_complete.zip containing the entire runnable project
COMPLETE_ZIP = BASE_DIR / "audit_package_complete.zip"
REVISED_ZIP = BASE_DIR / "audit_package_revised.zip"

EXCLUDE_DIRS = {
    ".venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    ".vscode",
}

EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".log",
}

def should_include(rel_path: Path) -> bool:
    parts = rel_path.parts
    for p in parts:
        if p in EXCLUDE_DIRS:
            return False
    if rel_path.suffix in EXCLUDE_EXTENSIONS:
        return False
    # Avoid including the zip files inside themselves
    if rel_path.name in ("audit_package_complete.zip", "audit_package_revised.zip", "audit_package.zip"):
        return False
    return True

# Build complete runnable project zip
print("Building audit_package_complete.zip...")
with zipfile.ZipFile(COMPLETE_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            file_path = Path(root) / f
            rel_path = file_path.relative_to(BASE_DIR)
            if should_include(rel_path):
                zf.write(file_path, arcname=str(rel_path).replace("\\", "/"))

print(f"Created {COMPLETE_ZIP} ({COMPLETE_ZIP.stat().st_size:,} bytes)")

# 2. Build audit_package_revised.zip with core updated files & audit artifacts
print("\nBuilding audit_package_revised.zip...")
REVISED_FILES = [
    "app/strategies/orb.py",
    "app/trading/order_executor.py",
    "app/trading/paper_tracker.py",
    "app/storage/database.py",
    "app/storage/models.py",
    "app/dhan/option_finder.py",
    "app/notifications/telegram.py",
    "app/market/session.py",
    "config/strategy.yaml",
    "tests/test_audit_validation.py",
    "tests/test_paper_tracker.py",
    "audit_export/AUDIT_VERIFICATION_REPORT.md",
    "audit_export/PYTEST_TEST_RUN_OUTPUT.txt",
    "requirements.txt",
    "pytest.ini",
    ".env.example",
    "README.md",
]

with zipfile.ZipFile(REVISED_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
    for rel_f in REVISED_FILES:
        f_path = BASE_DIR / rel_f
        if f_path.exists():
            zf.write(f_path, arcname=rel_f)
            print(f"  Added {rel_f}")
        else:
            print(f"  WARNING: Missing {rel_f}")

print(f"Created {REVISED_ZIP} ({REVISED_ZIP.stat().st_size:,} bytes)")
