"""
Option Contract Resolution and Real-Time Premium Pricing Engine for Index ORB Setups.

Resolves ATM strikes, weekly/monthly expiries, security IDs from Dhan Scrip Master,
fetches live option premium LTP via Dhan API, and calculates options-level SL & Target.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.config import DATA_DIR, logger, settings
from app.dhan.auth import auth
from app.storage.models import Direction


@dataclass
class OptionContractInfo:
    security_id: str
    underlying: str
    custom_symbol: str
    trading_symbol: str
    strike_price: float
    option_type: str  # "CE" or "PE"
    expiry_date: str
    lot_size: int
    exchange_segment: str  # "NSE_FNO" or "BSE_FNO"
    ltp: float
    stop_loss_premium: float
    target_premium: float
    margin_required: float


def is_valid_price(price: Any) -> bool:
    """Validates that a price quote is non-null, finite, and strictly positive (> 0.50)."""
    import math
    if price is None:
        return False
    try:
        val = float(price)
        if math.isnan(val) or math.isinf(val):
            return False
        return val > 0.5
    except (ValueError, TypeError):
        return False


class OptionFinder:
    """Finds ATM options contracts and queries real-time premium pricing from DhanHQ."""

    def __init__(self, scrip_csv_path: Optional[Path] = None):
        self.csv_path = scrip_csv_path or (DATA_DIR / "scrip_master.csv")
        self._opt_index: Dict[str, List[Dict[str, Any]]] = {}  # key: UNDERLYING_OPT -> list of contracts
        self._loaded = False

    def load_contracts(self) -> None:
        """Parses OPTIDX contracts from Dhan scrip master CSV."""
        if self._loaded or not self.csv_path.exists():
            return

        logger.info(f"Indexing OPTIDX contracts from {self.csv_path.name}...")
        count = 0
        try:
            with open(self.csv_path, mode="r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    inst_name = row.get("SEM_INSTRUMENT_NAME", "").strip().upper()
                    if inst_name not in ("OPTIDX", "OP"):
                        continue

                    exch = row.get("SEM_EXM_EXCH_ID", "").strip().upper()
                    seg = row.get("SEM_SEGMENT", "").strip().upper()
                    trading_sym = row.get("SEM_TRADING_SYMBOL", "").strip().upper()
                    custom_sym = row.get("SEM_CUSTOM_SYMBOL", "").strip()
                    sec_id = row.get("SEM_SMST_SECURITY_ID", "").strip()
                    opt_type = row.get("SEM_OPTION_TYPE", "").strip().upper()
                    expiry_str = row.get("SEM_EXPIRY_DATE", "").strip()

                    try:
                        strike = float(row.get("SEM_STRIKE_PRICE", 0.0))
                        lot = int(float(row.get("SEM_LOT_UNITS", 1.0)))
                    except ValueError:
                        continue

                    # Identify underlying
                    underlying = ""
                    if "NIFTY" in trading_sym and "BANK" not in trading_sym and "FIN" not in trading_sym and "MID" not in trading_sym:
                        underlying = "NIFTY"
                    elif "BANKNIFTY" in trading_sym or "NIFTY BANK" in custom_sym:
                        underlying = "BANKNIFTY"
                    elif "SENSEX" in trading_sym and "50" not in trading_sym:
                        underlying = "SENSEX"

                    if not underlying or strike <= 0 or not opt_type:
                        continue

                    key = f"{underlying}_{opt_type}"
                    if key not in self._opt_index:
                        self._opt_index[key] = []

                    self._opt_index[key].append({
                        "security_id": sec_id,
                        "underlying": underlying,
                        "trading_symbol": trading_sym,
                        "custom_symbol": custom_sym or trading_sym,
                        "strike_price": strike,
                        "option_type": opt_type,
                        "expiry_date": expiry_str,
                        "lot_size": lot,
                        "exchange": exch,
                        "exchange_segment": "NSE_FNO" if exch == "NSE" else "BSE_FNO",
                    })
                    count += 1
            self._loaded = True
            logger.info(f"Indexed {count} OPTIDX contracts for NIFTY, BANKNIFTY, SENSEX.")
        except Exception as e:
            logger.error(f"Error loading OPTIDX contracts: {e}")

    def get_atm_strike(self, underlying: str, spot_price: float) -> float:
        """Rounds index spot price to standard ATM strike."""
        u = underlying.strip().upper()
        if u == "NIFTY":
            return round(spot_price / 50.0) * 50.0
        elif u in ("BANKNIFTY", "SENSEX"):
            return round(spot_price / 100.0) * 100.0
        return round(spot_price / 50.0) * 50.0

    async def find_atm_contract(
        self,
        underlying: str,
        spot_price: float,
        direction: Direction,
        target_date: Optional[date] = None,
    ) -> Optional[OptionContractInfo]:
        """
        Finds the nearest-expiry ATM contract and fetches its real-time LTP from Dhan.
        Calculates option-specific Stop Loss and Target levels.
        """
        self.load_contracts()

        opt_type = "CE" if direction == Direction.LONG else "PE"
        key = f"{underlying.strip().upper()}_{opt_type}"
        candidates = self._opt_index.get(key, [])
        if not candidates:
            logger.warning(f"No contracts found for {key}")
            return None

        atm_strike = self.get_atm_strike(underlying, spot_price)
        cur_date_str = (target_date or date.today()).isoformat()

        # Filter by strike match and future expiry
        matched = []
        for c in candidates:
            if abs(c["strike_price"] - atm_strike) < 1.0:
                exp_date_part = c["expiry_date"].split(" ")[0]
                if exp_date_part >= cur_date_str:
                    matched.append(c)

        if not matched:
            # Fallback to closest strike if exact ATM not found
            for c in candidates:
                exp_date_part = c["expiry_date"].split(" ")[0]
                if exp_date_part >= cur_date_str and abs(c["strike_price"] - atm_strike) <= 100:
                    matched.append(c)

        if not matched:
            logger.warning(f"No active expiry found for {key} at strike {atm_strike}")
            return None

        # Sort by nearest expiry date
        matched.sort(key=lambda x: x["expiry_date"])

        # Expiry Day Theta Shield:
        # On expiry day, 0 DTE options suffer exponential time decay (theta burn) after 11:30 AM.
        # If today is expiry day and a next-week contract exists, roll over to the next weekly expiry
        # so trade maintains steady delta and intrinsic/extrinsic value without rapid time decay.
        from datetime import datetime
        now_dt = datetime.now()
        is_expiry_today = any(c["expiry_date"].split(" ")[0] == cur_date_str for c in matched)
        has_next_expiry = any(c["expiry_date"].split(" ")[0] > cur_date_str for c in matched)
        if is_expiry_today and has_next_expiry and (now_dt.hour > 11 or (now_dt.hour == 11 and now_dt.minute >= 30)):
            logger.info("🛡️ Expiry Day Theta Shield Active: Shifting to next weekly contract to protect capital from 0 DTE theta decay")
            next_contracts = [c for c in matched if c["expiry_date"].split(" ")[0] > cur_date_str]
            if next_contracts:
                matched = next_contracts

        best = matched[0]

        # Query live LTP from Dhan
        sec_id = best["security_id"]
        exch_seg = best["exchange_segment"]
        ltp = await self.fetch_option_ltp(sec_id, exch_seg)

        # Reject contract if live LTP is unavailable, non-finite (NaN/inf), or <= 0.5
        if not is_valid_price(ltp):
            logger.warning(
                f"Live quote invalid or unavailable for {best['trading_symbol']} (sec_id={sec_id}, ltp={ltp}). "
                f"Aborting contract lookup to prevent unverified paper entry."
            )
            return None

        # Calculate Options SL (25% risk) and 1:2 R:R Target (+50% gain)
        sl_premium = round(max(5.0, ltp * 0.75), 2)  # 25% max option risk
        risk_pts = round(ltp - sl_premium, 2)
        target_premium = round(ltp + (risk_pts * 2.0), 2)  # 1:2 Risk to Reward
        margin_required = round(ltp * best["lot_size"], 2)

        return OptionContractInfo(
            security_id=sec_id,
            underlying=underlying.upper(),
            custom_symbol=best["custom_symbol"],
            trading_symbol=best["trading_symbol"],
            strike_price=atm_strike,
            option_type=opt_type,
            expiry_date=best["expiry_date"].split(" ")[0],
            lot_size=best["lot_size"],
            exchange_segment=exch_seg,
            ltp=ltp,
            stop_loss_premium=sl_premium,
            target_premium=target_premium,
            margin_required=margin_required,
        )

    async def fetch_option_ltp(self, security_id: str, exchange_segment: str = "NSE_FNO") -> Optional[float]:
        """Queries Dhan Marketfeed LTP for an option contract. Returns None on failure or invalid price."""
        url = "https://api.dhan.co/v2/marketfeed/ltp"
        headers = auth.get_headers()
        payload = {exchange_segment: [int(security_id)]}

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get(exchange_segment, {})
                rec = data.get(str(security_id)) or data.get(int(security_id))
                if rec and "last_price" in rec:
                    raw_val = rec["last_price"]
                    if is_valid_price(raw_val):
                        return float(raw_val)
                    return None
        except Exception as e:
            logger.debug(f"Error fetching option LTP for {security_id}: {e}")
        return None
        return 0.0


option_finder = OptionFinder()
