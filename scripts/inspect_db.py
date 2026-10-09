import sqlite3

conn = sqlite3.connect("data/orb_scanner.db")
cursor = conn.cursor()
for t in ["candles_1m", "candles_5m", "candles_15m"]:
    count = cursor.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    print(f"{t}: {count} rows")
    if count > 0:
        symbols = cursor.execute(f"SELECT DISTINCT symbol FROM {t}").fetchall()
        dates = cursor.execute(f"SELECT MIN(timestamp), MAX(timestamp) FROM {t}").fetchone()
        print(f"  Symbols: {[s[0] for s in symbols][:10]}")
        print(f"  Date range: {dates}")
