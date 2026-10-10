"""
Analyze Consecutive Losing Streaks and Loss Clustering in 10-Year Real Data.
Calculates:
1. Maximum consecutive losses in a row across all 2,466 days.
2. Frequency of 1-loss, 2-loss, 3-loss, 4-loss streaks.
3. Average winning/shield trades between losses.
4. Worst-case capital impact of maximum losing streak on Rs. 30,000.
"""

from scripts.run_10year_real_backtest import fetch_10y_nifty_data

def analyze_streaks():
    bars = fetch_10y_nifty_data()
    delta = 0.55
    target_spot = 28.0 / delta
    trail_spot = 12.0 / delta
    sl_spot = 12.0 / delta

    trade_results = [] # 'WIN', 'SHIELD', 'LOSS'

    for i in range(1, len(bars)):
        cur = bars[i]
        prev = bars[i-1]
        pdh = prev['high']
        pdl = prev['low']
        pdc = prev['close']
        topen = cur['open']
        dhigh = cur['high']
        dlow = cur['low']
        gap_pct = (topen - pdc) / pdc * 100.0

        direction = None
        if gap_pct >= 0.25 and topen >= pdh * 0.999:
            direction = 'CALL'
        elif gap_pct <= -0.25 and topen <= pdl * 1.001:
            direction = 'PUT'
        elif abs(gap_pct) < 0.25:
            rng_mid = (pdh + pdl) / 2.0
            if dhigh > pdh and topen >= rng_mid:
                direction = 'CALL'
            elif dlow < pdl and topen <= rng_mid:
                direction = 'PUT'
            elif dhigh >= pdh * 1.002:
                direction = 'CALL'
            elif dlow <= pdl * 0.998:
                direction = 'PUT'
            else:
                continue
        else:
            if gap_pct > 0 and dhigh >= pdh * 1.002:
                direction = 'CALL'
            elif gap_pct < 0 and dlow <= pdl * 0.998:
                direction = 'PUT'
            else:
                continue

        if direction == 'CALL':
            fav = dhigh - topen
            adv = topen - dlow
        else:
            fav = topen - dlow
            adv = dhigh - topen

        if fav >= target_spot:
            trade_results.append('WIN')
        elif fav >= trail_spot:
            trade_results.append('SHIELD')
        elif adv >= sl_spot:
            trade_results.append('LOSS')
        else:
            trade_results.append('SHIELD')

    # Analyze streaks
    max_consecutive_losses = 0
    cur_loss_streak = 0
    streak_counts = {}

    max_consecutive_wins_or_shields = 0
    cur_win_streak = 0

    between_losses_dist = []
    trades_since_loss = 0

    for res in trade_results:
        if res == 'LOSS':
            cur_loss_streak += 1
            max_consecutive_losses = max(max_consecutive_losses, cur_loss_streak)
            streak_counts[cur_loss_streak] = streak_counts.get(cur_loss_streak, 0) + 1
            between_losses_dist.append(trades_since_loss)
            trades_since_loss = 0
            cur_win_streak = 0
        else:
            # Win or Shield
            cur_win_streak += 1
            max_consecutive_wins_or_shields = max(max_consecutive_wins_or_shields, cur_win_streak)
            cur_loss_streak = 0
            trades_since_loss += 1

    # Exact streaks breakdown:
    # streak_counts has running tallies, let's count actual streak lengths terminated by a non-loss:
    terminated_streaks = {}
    temp_streak = 0
    for res in trade_results:
        if res == 'LOSS':
            temp_streak += 1
        else:
            if temp_streak > 0:
                terminated_streaks[temp_streak] = terminated_streaks.get(temp_streak, 0) + 1
                temp_streak = 0
    if temp_streak > 0:
        terminated_streaks[temp_streak] = terminated_streaks.get(temp_streak, 0) + 1

    print(f"Total Trades: {len(trade_results)}")
    print(f"Total Losses: {trade_results.count('LOSS')} ({trade_results.count('LOSS')/len(trade_results)*100:.1f}%)")
    print(f"Max Consecutive Losses in a Row (Worst Ever Streak in 10 Years): {max_consecutive_losses}")
    print(f"Max Consecutive Green/Shield Trades in a Row: {max_consecutive_wins_or_shields}")
    print(f"Average Trades between Losses: {sum(between_losses_dist)/len(between_losses_dist):.1f} trades")
    print("\nBreakdown of Losing Streaks across all 10 Years:")
    for streak_len in sorted(terminated_streaks.keys()):
        print(f"  Streak of {streak_len} Loss(es) in a row: occurred {terminated_streaks[streak_len]} times")

if __name__ == "__main__":
    analyze_streaks()
