import json

with open("data/scalp_audit_monthly.json", "r", encoding="utf-8") as f:
    d = json.load(f)

print("| Date | NIFTY Open | Day High | Day Low | 09:16 Signal | Contract | Entry | Exit | Points | Trade Outcome | Net Realized P&L | Running Balance |")
print("| :--- | :---: | :---: | :---: | :---: | :--- | :---: | :---: | :---: | :--- | :---: | :---: |")

for s in d["sessions"]:
    dt = s["date"]
    o = f"{s['nifty_open']:,.1f}"
    h = f"{s['nifty_high']:,.1f}"
    l = f"{s['nifty_low']:,.1f}"
    dr = s["trade_direction"]
    out = s["outcome"]
    net = s["net_pnl"]
    bal = f"Rs.{s['running_capital']:,.2f}"

    if dr == "NO_TRADE":
        sig = "NO TRADE"
        contract = "-"
        entry = "-"
        exit_p = "-"
        pts = "0.0"
        pnl_str = "Rs. 0.00"
    else:
        strike = int(round(s["nifty_open"] / 50.0) * 50)
        opt_type = "CE" if dr == "CALL" else "PE"
        sig = "BUY CE" if dr == "CALL" else "BUY PE"
        contract = f"NIFTY {strike} {opt_type}"
        entry_val = 140.0
        entry = f"Rs.{entry_val:.1f}"

        if "TARGET_HIT" in out:
            exit_val = entry_val + 28.0
            exit_p = f"Rs.{exit_val:.1f}"
            pts = "+28.0 pts"
            pnl_str = f"+Rs.{net:,.2f}"
        elif "COST_SHIELD" in out:
            exit_val = entry_val + 1.0
            exit_p = f"Rs.{exit_val:.1f}"
            pts = "+1.0 pt"
            pnl_str = f"+Rs.{net:,.2f}"
        elif "STOP_LOSS" in out:
            exit_val = entry_val - 12.0
            exit_p = f"Rs.{exit_val:.1f}"
            pts = "-12.0 pts"
            pnl_str = f"-Rs.{abs(net):,.2f}"

    print(f"| {dt} | {o} | {h} | {l} | {sig} | {contract} | {entry} | {exit_p} | {pts} | {out} | {pnl_str} | {bal} |")
