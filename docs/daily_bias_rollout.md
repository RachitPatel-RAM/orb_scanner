# BornBull Daily Bias & Liquidity Context: Operations & Rollout Runbook

This runbook guides the deployment, verification, shadow operation, and safe promotion of the Daily Bias Engine and Intraday Liquidity Context veto for the Dhan/Telegram ORB scanner application.

---

## 1. Safety & Architecture Overview

All daily bias and context components have been engineered behind **safe, isolated feature flags**:

| Setting | Default | Description |
| :--- | :--- | :--- |
| `BIAS_GATE_MODE` | `SHADOW` | `OFF` (baseline only), `SHADOW` (evaluates & logs without altering baseline), `STRICT` (enforces gates) |
| `CHANNEL_PUBLISHING_ENABLED` | `false` | When `false`, suppresses all external Telegram broadcasts (dry-run mode) |
| `LIVE_ORDER_ENABLED` | `false` | When `false`, order execution operates in pure paper/simulation mode |
| `SESSION_CONTEXT_ENABLED` | `false` | Session-range adapter disabled initially for Indian cash/index sessions |
| `ALLOW_NEUTRAL_INTRADAY_FALLBACK` | `false` | Enforces conservative blocking of neutral daily bias days |
| `DAILY_REFERENCE_MODE` | `TWO_COMPLETED_SESSIONS` | Deterministic two-session mode ($C_{D-1} > H_{D-2} \rightarrow \text{BULLISH}$) |
| `DAILY_BIAS_TIME` | `09:05` | Morning bias snapshot computation time (IST) |
| `BIAS_RULE_VERSION` | `v1.0` | Auditable version identifier |

---

## 2. Token Revocation & Capability Preflight

> [!CAUTION]
> **Secret Hygiene:** If any Telegram Bot Token was previously posted into logs, it **MUST be revoked immediately** in Telegram via `@BotFather` (`/revoke`). Never reuse a leaked token or commit it to Git.

### Step 2.1: Configure Replacement Secrets
Update the local or server `.env` file with the replacement token:
```ini
TELEGRAM_BOT_TOKEN="<NEW_TOKEN_FROM_BOTFATHER>"
TELEGRAM_CHANNEL_ID="@bornbulltrade"
TELEGRAM_VIP_CHANNEL_ID="-100xxxxxxxxxx"
TELEGRAM_ADMIN_ID="<ADMIN_TELEGRAM_USER_ID>"
```

### Step 2.2: Run Read-Only Capability Preflight
Run the preflight tool to test permissions without sending messages:
```bash
python scripts/telegram_preflight.py
```
This tool verifies:
1. `getMe`: Bot identity and active connection.
2. `getChat`: Channel existence and numeric IDs for Public and VIP channels.
3. `getChatMember`: Confirms administrator status and `can_post_messages` permission.
All tokens are masked automatically in console logs (`1234****:****`).

---

## 3. Database Schema Verification

The SQLite schema automatically applies needed tables in WAL mode on application boot:
- `daily_bias_snapshots`: Stores morning 09:05 immutable snapshots per symbol/session.
- `liquidity_context_events`: Stores confirmed 60-m swing high/low sweeps and invalidations.
- `bias_gate_decisions`: Auditable record of all gate decisions (`PASS_BIAS`, `BLOCK_OPPOSITE_DAILY`, `BLOCK_NEUTRAL`, `BLOCK_CONTEXT_CONFLICT`).

To inspect or verify tables:
```bash
sqlite3 data/orb_scanner.db "SELECT count(*) FROM daily_bias_snapshots;"
```

---

## 4. Shadow Mode Deployment & Monitoring

### Step 4.1: Recommended Shadow Configuration (`.env`)
```ini
BIAS_GATE_MODE=SHADOW
CHANNEL_PUBLISHING_ENABLED=true      # Enable broadcast to Telegram channels
LIVE_ORDER_ENABLED=false             # Keep false for paper execution validation
```

### Step 4.2: Starting the Service
On the production server (`/home/patelram5002/paper_lab`):
```bash
# Verify git status and checkout branch
git checkout feature/daily-bias-engine

# Restart the service
sudo systemctl restart orb-scanner.service

# Check service status and live logs
sudo systemctl status orb-scanner.service
sudo journalctl -u orb-scanner.service -f
```

### Step 4.3: Telegram Interactive Verification
In Telegram chat with `@Directionalertbot`:
1. **Send `/health`**: Returns redacted system telemetry (uptime, active universe count, database health, Dhan API status, and gate mode).
2. **Send `/bias NIFTY`**: Returns the current session's daily bias snapshot, prior close, reference high/low, and permitted entry direction.

---

## 5. Promotion to Strict Mode

Once the shadow phase demonstrates consistent, auditable behavior and the owner verifies that false breakout losses are effectively avoided without unintended side effects:

1. Update `.env`:
   ```ini
   BIAS_GATE_MODE=STRICT
   ```
2. Restart service:
   ```bash
   sudo systemctl restart orb-scanner.service
   ```
3. In `STRICT` mode:
   - Bullish setups on Bearish/Neutral days will be blocked (`BLOCK_OPPOSITE_DAILY`, `BLOCK_NEUTRAL`).
   - Conflicted liquidity sweeps will pause new entries (`BLOCK_CONTEXT_CONFLICT`).
   - Every block reason is logged in `bias_gate_decisions`.

---

## 6. Immediate Rollback Plan

If any unexpected behavior, network failure, or strategy discrepancy occurs:

### Immediate Soft Rollback (Zero Downtime)
Change the gate mode in `.env`:
```ini
BIAS_GATE_MODE=OFF
```
Restart the service (`sudo systemctl restart orb-scanner.service`).
The application immediately resumes 100% baseline ORB operation while preserving all historical logs.

### Hard Code Rollback
```bash
git checkout main
sudo systemctl restart orb-scanner.service
```

---

## 7. Regulatory & Compliance Notice

> [!IMPORTANT]
> **SEBI Research Analyst Compliance Advisory:**
> Under SEBI's Research Analysts Master Circular (February 2026), automated technical analysis alerts that identify specific security entry, exit, or stop-loss points for paid subscribers may fall under security-specific investment recommendations. 
> 
> A general disclaimer or configuration flag does not replace formal regulatory authorization. Commercial dissemination to paid VIP subscribers should be reviewed by qualified legal counsel. In engineering templates, all unsupported claims of "guaranteed profit", "zero risk", "sure-shot calls", and "100% loss-free strategies" have been strictly removed.
