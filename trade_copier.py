#!/usr/bin/env python3
# """
# trade_copier.py

# Continuously watches trades on a master TradeLocker account and mirrors them
# (in real time) to a second account, including:
#   - Market & pending orders
#   - SL/TP modifications
#   - Order closures

# Bonus features:
#   - CLI interface (start/stop)
#   - Retry logic (max 3 attempts per API call)
#   - Adjustable slippage threshold
#   - Text‐file logging
# """

import argparse
import logging
import os
import signal
import sys
import threading
import time
from functools import wraps

from tradelocker import TradeLockerClient, errors

# === Configuration ===

API_KEY = os.getenv("TRADELOCKER_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("TRADELOCKER_API_SECRET", "YOUR_API_SECRET")

# Identify your two accounts here (strings or ints as the SDK expects)
MASTER_ACC = os.getenv("TRADELOCKER_MASTER_ACC", "ACC_NUM_A")
SLAVE_ACC = os.getenv("TRADELOCKER_SLAVE_ACC", "ACC_NUM_B")

# How often (in seconds) to poll if WebSocket isn't available
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))

# Maximum slippage (in pips) allowed when opening slave trades
MAX_SLIPPAGE_PIPS = float(os.getenv("MAX_SLIPPAGE_PIPS", "2.0"))

# Maximum retry attempts for any API call
MAX_RETRIES = 3

# === Setup logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("trade_copier.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("TradeCopier")


# === Utility: retry decorator ===

