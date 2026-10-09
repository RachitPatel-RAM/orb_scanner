import os
import zipfile
from pathlib import Path

BASE_DIR = Path(r"D:\micro tool\paper_lab")
ZIP_OUT = BASE_DIR / "audit_package_revised.zip"

FILES_TO_ADD = [
    ("app/strategies/orb.py", "app/strategies/orb.py"),
    ("app/trading/order_executor.py", "app/trading/order_executor.py"),
    ("app/dhan/option_finder.py", "app/dhan/option_finder.py"),
    ("app/notifications/telegram.py", "app/notifications/telegram.py"),
    ("app/market/session.py", "app/market/session.py"),
    ("config/strategy.yaml", "config/strategy.yaml"),
    ("tests/test_audit_validation.py", "tests/test_audit_validation.py"),
    ("audit_export/CHANGES_AND_DIFF.md", "CHANGES_AND_DIFF.md"),
    ("audit_export/PYTEST_TEST_RUN_OUTPUT.txt", "PYTEST_TEST_RUN_OUTPUT.txt"),
    ("audit_export/FULL_PYTEST_OUTPUT.txt", "FULL_PYTEST_OUTPUT.txt"),
]

if ZIP_OUT.exists():
    ZIP_OUT.unlink()

with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for src_rel, arc_name in FILES_TO_ADD:
        src_path = BASE_DIR / src_rel
        if src_path.exists():
            zf.write(src_path, arcname=arc_name)
            print(f"Added {src_rel} -> {arc_name}")
        else:
            print(f"WARNING: Missing {src_rel}")

print(f"\nCreated {ZIP_OUT} (Size: {ZIP_OUT.stat().st_size} bytes)")
