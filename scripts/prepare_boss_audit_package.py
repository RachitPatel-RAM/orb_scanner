import os
from pathlib import Path
import re
import shutil

def prepare_audit_package():
    out_dir = Path("audit_export")
    out_dir.mkdir(exist_ok=True)

    # 1. Backtest Script
    shutil.copy("scripts/simulate_compounding_5year.py", out_dir / "backtest_script.py")
    print(f"Copied backtest script to {out_dir / 'backtest_script.py'}")

    # 2. Redacted order_executor.py
    oe_path = Path("app/trading/order_executor.py")
    if oe_path.exists():
        with open(oe_path, "r", encoding="utf-8") as f:
            oe_code = f.read()
        # Redact any sensitive tokens/passwords
        oe_clean = re.sub(r'([A-Za-z0-9_-]{25,})', '[REDACTED_KEY]', oe_code)
        with open(out_dir / "order_executor_redacted.py", "w", encoding="utf-8") as f:
            f.write(oe_clean)
        print(f"Redacted order_executor saved to {out_dir / 'order_executor_redacted.py'}")

    # 3. Redacted option_finder.py
    of_path = Path("app/dhan/option_finder.py")
    if of_path.exists():
        with open(of_path, "r", encoding="utf-8") as f:
            of_code = f.read()
        of_clean = re.sub(r'([A-Za-z0-9_-]{25,})', '[REDACTED_KEY]', of_code)
        with open(out_dir / "option_finder_redacted.py", "w", encoding="utf-8") as f:
            f.write(of_clean)
        print(f"Redacted option_finder saved to {out_dir / 'option_finder_redacted.py'}")

    print("All audit files successfully packaged in audit_export/ directory.")

if __name__ == "__main__":
    prepare_audit_package()
