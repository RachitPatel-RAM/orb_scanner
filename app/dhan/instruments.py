"""
Dhan Instrument Master Downloader, Parser, and Universe Resolver.

Caches the DhanHQ official scrip master CSV, builds bi-directional mappings
(symbol <-> security_id), and resolves configured universes (Nifty 50, custom, etc.).
"""

from __future__ import annotations

import csv
import io
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import httpx

from app.config import UniverseConfig, logger, settings
from app.storage.database import db


@dataclass
class InstrumentInfo:
    security_id: str
    symbol: str
    display_name: str
    exchange_segment: str
    instrument_type: str
    lot_size: int = 1
    tick_size: float = 0.05


INDEX_DEFS: List[Tuple[str, str, str, str, str, int, float]] = [
    ("13", "NIFTY", "Nifty 50", "NSE", "IDX_I", 75, 0.05),
    ("25", "BANKNIFTY", "Nifty Bank", "NSE", "IDX_I", 30, 0.05),
    ("51", "SENSEX", "Sensex", "BSE", "IDX_I", 20, 0.05),
]


class InstrumentManager:
    """Manages the download, local caching, parsing, and resolution of Dhan instruments."""

    def __init__(self, config: Optional[UniverseConfig] = None):
        self.config = config or settings.universe
        self.cache_path = Path(self.config.local_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # In-memory lookups
        self.sec_id_to_symbol: Dict[str, str] = {}
        self.symbol_to_sec_id: Dict[str, str] = {}
        self.instruments_by_id: Dict[str, InstrumentInfo] = {}
        self.fno_symbols: Set[str] = set()
        self.fno_lot_sizes: Dict[str, int] = {}

        # Register core index definitions
        for sec_id, sym, d_name, exch, seg, lot, tick in INDEX_DEFS:
            inst = InstrumentInfo(
                security_id=sec_id,
                symbol=sym,
                display_name=d_name,
                exchange_segment=seg,
                instrument_type="INDEX",
                lot_size=lot,
                tick_size=tick,
            )
            self.instruments_by_id[sec_id] = inst
            self.sec_id_to_symbol[sec_id] = sym
            self.symbol_to_sec_id[sym] = sec_id
        # Aliases for robust resolution
        self.symbol_to_sec_id["NIFTY 50"] = "13"
        self.symbol_to_sec_id["NIFTY50"] = "13"
        self.symbol_to_sec_id["BANK NIFTY"] = "25"
        self.symbol_to_sec_id["NIFTY BANK"] = "25"
        self.symbol_to_sec_id["BSE SENSEX"] = "51"

    def is_cache_valid(self) -> bool:
        """Checks if local cached CSV exists and is within refresh interval."""
        if not self.cache_path.exists():
            return False
        mtime = datetime.fromtimestamp(self.cache_path.stat().st_mtime)
        return datetime.now() - mtime < timedelta(days=self.config.refresh_days)

    async def download_master(self, force: bool = False) -> bool:
        """
        Downloads official Dhan Scrip Master CSV from images.dhan.co.
        Saves to local cache path with streaming support.
        """
        if not force and self.is_cache_valid():
            logger.info(f"Using cached instrument master at {self.cache_path}")
            return True

        url = self.config.master_file_url
        logger.info(f"Downloading scrip master from {url}...")

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    with open(self.cache_path, "wb") as f:
                        f.write(resp.content)
                    logger.info(f"Scrip master downloaded successfully ({len(resp.content)} bytes).")
                    return True
                else:
                    logger.error(f"Failed to download scrip master: HTTP {resp.status_code}")
                    return False
        except Exception as e:
            logger.error(f"Exception downloading scrip master: {e}")
            return False

    def load_and_parse(self) -> None:
        """
        Parses cached CSV and indexes NSE Equity instruments.
        Supports standard Dhan column naming conventions:
        - SEM_SMST_SECURITY_ID / SecurityId
        - SEM_TRADING_SYMBOL / TradingSymbol
        - SEM_CUSTOM_SYMBOL / CustomSymbol
        - SEM_EXM_EXCH_ID / Exchange
        - SEM_INSTRUMENT_NAME / Instrument
        """
        if not self.cache_path.exists():
            logger.warning(f"Master file not found at {self.cache_path}. Run download first.")
            return

        count = 0
        bulk_items = []
        with open(self.cache_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalize column keys
                r = {k.strip().upper(): v.strip() for k, v in row.items() if k}

                exch = r.get("SEM_EXM_EXCH_ID", r.get("EXCHANGE", ""))
                inst_type = r.get("SEM_INSTRUMENT_NAME", r.get("INSTRUMENT", ""))

                if exch == "NSE" and inst_type in ("OPTSTK", "FUTSTK"):
                    c_sym = r.get("SEM_CUSTOM_SYMBOL", r.get("SEM_TRADING_SYMBOL", ""))
                    parts = c_sym.split()
                    if parts:
                        root = parts[0]
                        self.fno_symbols.add(root)
                        lot_str = r.get("SEM_LOT_UNITS", r.get("LOT_SIZE", ""))
                        if lot_str and root not in self.fno_lot_sizes:
                            try:
                                l_val = int(float(lot_str))
                                if l_val > 0:
                                    self.fno_lot_sizes[root] = l_val
                            except (ValueError, TypeError):
                                pass

                # Filter for NSE Equity by default
                if exch != "NSE" or (inst_type and inst_type != "EQUITY"):
                    continue

                sec_id = r.get("SEM_SMST_SECURITY_ID", r.get("SECURITY_ID", r.get("SECURITYID", "")))
                trading_sym = r.get("SEM_TRADING_SYMBOL", r.get("TRADING_SYMBOL", ""))
                custom_sym = r.get("SEM_CUSTOM_SYMBOL", r.get("CUSTOM_SYMBOL", ""))

                if not sec_id or not trading_sym:
                    continue

                # Strip '-EQ' suffix from trading symbol for standard symbol matching
                clean_symbol = trading_sym
                if clean_symbol.endswith("-EQ"):
                    clean_symbol = clean_symbol[:-3]

                lot_str = r.get("SEM_LOT_UNITS", r.get("LOT_SIZE", "1"))
                tick_str = r.get("SEM_TICK_SIZE", r.get("TICK_SIZE", "0.05"))

                try:
                    lot_size = int(float(lot_str)) if lot_str else 1
                except ValueError:
                    lot_size = 1

                try:
                    tick_size = float(tick_str) if tick_str else 0.05
                except ValueError:
                    tick_size = 0.05

                info = InstrumentInfo(
                    security_id=sec_id,
                    symbol=clean_symbol,
                    display_name=custom_sym or trading_sym,
                    exchange_segment="NSE_EQ",
                    instrument_type="EQUITY",
                    lot_size=lot_size,
                    tick_size=tick_size,
                )

                self.instruments_by_id[sec_id] = info
                self.sec_id_to_symbol[sec_id] = clean_symbol
                self.symbol_to_sec_id[clean_symbol] = sec_id
                self.symbol_to_sec_id[trading_sym] = sec_id

                bulk_items.append((
                    sec_id,
                    clean_symbol,
                    info.display_name,
                    info.exchange_segment,
                    info.instrument_type,
                    lot_size,
                    tick_size,
                ))
                count += 1

        if bulk_items:
            db.save_instruments_bulk(bulk_items)

        for sec_id, sym, d_name, exch, seg, lot, tick in INDEX_DEFS:
            inst = InstrumentInfo(
                security_id=sec_id,
                symbol=sym,
                display_name=d_name,
                exchange_segment=seg,
                instrument_type="INDEX",
                lot_size=lot,
                tick_size=tick,
            )
            self.instruments_by_id[sec_id] = inst
            self.sec_id_to_symbol[sec_id] = sym
            self.symbol_to_sec_id[sym] = sec_id
        self.symbol_to_sec_id["NIFTY 50"] = "13"
        self.symbol_to_sec_id["NIFTY50"] = "13"
        self.symbol_to_sec_id["BANK NIFTY"] = "25"
        self.symbol_to_sec_id["NIFTY BANK"] = "25"
        self.symbol_to_sec_id["BSE SENSEX"] = "51"

        logger.info(f"Loaded and indexed {count} NSE Equity instruments and core indices.")

    def get_security_id(self, symbol: str) -> Optional[str]:
        """Resolves symbol to security_id."""
        clean = symbol.strip().upper()
        if clean in self.symbol_to_sec_id:
            return self.symbol_to_sec_id[clean]
        if f"{clean}-EQ" in self.symbol_to_sec_id:
            return self.symbol_to_sec_id[f"{clean}-EQ"]
        return None

    def get_symbol(self, security_id: str) -> Optional[str]:
        """Resolves security_id to clean symbol."""
        return self.sec_id_to_symbol.get(str(security_id).strip())

    def get_instrument_info(self, security_id: str) -> Optional[InstrumentInfo]:
        """Gets full metadata for security_id."""
        return self.instruments_by_id.get(str(security_id).strip())

    def get_lot_size(self, symbol_or_sec_id: str) -> int:
        """Returns lot size (F&O lot size if available, otherwise equity lot size or 1)."""
        key = str(symbol_or_sec_id).strip().upper()
        clean_sym = self.sec_id_to_symbol.get(key, key).replace("-EQ", "")
        if clean_sym in self.fno_lot_sizes:
            return self.fno_lot_sizes[clean_sym]
        inst = self.instruments_by_id.get(key)
        if inst and inst.lot_size:
            return inst.lot_size
        return 1

    def resolve_universe(self, mode: Optional[str] = None) -> List[InstrumentInfo]:
        """
        Resolves the list of InstrumentInfo based on the configured mode:
        'custom', 'nifty50', 'all_nse_equity', etc.
        """
        target_mode = (mode or self.config.mode).lower()
        results: List[InstrumentInfo] = []

        if target_mode in ("indices_only", "indices"):
            for sec_id, sym, d_name, exch, seg, lot, tick in INDEX_DEFS:
                if sec_id in self.instruments_by_id:
                    results.append(self.instruments_by_id[sec_id])

        elif target_mode == "custom":
            symbols = self.config.custom_symbols
            for s in symbols:
                sec_id = self.get_security_id(s)
                if sec_id and sec_id in self.instruments_by_id:
                    results.append(self.instruments_by_id[sec_id])
                else:
                    logger.warning(f"Could not resolve security ID for custom symbol '{s}'")

        elif target_mode == "nifty50":
            symbols = self.config.nifty50_symbols
            for s in symbols:
                sec_id = self.get_security_id(s)
                if sec_id and sec_id in self.instruments_by_id:
                    results.append(self.instruments_by_id[sec_id])
                else:
                    logger.warning(f"Nifty 50 symbol '{s}' not resolved in scrip master.")

        elif target_mode in ("fno", "nse_fno"):
            for s in sorted(self.fno_symbols):
                sec_id = self.get_security_id(s)
                if sec_id and sec_id in self.instruments_by_id:
                    results.append(self.instruments_by_id[sec_id])

        elif target_mode in ("all_nse_equity", "all"):
            results = list(self.instruments_by_id.values())

        else:
            # Fallback to nifty50 or custom
            logger.info(f"Mode '{target_mode}' resolving against available Nifty50/Custom pool.")
            symbols = self.config.nifty50_symbols or self.config.custom_symbols
            for s in symbols:
                sec_id = self.get_security_id(s)
                if sec_id and sec_id in self.instruments_by_id:
                    results.append(self.instruments_by_id[sec_id])

        logger.info(f"Resolved universe ({target_mode}): {len(results)} instruments.")
        return results


instrument_manager = InstrumentManager()
