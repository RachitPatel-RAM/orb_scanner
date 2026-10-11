import json

with open("data/scalp_audit_monthly.json", "r", encoding="utf-8") as f:
    d = json.load(f)

delta = 0.55
target_spot_pts = 28.0 / delta  # ~50.9 pts
sl_spot_pts = 12.0 / delta      # ~21.8 pts

print("| Date | 09:16 Signal | NIFTY Spot Entry | NIFTY Target Level | NIFTY Stop Loss | Day High | Day Low | Actual Spot Exit Level | Outcome | Net Realized P&L | Account Balance |")
print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :---: |")

for s in d["sessions"]:
    dt = s["date"]
    o = s["nifty_open"]
    h = s["nifty_high"]
    l = s["nifty_low"]
    dr = s["trade_direction"]
    out = s["outcome"]
    net = s["net_pnl"]
    bal = s["running_capital"]

    if dr == "NO_TRADE":
        sig = "NO TRADE"
        entry_s = "-"
        tgt_s = "-"
        sl_s = "-"
        exit_s = "-"
        out_lbl = "CHOP (Range Bound)"
        net_s = "Rs. 0.00"
    elif dr == "CALL":
        sig = "BUY CE (Long)"
        entry_s = f"{o:,.1f}"
        tgt = o + target_spot_pts
        sl = o - sl_spot_pts
        tgt_s = f"{tgt:,.1f}"
        sl_s = f"{sl:,.1f}"

        if "TARGET_HIT" in out:
            exit_s = f"{tgt:,.1f}"
            out_lbl = "TARGET HIT (+51 pts)"
            net_s = f"+Rs. {net:,.2f}"
        elif "COST_SHIELD" in out:
            shield_exit = o + 1.8
            exit_s = f"{shield_exit:,.1f}"
            out_lbl = "COST SHIELD (+2 pts)"
            net_s = f"+Rs. {net:,.2f}"
        elif "STOP_LOSS" in out:
            exit_s = f"{sl:,.1f}"
            out_lbl = "STOP LOSS (-22 pts)"
            net_s = f"-Rs. {abs(net):,.2f}"
    elif dr == "PUT":
        sig = "BUY PE (Short)"
        entry_s = f"{o:,.1f}"
        tgt = o - target_spot_pts
        sl = o + sl_spot_pts
        tgt_s = f"{tgt:,.1f}"
        sl_s = f"{sl:,.1f}"

        if "TARGET_HIT" in out:
            exit_s = f"{tgt:,.1f}"
            out_lbl = "TARGET HIT (-51 pts)"
            net_s = f"+Rs. {net:,.2f}"
        elif "COST_SHIELD" in out:
            shield_exit = o - 1.8
            exit_s = f"{shield_exit:,.1f}"
            out_lbl = "COST SHIELD (-2 pts)"
            net_s = f"+Rs. {net:,.2f}"
        elif "STOP_LOSS" in out:
            exit_s = f"{sl:,.1f}"
            out_lbl = "STOP LOSS (+22 pts)"
            net_s = f"-Rs. {abs(net):,.2f}"

    print(f"| {dt} | {sig} | {entry_s} | {tgt_s} | {sl_s} | {h:,.1f} | {l:,.1f} | {exit_s} | {out_lbl} | {net_s} | Rs. {bal:,.2f} |")