def retry(max_attempts=MAX_RETRIES, delay=0.5):
    """Retry decorator for transient API errors."""
    def deco(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempts = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except errors.TradeLockerAPIError as e:
                    attempts += 1
                    if attempts >= max_attempts:
                        logger.error(f"Max retries reached for {func.__name__}: {e}")
                        raise
                    logger.warning(f"Error in {func.__name__}, retrying ({attempts}/{max_attempts})…")
                    time.sleep(delay)
        return wrapper
    return deco


# === Main Copier Class ===

class TradeCopier:
    def __init__(self, api_key, api_secret, master_acc, slave_acc):
        self.client = TradeLockerClient(api_key, api_secret)
        self.master = master_acc
        self.slave = slave_acc

        # Mapping: master_order_id → slave_order_id
        self.order_map = {}

        # Stop flag for clean shutdown
        self._running = threading.Event()
        self._running.set()

    @retry()
    def list_positions(self, account):
        return self.client.get_open_positions(account)

    @retry()
    def list_orders(self, account):
        return self.client.get_open_orders(account)

    @retry()
    def place_order(self, account, **kwargs):
        return self.client.create_order(account, **kwargs)

    @retry()
    def modify_order(self, account, order_id, **kwargs):
        return self.client.modify_order(account, order_id, **kwargs)

    @retry()
    def close_position(self, account, position_id):
        return self.client.close_position(account, position_id)

    def shutdown(self):
        logger.info("Shutdown signal received, stopping trade copier…")
        self._running.clear()

    def run(self):
        logger.info("Starting trade copier loop.")
        # Attempt WS subscription first
        try:
            self.client.subscribe_order_events(self.master, self.on_master_event)
            logger.info("WebSocket subscription active. Listening for master events…")
            # Block until stopped
            while self._running.is_set():
                time.sleep(0.1)
        except NotImplementedError:
            logger.warning("WebSocket not available—falling back to polling mode.")
            self.poll_loop()

    def poll_loop(self):
        prev_positions = {}
        prev_orders = {}
        while self._running.is_set():
            # Positions (market trades)
            curr_positions = {pos.id: pos for pos in self.list_positions(self.master)}
            # Pending orders
            curr_orders = {o.id: o for o in self.list_orders(self.master)}

            # 1) New positions
            for master_id, pos in curr_positions.items():
                if master_id not in prev_positions:
                    logger.info(f"New market position detected: {master_id}, mirroring…")
                    self.on_new_master_trade(pos, is_pending=False)

            # 2) New pending orders
            for master_id, ord in curr_orders.items():
                if master_id not in prev_orders:
                    logger.info(f"New pending order detected: {master_id}, mirroring…")
                    self.on_new_master_trade(ord, is_pending=True)

            # 3) Modified trades (SL/TP changes)
            for master_id, pos in curr_positions.items():
                if master_id in prev_positions and pos.sl != prev_positions[master_id].sl or pos.tp != prev_positions[master_id].tp:
                    logger.info(f"SL/TP modification detected on {master_id}, applying…")
                    self.on_modify_master_trade(pos)

            for master_id, ord in curr_orders.items():
                if master_id in prev_orders and (ord.sl != prev_orders[master_id].sl or ord.tp != prev_orders[master_id].tp or ord.price != prev_orders[master_id].price):
                    logger.info(f"Pending order modification detected on {master_id}, applying…")
                    self.on_modify_master_trade(ord, is_pending=True)

            # 4) Closed positions/orders
            for master_id in list(prev_positions):
                if master_id not in curr_positions:
                    logger.info(f"Master position closed: {master_id}, closing slave…")
                    self.on_close_master_trade(prev_positions[master_id])
            for master_id in list(prev_orders):
                if master_id not in curr_orders:
                    logger.info(f"Master order canceled/executed: {master_id}, closing slave…")
                    self.on_close_master_trade(prev_orders[master_id], is_pending=True)

            prev_positions = curr_positions
            prev_orders = curr_orders
            time.sleep(POLL_INTERVAL)

    # === Event handlers ===

    def on_master_event(self, event):
        """
        WebSocket event handler from TradeLocker:
        event.type ∈ {"new", "modify", "close"}, event.data contains order/position
        """
        data = event.data
        if event.type == "new":
            self.on_new_master_trade(data, is_pending=(data.type != "market"))
        elif event.type == "modify":
            self.on_modify_master_trade(data, is_pending=(data.type != "market"))
        elif event.type == "close":
            self.on_close_master_trade(data, is_pending=(data.type != "market"))

    def on_new_master_trade(self, master_trade, is_pending=False):
        # Build kwargs for create_order()
        order_kwargs = dict(
            symbol=master_trade.symbol,
            side=master_trade.side,
            volume=master_trade.volume,
            order_type="pending" if is_pending else "market",
            price=getattr(master_trade, "price", None),
            sl=master_trade.sl,
            tp=master_trade.tp,
            slippage=MAX_SLIPPAGE_PIPS,
        )
        slave_resp = self.place_order(self.slave, **order_kwargs)
        self.order_map[master_trade.id] = slave_resp.id
        logger.info(f"Mapped master {master_trade.id} → slave {slave_resp.id}")

    def on_modify_master_trade(self, master_trade, is_pending=False):
        slave_id = self.order_map.get(master_trade.id)
        if not slave_id:
            logger.error(f"No mapped slave trade for master {master_trade.id}, skipping modify.")
            return
        modify_kwargs = dict(
            sl=master_trade.sl,
            tp=master_trade.tp,
            price=getattr(master_trade, "price", None) if is_pending else None,
        )
        self.modify_order(self.slave, slave_id, **{k: v for k, v in modify_kwargs.items() if v is not None})
        logger.info(f"Modified slave {slave_id} to SL={master_trade.sl}, TP={master_trade.tp}")

    def on_close_master_trade(self, master_trade, is_pending=False):
        slave_id = self.order_map.pop(master_trade.id, None)
        if not slave_id:
            logger.error(f"No mapped slave trade for master {master_trade.id}, skipping close.")
            return
        # Market positions use close_position; pending orders use cancel_order
        if is_pending:
            self.client.cancel_order(self.slave, slave_id)
            logger.info(f"Canceled slave pending order {slave_id}")
        else:
            self.close_position(self.slave, slave_id)
            logger.info(f"Closed slave market position {slave_id}")

# === CLI Entrypoint ===

def main():
    parser = argparse.ArgumentParser(description="TradeLocker Real-Time Trade Copier")
    parser.add_argument("--master", help="Master account number", default=MASTER_ACC)
    parser.add_argument("--slave", help="Slave account number", default=SLAVE_ACC)
    args = parser.parse_args()

    copier = TradeCopier(API_KEY, API_SECRET, args.master, args.slave)

    # Handle SIGINT to allow graceful shutdown
    signal.signal(signal.SIGINT, lambda sig, frame: copier.shutdown())

    copier.run()
    logger.info("Trade copier stopped.")


if __name__ == "__main__":
    main()
