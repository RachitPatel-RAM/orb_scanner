import json
from pathlib import Path
import shutil
import zipfile
import httpx

def build_zip():
    out_dir = Path("audit_export")
    out_dir.mkdir(exist_ok=True)

    # 1. Copy export_audit_trade_log.py
    shutil.copy("scripts/export_audit_trade_log.py", out_dir / "export_audit_trade_log.py")

    # 2. Fetch and save raw input sample (50 candles of Yahoo Finance Nifty daily data)
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=5y"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = httpx.get(url, headers=headers)
    raw_data = resp.json()["chart"]["result"][0]
    
    # Save first 50 sample records with timestamps and OHLCV
    sample_records = []
    timestamps = raw_data["timestamp"][:50]
    q = raw_data["indicators"]["quote"][0]
    for i in range(len(timestamps)):
        sample_records.append({
            "timestamp": timestamps[i],
            "open": q["open"][i],
            "high": q["high"][i],
            "low": q["low"][i],
            "close": q["close"][i],
            "volume": q["volume"][i],
        })
    with open(out_dir / "raw_nifty_daily_sample.json", "w", encoding="utf-8") as f:
        json.dump(sample_records, f, indent=2)

    # 3. Create audit_package.zip containing all files in audit_export
    zip_path = Path("audit_package.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file in out_dir.iterdir():
            if file.is_file():
                zipf.write(file, arcname=file.name)

    print(f"audit_package.zip created successfully ({zip_path.stat().st_size} bytes)")
    print("Files included:")
    with zipfile.ZipFile(zip_path, "r") as zipf:
        for info in zipf.infolist():
            print(f"  • {info.filename} ({info.file_size} bytes)")

if __name__ == "__main__":
    build_zip()
