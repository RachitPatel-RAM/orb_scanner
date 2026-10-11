import json

with open("data/september_real_audit.json", "r", encoding="utf-8") as f:
    trades = json.load(f)

with open("data/september_audit_table.md", "w", encoding="utf-8") as out:
    out.write("| Date | Index | Option | Lots | Qty | Result Status | Net P&L | Ending Balance | Trade Notes |\n")
    out.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
    for t in trades:
        out.write(f"| **{t['date']}** | {t['index']} | {t['type']} | {t['lots']} | {t['qty']} | {t['status']} | **{t['net_pnl']:+,.2f}** | **₹{t['end_balance']:,.2f}** | {t['desc']} |\n")

print("Successfully written to data/september_audit_table.md")
