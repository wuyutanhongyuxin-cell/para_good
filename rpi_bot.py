#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RPI Bot - 100% RPI 触发交易机器人 (v3.3 Maker优先版)

核心原理:
    RPI (Rebate Point Index) 只出现在 TAKER 订单上
    使用市价单进行快速买入/卖出，每笔交易都是 TAKER
    配合 interactive token 实现 0 手续费 + RPI 积分

v3.1 新增功能:
    - 双向交易支持 (做多/做空)
    - 对称的止盈止损逻辑
    - 可配置的做空开关

v3.2 新增功能:
    - 方向一致性过滤 (订单簿方向需与微趋势一致)
    - 动量过滤 (线性回归斜率检查)
    - 完整的环境变量配置支持

v3.3 新增功能 (Maker优先):
    - 入场改为限价单优先 (POST_ONLY)
    - 支持挂单价格偏移 (offset_ticks)
    - 限价单超时撤单与重挂机制 (最多重挂N次)
    - 点差超限时禁止Taker fallback
    - 平仓也支持Maker优先 (非止损情况)
    - 增强日志: 区分Maker/Taker入场与退出

基于 pp2 项目改进: https://github.com/wuyutanhongyuxin-cell/pp2
"""

import os
import sys
import json
import time
import asyncio
import logging
import signal
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from decimal import Decimal, ROUND_DOWN

from dotenv import load_dotenv

_shutdown_requested = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('RPI-BOT')


# =============================================================================
# 配置类
# =============================================================================

@dataclass
class RPIConfig:
    """RPI 交易配置"""

    # 核心参数
    trade_size: str = "0.003"
    trade_interval: float = 5.0
    market: str = "BTC-USD-PERP"

    # 限速设置
    limits_per_second: int = 2
    limits_per_minute: int = 30
    limits_per_hour: int = 300
    limits_per_day: int = 1000

    # Spread 优化
    max_spread_pct: float = 0.03

    # 止盈止损 (多空共用)
    stop_loss_pct: float = 0.015
    max_wait_seconds: float = 30.0
    check_interval: float = 0.5

    # 趋势过滤
    trend_filter_enabled: bool = True

    # 波动率过滤
    volatility_filter_enabled: bool = True
    max_volatility_pct: float = 0.1
    volatility_window: int = 10

    # 入场模式
    entry_mode: str = "trend"

    # 动态止损
    dynamic_stop_loss: bool = True
    stop_loss_spread_multiplier: float = 2.0
    dynamic_stop_loss_min: float = 0.01
    dynamic_stop_loss_max: float = 0.05

    # 追踪止损
    trailing_stop_enabled: bool = True
    trailing_stop_activation: float = 1.0
    trailing_stop_callback: float = 0.5
    breakeven_activation: float = 1.0

    # 智能持仓
    smart_hold_enabled: bool = True
    hold_extension_seconds: float = 15.0
    early_exit_stagnation_threshold: float = 0.02

    # RSI 过滤
    rsi_filter_enabled: bool = True
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    rsi_trend_threshold: float = 50.0

    # 研究优化参数
    exit_order_type: str = "limit"
    exit_post_only: bool = True
    exit_price_offset_ticks: int = 0
    direction_signal_threshold: float = 0.3
    risk_reward_ratio: float = 2.0

    # ===== v3.3 Maker优先配置 =====
    entry_order_type: str = "limit"
    entry_post_only: bool = True
    entry_price_offset_ticks: int = 0
    entry_order_timeout_seconds: float = 3.0
    entry_requote_max: int = 2
    market_spread_limit_pct: float = 0.006

    # 时段过滤
    time_filter_enabled: bool = True
    optimal_hours_start: int = 8
    optimal_hours_end: int = 21
    weekend_multiplier: float = 0.5

    # Kelly
    kelly_enabled: bool = True
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.30

    # 仓位平滑
    position_smoothing_enabled: bool = True
    max_position_increase_pct: float = 0.50

    # 回撤控制
    drawdown_control_enabled: bool = True
    max_daily_loss_pct: float = 0.03
    max_total_loss_pct: float = 0.10

    # ===== v3.1 做空交易配置 =====
    enable_short: bool = True

    # ===== v3.2 方向一致性过滤 =====
    direction_trend_confirm: bool = True

    # ===== v3.2 动量过滤配置 =====
    price_momentum_window: int = 6
    price_momentum_min_slope: float = 0.0

    enabled: bool = True


@dataclass
class RateLimitState:
    day: str = ""
    trades: List[int] = field(default_factory=list)


@dataclass
class AccountInfo:
    l2_private_key: str
    l2_address: str
    name: str = ""


# =============================================================================
# Paradex API 客户端
# =============================================================================

class ParadexInteractiveClient:
    def __init__(self, l2_private_key: str, l2_address: str, environment: str = "prod"):
        self.l2_private_key = l2_private_key
        self.l2_address = l2_address
        self.environment = environment
        self.base_url = f"https://api.{'prod' if environment == 'prod' else 'testnet'}.paradex.trade/v1"
        self.jwt_token: Optional[str] = None
        self.jwt_expires_at: int = 0
        self.market_info: Dict[str, Any] = {}
        self.client_id_format = "rpi"

        try:
            from paradex_py import ParadexSubkey
            from paradex_py.environment import PROD, TESTNET
            env = PROD if environment == "prod" else TESTNET
            self.paradex = ParadexSubkey(env=env, l2_private_key=l2_private_key, l2_address=l2_address)
            log.info(f"Paradex SDK 初始化成功 (环境: {environment})")
        except ImportError:
            log.error("请先安装 paradex-py: pip install paradex-py")
            raise

    async def authenticate_interactive(self) -> bool:
        try:
            import aiohttp
            auth_headers = self.paradex.account.auth_headers()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = f"{self.base_url}/auth?token_usage=interactive"
                headers = {"Content-Type": "application/json", **auth_headers}
                async with session.post(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.jwt_token = data.get("jwt_token")
                        import base64
                        payload = self.jwt_token.split('.')[1]
                        payload += '=' * (4 - len(payload) % 4)
                        decoded = json.loads(base64.b64decode(payload))
                        self.jwt_expires_at = decoded.get("exp", 0)
                        log.info(f"认证成功! token_usage={decoded.get('token_usage', 'unknown')}")
                        return True
                    return False
        except Exception as e:
            log.error(f"认证异常: {e}")
            return False

    async def ensure_authenticated(self) -> bool:
        if self.jwt_token and self.jwt_expires_at > int(time.time()) + 60:
            return True
        return await self.authenticate_interactive()

    def _generate_client_id(self) -> str:
        import uuid
        ts = int(time.time() * 1000)
        if self.client_id_format == "rpi":
            return f"rpi_{ts}"
        elif self.client_id_format == "uuid":
            return str(uuid.uuid4())
        return f"rpi_{ts}"

    def _get_auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt_token}", "Content-Type": "application/json"}

    async def get_balance(self) -> Optional[float]:
        try:
            if not await self.ensure_authenticated():
                return None
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"{self.base_url}/balance", headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("results", []):
                            if item.get("token") == "USDC":
                                return float(item.get("size", 0))
            return 0
        except Exception as e:
            log.error(f"获取余额失败: {e}")
            return None

    async def get_positions(self, market: str = None) -> List[Dict]:
        try:
            if not await self.ensure_authenticated():
                return []
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"{self.base_url}/positions", headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        positions = data.get("results", [])
                        if market:
                            positions = [p for p in positions if p.get("market") == market]
                        # 使用 >= 0.00001 避免浮点精度问题遗漏小仓位
                        return [p for p in positions if p.get("status") != "CLOSED" and float(p.get("size", 0)) >= 0.00001]
            return []
        except Exception as e:
            log.error(f"获取持仓失败: {e}")
            return []

    async def get_bbo(self, market: str) -> Optional[Dict]:
        try:
            if not await self.ensure_authenticated():
                return None
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"{self.base_url}/orderbook/{market}?depth=1", headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bids, asks = data.get("bids", []), data.get("asks", [])
                        best_bid = data.get("best_bid_api") or (bids[0] if bids else None)
                        best_ask = data.get("best_ask_api") or (asks[0] if asks else None)
                        if best_bid and best_ask:
                            return {"bid": float(best_bid[0]), "ask": float(best_ask[0]),
                                    "bid_size": float(best_bid[1]), "ask_size": float(best_ask[1])}
            return None
        except Exception as e:
            log.error(f"获取 BBO 失败: {e}")
            return None

    async def get_orderbook_imbalance(self, market: str, depth: int = 5) -> Optional[float]:
        try:
            if not await self.ensure_authenticated():
                return None
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"{self.base_url}/orderbook/{market}?depth={depth}", headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bids, asks = data.get("bids", []), data.get("asks", [])
                        total_bid = sum(float(b[1]) for b in bids[:depth])
                        total_ask = sum(float(a[1]) for a in asks[:depth])
                        if total_bid + total_ask == 0:
                            return 0
                        return (total_bid - total_ask) / (total_bid + total_ask)
            return None
        except Exception as e:
            log.error(f"获取订单簿失败: {e}")
            return None

    async def get_price_volatility(self, market: str, window_seconds: int = 10, samples: int = 5) -> Optional[Dict]:
        try:
            prices = []
            interval = window_seconds / samples
            for i in range(samples):
                bbo = await self.get_bbo(market)
                if bbo:
                    prices.append(bbo["bid"])
                if i < samples - 1:
                    await asyncio.sleep(interval)
            if len(prices) < 2:
                return None
            min_p, max_p = min(prices), max(prices)
            avg_p = sum(prices) / len(prices)
            return {
                "volatility_pct": (max_p - min_p) / avg_p * 100,
                "trend": "up" if prices[-1] > prices[0] else "down" if prices[-1] < prices[0] else "flat",
                "price_change_pct": (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0,
                "prices": prices,
                "latest_price": prices[-1]
            }
        except Exception as e:
            log.error(f"获取波动率失败: {e}")
            return None

    async def place_market_order(self, market: str, side: str, size: str, reduce_only: bool = False) -> Optional[Dict]:
        try:
            if not await self.ensure_authenticated():
                return None
            from paradex_py.common.order import Order, OrderSide, OrderType
            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell
            order = Order(
                market=market, order_type=OrderType.Market, order_side=order_side,
                size=Decimal(size), client_id=self._generate_client_id(),
                reduce_only=reduce_only, signature_timestamp=int(time.time() * 1000),
            )
            order.signature = self.paradex.account.sign_order(order)
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(f"{self.base_url}/orders", headers=self._get_auth_headers(), json=order.dump_to_dict()) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        log.info(f"市价单成功: {side} {size} BTC, order_id={result.get('id')}")
                        return result
                    error = await resp.text()
                    log.error(f"市价单失败: {resp.status} - {error}")
                    return None
        except Exception as e:
            log.error(f"市价单失败: {e}")
            return None

    async def place_limit_order(self, market: str, side: str, size: str, price: str,
                                 post_only: bool = True, reduce_only: bool = False) -> Optional[Dict]:
        try:
            if not await self.ensure_authenticated():
                return None
            from paradex_py.common.order import Order, OrderSide, OrderType
            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell
            order = Order(
                market=market, order_type=OrderType.Limit, order_side=order_side,
                size=Decimal(size), limit_price=Decimal(price), client_id=self._generate_client_id(),
                reduce_only=reduce_only, instruction="POST_ONLY" if post_only else "GTC",
                signature_timestamp=int(time.time() * 1000),
            )
            order.signature = self.paradex.account.sign_order(order)
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(f"{self.base_url}/orders", headers=self._get_auth_headers(), json=order.dump_to_dict()) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        log.info(f"限价单成功: {side} {size} @ ${price}")
                        return result
                    return None
        except Exception as e:
            log.error(f"限价单失败: {e}")
            return None

    async def wait_order_fill(self, order_id: str, timeout_seconds: float = 5.0) -> Optional[Dict]:
        try:
            import aiohttp
            start = time.time()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds + 2)) as session:
                while time.time() - start < timeout_seconds:
                    async with session.get(f"{self.base_url}/orders/{order_id}", headers=self._get_auth_headers()) as resp:
                        if resp.status == 200:
                            order = await resp.json()
                            if order.get("status") == "CLOSED":
                                # 检查是否完全成交（避免部分成交导致残留仓位）
                                remaining = float(order.get("remaining_size", 0))
                                if remaining >= 0.00001:  # 有残留未成交
                                    log.warning(f"[部分成交] 订单{order_id}部分成交，剩余{remaining} BTC未成交")
                                    return None  # 视为失败，避免残留仓位
                                return order
                            if order.get("status") in ["CANCELED", "REJECTED"]:
                                return None
                    await asyncio.sleep(0.2)
            await self.cancel_order(order_id)
            return None
        except Exception as e:
            log.error(f"等待成交失败: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.delete(f"{self.base_url}/orders/{order_id}", headers=self._get_auth_headers()) as resp:
                    return resp.status in [200, 204]
        except:
            return False

    async def cancel_all_orders(self, market: str = None) -> int:
        try:
            if not await self.ensure_authenticated():
                return 0
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                params = {"status": "OPEN"}
                if market:
                    params["market"] = market
                async with session.get(f"{self.base_url}/orders", headers=self._get_auth_headers(), params=params) as resp:
                    if resp.status != 200:
                        return 0
                    orders = (await resp.json()).get("results", [])
                    cancelled = 0
                    for order in orders:
                        oid = order.get("id")
                        if oid:
                            async with session.delete(f"{self.base_url}/orders/{oid}", headers=self._get_auth_headers()) as r:
                                if r.status in [200, 204]:
                                    cancelled += 1
                    if cancelled:
                        log.info(f"已取消 {cancelled}/{len(orders)} 个挂单")
                    return cancelled
        except Exception as e:
            log.error(f"取消订单失败: {e}")
            return 0

    async def close_all_positions(self, market: str = None) -> int:
        try:
            if not await self.ensure_authenticated():
                return 0
            positions = await self.get_positions()
            closed = 0
            for pos in positions:
                pos_market = pos.get("market")
                if market and pos_market != market:
                    continue
                size = pos.get("size", "0")
                side = pos.get("side", "")
                # 避免浮点精度问题，用小阈值判断
                if float(size) < 0.00001:
                    continue
                close_side = "SELL" if side == "LONG" else "BUY"
                if await self.place_market_order(pos_market, close_side, size, reduce_only=True):
                    closed += 1
                    log.info(f"已平仓 {pos_market}: {close_side} {size}")
            return closed
        except Exception as e:
            log.error(f"平仓失败: {e}")
            return 0


# =============================================================================
# 多账号管理器
# =============================================================================

class AccountManager:
    def __init__(self, accounts: List[AccountInfo], environment: str = "prod"):
        if not accounts:
            raise ValueError("至少需要配置一个账号")
        self.accounts = accounts
        self.environment = environment
        self.current_index = 0
        self.clients: Dict[int, ParadexInteractiveClient] = {}
        self.rate_states: Dict[int, RateLimitState] = {i: RateLimitState() for i in range(len(accounts))}
        self.daily_limits = 1000
        log.info(f"账号管理器: 共 {len(accounts)} 个账号")

    def get_current_client(self) -> Optional[ParadexInteractiveClient]:
        if self.current_index >= len(self.accounts):
            return None
        if self.current_index not in self.clients:
            acc = self.accounts[self.current_index]
            try:
                self.clients[self.current_index] = ParadexInteractiveClient(
                    acc.l2_private_key, acc.l2_address, self.environment
                )
                log.info(f"已加载账号 #{self.current_index + 1}")
            except Exception as e:
                log.error(f"加载账号失败: {e}")
                return None
        return self.clients[self.current_index]

    def get_current_rate_state(self) -> RateLimitState:
        return self.rate_states[self.current_index]

    def get_current_account_name(self) -> str:
        if self.current_index >= len(self.accounts):
            return "无可用账号"
        return self.accounts[self.current_index].name or f"账号#{self.current_index + 1}"

    def is_account_hour_limited(self, idx: int) -> bool:
        cutoff = int(time.time() * 1000) - 3600000
        return len([t for t in self.rate_states[idx].trades if t > cutoff]) >= 300

    def switch_to_next_available_account(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        for _ in range(len(self.accounts)):
            self.current_index = (self.current_index + 1) % len(self.accounts)
            state = self.rate_states[self.current_index]
            if state.day == today and len(state.trades) >= self.daily_limits:
                continue
            if not self.is_account_hour_limited(self.current_index):
                log.info(f"切换到 {self.get_current_account_name()}")
                return "switched"
        has_day = any(self.rate_states[i].day != today or len(self.rate_states[i].trades) < self.daily_limits
                      for i in range(len(self.accounts)))
        return "all_hour_limited" if has_day else "all_day_limited"

    def all_accounts_exhausted(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        return all(self.rate_states[i].day == today and len(self.rate_states[i].trades) >= self.daily_limits
                   for i in range(len(self.accounts)))

    def save_state(self, filepath: str = "rpi_account_states.json"):
        try:
            with open(filepath, 'w') as f:
                json.dump({
                    "current_index": self.current_index,
                    "rate_states": {str(i): {"day": s.day, "trades": s.trades[-1000:]} for i, s in self.rate_states.items()}
                }, f)
        except Exception as e:
            log.error(f"保存状态失败: {e}")

    def load_state(self, filepath: str = "rpi_account_states.json"):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    self.current_index = data.get("current_index", 0)
                    for i_str, sd in data.get("rate_states", {}).items():
                        i = int(i_str)
                        if i < len(self.accounts):
                            self.rate_states[i] = RateLimitState(day=sd.get("day", ""), trades=sd.get("trades", []))
                log.info(f"已加载状态，当前账号: {self.get_current_account_name()}")
        except Exception as e:
            log.warning(f"加载状态失败: {e}")


# =============================================================================
# RPI 交易机器人 (v3.3 Maker优先版)
# =============================================================================

class RPIBot:
    """RPI 机器人 v3.3 - Maker优先入场与退出"""

    def __init__(self, client: ParadexInteractiveClient, config: RPIConfig,
                 account_manager: Optional[AccountManager] = None):
        self.client = client
        self.config = config
        self.account_manager = account_manager
        self.rate_state = RateLimitState()

        self.total_trades = 0
        self.rpi_trades = 0
        self.long_trades = 0
        self.short_trades = 0
        self.start_time = None
        self.session_start_balance: Optional[float] = None
        self.daily_start_balance: Optional[float] = None
        self.current_day: str = ""
        self.recent_trades: List[Dict] = []
        self.total_pnl: float = 0.0

        self.last_position_size: Optional[float] = None
        self.price_history: List[float] = []

        if self.account_manager:
            self.rate_state = self.account_manager.get_current_rate_state()

    def _day_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _prune_trades(self):
        cutoff = int(time.time() * 1000) - 86400000
        self.rate_state.trades = [t for t in self.rate_state.trades if t > cutoff]

    def _count_trades_in_window(self, window_ms: int) -> int:
        cutoff = int(time.time() * 1000) - window_ms
        return len([t for t in self.rate_state.trades if t > cutoff])

    def _can_trade(self) -> tuple:
        if self.account_manager:
            self.rate_state = self.account_manager.get_current_rate_state()
        if self.rate_state.day != self._day_key():
            self.rate_state.day = self._day_key()
            self.rate_state.trades = []
        self._prune_trades()
        usage = {
            "sec": self._count_trades_in_window(1000),
            "min": self._count_trades_in_window(60000),
            "hour": self._count_trades_in_window(3600000),
            "day": len(self.rate_state.trades),
        }
        if self.account_manager:
            if usage["day"] >= self.config.limits_per_day:
                return (False, "all_accounts_exhausted", usage) if self.account_manager.all_accounts_exhausted() else (False, "day_limit_switch", usage)
            if usage["hour"] >= self.config.limits_per_hour:
                return False, "hour_switch", usage
        else:
            if usage["day"] >= self.config.limits_per_day:
                return False, "day", usage
            if usage["hour"] >= self.config.limits_per_hour:
                return False, "hour", usage
        if usage["min"] >= self.config.limits_per_minute:
            return False, "min", usage
        if usage["sec"] >= self.config.limits_per_second:
            return False, "sec", usage
        return True, None, usage

    def _record_trade(self):
        self.rate_state.trades.append(int(time.time() * 1000))
        self.total_trades += 1
        if self.account_manager:
            self.account_manager.save_state()

    def _record_trade_result(self, pnl: float, win: bool, direction: str):
        self.recent_trades.append({"time": time.time(), "pnl": pnl, "win": win, "direction": direction})
        self.total_pnl += pnl
        if len(self.recent_trades) > 100:
            self.recent_trades = self.recent_trades[-100:]

    def _check_trading_time(self) -> tuple:
        if not self.config.time_filter_enabled:
            return True, 1.0, "时段过滤关闭"
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        hour, weekday = now.hour, now.weekday()
        if weekday >= 5:
            return True, self.config.weekend_multiplier, f"周末, 仓位x{self.config.weekend_multiplier}"
        if self.config.optimal_hours_start <= hour < self.config.optimal_hours_end:
            return (True, 1.0, "黄金时段") if 13 <= hour < 17 else (True, 0.8, "良好时段")
        return False, 0.3, f"低流动性时段 (UTC {hour}:00)"

    def _calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        recent = deltas[-period:]
        gains = [d if d > 0 else 0 for d in recent]
        losses = [-d if d < 0 else 0 for d in recent]
        avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
        if avg_loss == 0:
            return 100.0
        return 100 - (100 / (1 + avg_gain / avg_loss))

    def _calculate_momentum_slope(self, prices: List[float]) -> Optional[float]:
        """
        计算价格动量斜率 (线性回归)
        返回: 斜率百分比 (slope_pct)
        """
        n = len(prices)
        if n < 2:
            return None

        # 简单线性回归: y = ax + b
        # a = (n * sum(xy) - sum(x) * sum(y)) / (n * sum(x^2) - sum(x)^2)
        x_sum = sum(range(n))
        y_sum = sum(prices)
        xy_sum = sum(i * p for i, p in enumerate(prices))
        x2_sum = sum(i * i for i in range(n))

        denominator = n * x2_sum - x_sum * x_sum
        if denominator == 0:
            return 0.0

        slope = (n * xy_sum - x_sum * y_sum) / denominator

        # 转换为百分比 (相对于平均价格)
        avg_price = y_sum / n
        if avg_price == 0:
            return 0.0

        slope_pct = (slope / avg_price) * 100
        return slope_pct

    def _is_signal_confirmed(self, prices: List[float], direction: str) -> tuple:
        """确认入场信号 (RSI过滤 + 动量过滤) - 支持做多和做空"""
        rsi = self._calculate_rsi(prices, self.config.rsi_period)

        # 动量计算
        momentum_window = min(self.config.price_momentum_window, len(prices))
        momentum_prices = prices[-momentum_window:] if momentum_window > 0 else prices
        slope_pct = self._calculate_momentum_slope(momentum_prices)
        slope_pct_val = slope_pct if slope_pct is not None else 0.0

        if not self.config.rsi_filter_enabled:
            # 只做动量检查
            if self.config.price_momentum_min_slope > 0:
                if direction == "LONG":
                    if slope_pct_val >= self.config.price_momentum_min_slope:
                        log.info(f"[信号过滤] RSI过滤关闭, slope_pct={slope_pct_val:.4f}% 满足做多动量")
                        return True, f"slope_pct={slope_pct_val:.4f}% 做多确认"
                    log.info(f"[信号过滤] RSI过滤关闭, slope_pct={slope_pct_val:.4f}% < {self.config.price_momentum_min_slope}% 动量不足")
                    return False, f"slope_pct={slope_pct_val:.4f}% 动量不足"
                elif direction == "SHORT":
                    if slope_pct_val <= -self.config.price_momentum_min_slope:
                        log.info(f"[信号过滤] RSI过滤关闭, slope_pct={slope_pct_val:.4f}% 满足做空动量")
                        return True, f"slope_pct={slope_pct_val:.4f}% 做空确认"
                    log.info(f"[信号过滤] RSI过滤关闭, slope_pct={slope_pct_val:.4f}% > -{self.config.price_momentum_min_slope}% 动量不足")
                    return False, f"slope_pct={slope_pct_val:.4f}% 动量不足"
            return True, "RSI过滤已关闭"

        if rsi is None:
            return True, "RSI数据不足"

        if self.config.entry_mode == "trend":
            if direction == "LONG":
                if rsi > self.config.rsi_trend_threshold:
                    # 动量检查
                    if self.config.price_momentum_min_slope > 0 and slope_pct_val < self.config.price_momentum_min_slope:
                        log.info(f"[信号过滤] RSI={rsi:.1f} 满足, 但 slope_pct={slope_pct_val:.4f}% < {self.config.price_momentum_min_slope}% 动量不足")
                        return False, f"RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 动量不足"
                    # RSI和动量均满足，确认做多
                    log.info(f"[信号过滤] RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 满足做多条件，确认入场")
                    return True, f"RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 做多确认"
                log.info(f"[信号过滤] RSI={rsi:.1f} < {self.config.rsi_trend_threshold}，不满足做多")
                return False, f"RSI={rsi:.1f} 低于阈值"

            elif direction == "SHORT":
                if rsi < self.config.rsi_trend_threshold:
                    # 动量检查 (做空需要负斜率)
                    if self.config.price_momentum_min_slope > 0 and slope_pct_val > -self.config.price_momentum_min_slope:
                        log.info(f"[信号过滤] RSI={rsi:.1f} 满足, 但 slope_pct={slope_pct_val:.4f}% > -{self.config.price_momentum_min_slope}% 动量不足")
                        return False, f"RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 动量不足"
                    # RSI和动量均满足，确认做空
                    log.info(f"[信号过滤] RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 满足做空条件，确认入场")
                    return True, f"RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 做空确认"
                log.info(f"[信号过滤] RSI={rsi:.1f} > {self.config.rsi_trend_threshold}，不满足做空")
                return False, f"RSI={rsi:.1f} 高于阈值"

        elif self.config.entry_mode == "mean_reversion":
            if direction == "LONG" and rsi < self.config.rsi_oversold:
                log.info(f"[信号过滤] RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 超卖，确认做多反转")
                return True, f"RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 超卖反转"
            elif direction == "SHORT" and rsi > self.config.rsi_overbought:
                log.info(f"[信号过滤] RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 超买，确认做空反转")
                return True, f"RSI={rsi:.1f} slope_pct={slope_pct_val:.4f}% 超买反转"
            return False, f"RSI={rsi:.1f} 不在极值区"

        return True, "默认通过"

    def _calculate_kelly_size(self, balance: float, base_size: float) -> float:
        if not self.config.kelly_enabled or len(self.recent_trades) < 10:
            return base_size
        wins = [t for t in self.recent_trades if t["win"]]
        losses = [t for t in self.recent_trades if not t["win"]]
        if not wins or not losses:
            return base_size
        win_rate = len(wins) / len(self.recent_trades)
        avg_win = sum(t["pnl"] for t in wins) / len(wins)
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses))
        if avg_loss == 0:
            return base_size
        b = avg_win / avg_loss
        kelly = ((b * win_rate - (1 - win_rate)) / b) * self.config.kelly_fraction if b > 0 else 0
        if kelly <= 0:
            log.warning(f"[Kelly] 负期望值! 胜率={win_rate:.1%}")
            return base_size * 0.5
        log.info(f"[Kelly] 胜率={win_rate:.1%}, R/R={b:.2f}, Kelly={kelly:.2%}")
        return min(base_size * (1 + kelly), base_size * 2)

    def _apply_position_smoothing(self, target_size: float) -> float:
        if not self.config.position_smoothing_enabled:
            return target_size
        if self.last_position_size is None:
            self.last_position_size = target_size
            return target_size
        increase = (target_size - self.last_position_size) / self.last_position_size if self.last_position_size > 0 else 0
        if increase > self.config.max_position_increase_pct:
            smoothed = self.last_position_size * (1 + self.config.max_position_increase_pct)
            log.info(f"[仓位管理] Kelly建议{target_size:.6f} BTC，平滑后{smoothed:.6f} BTC (最大增幅{self.config.max_position_increase_pct*100:.0f}%)")
            self.last_position_size = smoothed
            return smoothed
        self.last_position_size = target_size
        return target_size

    def _check_drawdown(self, balance: float) -> tuple:
        if not self.config.drawdown_control_enabled:
            return True, ""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.current_day != today:
            self.current_day = today
            self.daily_start_balance = balance
        if self.session_start_balance is None:
            self.session_start_balance = balance
            self.daily_start_balance = balance
        if self.daily_start_balance and self.daily_start_balance > 0:
            daily_loss = (self.daily_start_balance - balance) / self.daily_start_balance
            if daily_loss >= self.config.max_daily_loss_pct:
                return False, f"日内回撤 {daily_loss:.1%} 超过阈值 {self.config.max_daily_loss_pct*100:.1f}%"
        if self.session_start_balance and self.session_start_balance > 0:
            total_loss = (self.session_start_balance - balance) / self.session_start_balance
            if total_loss >= self.config.max_total_loss_pct:
                return False, f"总回撤 {total_loss:.1%} 超过阈值 {self.config.max_total_loss_pct*100:.1f}%"
        return True, ""

    def _get_direction_signal(self, imbalance: Optional[float]) -> tuple:
        """获取方向信号 - 支持做多和做空"""
        if imbalance is None:
            return "NEUTRAL", 0.0
        threshold = self.config.direction_signal_threshold
        if imbalance > threshold:
            return "LONG", abs(imbalance)
        elif imbalance < -threshold:
            return "SHORT", abs(imbalance)
        return "NEUTRAL", abs(imbalance)

    def _check_direction_trend_confirm(self, direction: str, micro_trend: str) -> tuple:
        """
        方向一致性过滤 (v3.2)
        当 config.direction_trend_confirm 为 true 时：
        - 订单簿方向为 LONG 且微趋势为 down -> 拒绝
        - 订单簿方向为 SHORT 且微趋势为 up -> 拒绝
        """
        if not self.config.direction_trend_confirm:
            return True, ""

        if direction == "LONG" and micro_trend == "down":
            log.info(f"[方向过滤] 订单簿=LONG 微趋势=down 跳过")
            return False, "订单簿=LONG 微趋势=down 方向不一致"

        if direction == "SHORT" and micro_trend == "up":
            log.info(f"[方向过滤] 订单簿=SHORT 微趋势=up 跳过")
            return False, "订单簿=SHORT 微趋势=up 方向不一致"

        return True, ""

    async def _execute_trade(self, direction: str, market: str, size: str, bid: float, ask: float,
                              stop_loss_pct: float, take_profit_pct: float) -> tuple:
        """
        执行交易 - 统一的多空交易逻辑 (v3.3 Maker优先)

        Args:
            direction: "LONG" 或 "SHORT"
            market: 交易市场
            size: 交易大小
            bid: 买一价
            ask: 卖一价
            stop_loss_pct: 止损比例
            take_profit_pct: 止盈比例

        Returns:
            (成功标志, 退出原因, PnL)
        """
        is_long = direction == "LONG"
        entry_side = "BUY" if is_long else "SELL"
        open_result = None
        entry_price = ask if is_long else bid
        is_maker_entry = False

        # ===== v3.3 Maker优先入场逻辑 =====
        if self.config.entry_order_type == "limit":
            # 计算挂单价格: 做多用bid(买一), 做空用ask(卖一)
            # offset_ticks > 0 表示更激进(更接近对手价)
            tick_size = 0.1  # BTC-USD-PERP tick size
            if is_long:
                limit_price = bid + self.config.entry_price_offset_ticks * tick_size
            else:
                limit_price = ask - self.config.entry_price_offset_ticks * tick_size

            log.info(f"[开{'多' if is_long else '空'}单] Maker优先: 限价{'买入' if is_long else '卖出'} {size} BTC @ ${limit_price:.1f} (best_{'bid' if is_long else 'ask'}={bid if is_long else ask:.1f}, offset={self.config.entry_price_offset_ticks})")

            # 重挂循环
            for attempt in range(self.config.entry_requote_max + 1):
                if attempt > 0:
                    # 重新获取BBO
                    new_bbo = await self.client.get_bbo(market)
                    if new_bbo:
                        bid, ask = new_bbo["bid"], new_bbo["ask"]
                        if is_long:
                            limit_price = bid + self.config.entry_price_offset_ticks * tick_size
                        else:
                            limit_price = ask - self.config.entry_price_offset_ticks * tick_size
                    log.info(f"[重挂] 第{attempt}次重挂 @ ${limit_price:.1f}")

                # 下限价单
                limit_result = await self.client.place_limit_order(
                    market=market,
                    side=entry_side,
                    size=size,
                    price=str(round(limit_price, 1)),
                    post_only=self.config.entry_post_only,
                    reduce_only=False
                )

                if not limit_result:
                    log.warning(f"[开仓] 限价单提交失败, 尝试 {attempt + 1}/{self.config.entry_requote_max + 1}")
                    continue

                order_id = limit_result.get("id")
                log.info(f"[开仓] 限价单已挂 order_id={order_id}, 等待成交 (超时={self.config.entry_order_timeout_seconds}s)")

                # 等待成交
                fill_result = await self.client.wait_order_fill(order_id, timeout_seconds=self.config.entry_order_timeout_seconds)

                if fill_result:
                    open_result = fill_result
                    entry_price = float(fill_result.get("avg_fill_price", limit_price))
                    is_maker_entry = True
                    log.info(f"[开仓] Maker成交! @ ${entry_price:.1f}")
                    break
                else:
                    log.info(f"[开仓] 限价单超时未成交, 已撤单 (尝试 {attempt + 1}/{self.config.entry_requote_max + 1})")

            # 限价单全部失败, 检查是否允许fallback到市价单
            if not open_result:
                current_spread_pct = (ask - bid) / bid * 100
                if current_spread_pct > self.config.market_spread_limit_pct:
                    log.warning(f"[开仓] Maker入场失败, 当前spread={current_spread_pct:.4f}% > 限制{self.config.market_spread_limit_pct:.4f}%, 禁止吃单")
                    return False, "maker_failed_spread_too_wide", 0
                else:
                    log.info(f"[开仓] Maker入场失败, spread={current_spread_pct:.4f}% <= {self.config.market_spread_limit_pct:.4f}%, 允许Taker fallback")

        # 市价单入场 (原始逻辑或Maker失败后的fallback)
        if not open_result:
            entry_price = ask if is_long else bid
            log.info(f"[开{'多' if is_long else '空'}单] 市价{'买入' if is_long else '卖出'} {size} BTC @ 约${entry_price:.1f}")
            open_result = await self.client.place_market_order(market=market, side=entry_side, size=size, reduce_only=False)

        if not open_result:
            return False, f"{'开多' if is_long else '开空'}失败", 0

        self._record_trade()
        if direction == "LONG":
            self.long_trades += 1
        else:
            self.short_trades += 1

        flags = open_result.get("flags", [])
        entry_type_str = "Maker" if is_maker_entry else "Taker"
        if "rpi" in [f.lower() for f in flags]:
            self.rpi_trades += 1
            log.info(f"  -> [{entry_type_str}] RPI! flags={flags}")
        else:
            log.info(f"  -> [{entry_type_str}] flags={flags}")

        # 计算止盈止损价格
        if is_long:
            target_price = entry_price * (1 + take_profit_pct)
            stop_price = entry_price * (1 - stop_loss_pct)
        else:
            target_price = entry_price * (1 - take_profit_pct)
            stop_price = entry_price * (1 + stop_loss_pct)

        log.info(f"[等待] 止盈: ${target_price:.1f} | 止损: ${stop_price:.1f}")

        # 持仓监控循环
        best_price = entry_price
        extreme_price = entry_price
        exit_reason = "timeout"
        breakeven_activated = False
        trailing_activated = False

        if self.config.max_wait_seconds <= 0:
            log.info("[极速] 立即平仓模式")
        else:
            wait_start = time.time()
            max_wait = self.config.max_wait_seconds
            extension_used = False

            while time.time() - wait_start < max_wait:
                if _shutdown_requested:
                    exit_reason = "shutdown"
                    break

                new_bbo = await self.client.get_bbo(market)
                if new_bbo:
                    best_price = new_bbo["bid"] if is_long else new_bbo["ask"]

                    if is_long:
                        if best_price > extreme_price:
                            extreme_price = best_price
                    else:
                        if best_price < extreme_price:
                            extreme_price = best_price

                    if is_long:
                        current_profit_pct = (best_price - entry_price) / entry_price
                    else:
                        current_profit_pct = (entry_price - best_price) / entry_price

                    profit_ratio = current_profit_pct / stop_loss_pct if stop_loss_pct > 0 else 0

                    # 追踪止损逻辑
                    if self.config.trailing_stop_enabled and current_profit_pct > 0:
                        if not breakeven_activated and profit_ratio >= self.config.breakeven_activation:
                            stop_price = entry_price
                            breakeven_activated = True
                            log.info(f"[追踪止损] 浮盈达{profit_ratio:.1f}倍保本激活阈值，止损上移至保本价 ${stop_price:.1f}")

                        if profit_ratio >= self.config.trailing_stop_activation:
                            trailing_activated = True
                            if is_long:
                                trailing_stop = extreme_price * (1 - self.config.trailing_stop_callback / 100)
                                if trailing_stop > stop_price:
                                    locked = (trailing_stop - entry_price) / entry_price * 100
                                    log.info(f"[追踪止损] 浮盈{current_profit_pct*100:.2f}%，上调止损至${trailing_stop:.1f} (锁定{locked:.2f}%)")
                                    stop_price = trailing_stop
                            else:
                                trailing_stop = extreme_price * (1 + self.config.trailing_stop_callback / 100)
                                if trailing_stop < stop_price:
                                    locked = (entry_price - trailing_stop) / entry_price * 100
                                    log.info(f"[追踪止损] 浮盈{current_profit_pct*100:.2f}%，下调止损至${trailing_stop:.1f} (锁定{locked:.2f}%)")
                                    stop_price = trailing_stop

                    # 检查止盈
                    if is_long:
                        if best_price >= target_price:
                            log.info(f"[止盈平多] ${best_price:.1f} >= ${target_price:.1f}")
                            exit_reason = "take_profit"
                            break
                    else:
                        if best_price <= target_price:
                            log.info(f"[止盈平空] ${best_price:.1f} <= ${target_price:.1f}")
                            exit_reason = "take_profit"
                            break

                    # 检查止损
                    if is_long:
                        if best_price <= stop_price:
                            if trailing_activated or breakeven_activated:
                                log.info(f"[追踪止损触发] 平多 ${best_price:.1f} <= ${stop_price:.1f}")
                                exit_reason = "trailing_stop"
                            else:
                                log.info(f"[止损平多] ${best_price:.1f} <= ${stop_price:.1f}")
                                exit_reason = "stop_loss"
                            break
                    else:
                        if best_price >= stop_price:
                            if trailing_activated or breakeven_activated:
                                log.info(f"[追踪止损触发] 平空 ${best_price:.1f} >= ${stop_price:.1f}")
                                exit_reason = "trailing_stop"
                            else:
                                log.info(f"[止损平空] ${best_price:.1f} >= ${stop_price:.1f}")
                                exit_reason = "stop_loss"
                            break

                    # 智能持仓延长
                    elapsed = time.time() - wait_start
                    remaining = max_wait - elapsed

                    if self.config.smart_hold_enabled and remaining < 3 and not extension_used:
                        price_range = abs(extreme_price - entry_price) / entry_price * 100
                        is_stagnant = price_range < self.config.early_exit_stagnation_threshold

                        if current_profit_pct > 0 and not is_stagnant:
                            max_wait += self.config.hold_extension_seconds
                            extension_used = True
                            log.info(f"[延长持仓] 盈利趋势良好 (+{current_profit_pct*100:.2f}%)，延长{self.config.hold_extension_seconds:.0f}秒")
                        elif is_stagnant and current_profit_pct <= 0:
                            log.info(f"[提前平仓] 行情停滞 (波动<{self.config.early_exit_stagnation_threshold}%)，提前退出")
                            exit_reason = "early_exit_stagnant"
                            break

                await asyncio.sleep(self.config.check_interval)
            else:
                log.info(f"[超时] {max_wait:.0f}s, 价格: ${best_price:.1f}")

        # 平仓
        close_side = "SELL" if is_long else "BUY"
        close_result = None
        actual_exit_price = best_price
        is_maker_exit = False
        tick_size = 0.1

        # ===== v3.3 Maker优先平仓 (非紧急止损情况) =====
        if self.config.exit_order_type == "limit" and exit_reason not in ["stop_loss", "trailing_stop", "shutdown"]:
            # 获取最新BBO用于挂单
            exit_bbo = await self.client.get_bbo(market)
            if exit_bbo:
                # 做多平仓用ask挂卖单, 做空平仓用bid挂买单 (确保Maker)
                if is_long:
                    exit_limit_price = exit_bbo["ask"] + self.config.exit_price_offset_ticks * tick_size
                else:
                    exit_limit_price = exit_bbo["bid"] - self.config.exit_price_offset_ticks * tick_size
            else:
                exit_limit_price = best_price

            log.info(f"[平仓] Maker优先: 限价{'卖出' if is_long else '买入'} {size} BTC @ ${exit_limit_price:.1f} (offset={self.config.exit_price_offset_ticks})")
            limit_result = await self.client.place_limit_order(
                market=market, side=close_side, size=size,
                price=str(round(exit_limit_price, 1)),
                post_only=self.config.exit_post_only,
                reduce_only=True
            )
            if limit_result:
                fill = await self.client.wait_order_fill(limit_result.get("id"), timeout_seconds=5.0)
                if fill:
                    close_result = fill
                    actual_exit_price = float(fill.get("avg_fill_price", exit_limit_price))
                    is_maker_exit = True
                    log.info(f"  -> [Maker] 限价单成交 @ ${actual_exit_price:.1f}")
                else:
                    log.info("  -> 限价单超时未成交，切换Taker平仓")

        # Taker市价单平仓 (fallback或紧急止损)
        if not close_result:
            exit_type_reason = "止损/追踪止损" if exit_reason in ["stop_loss", "trailing_stop"] else "Maker超时"
            log.info(f"[平仓] Taker: 市价{'卖出' if is_long else '买入'} {size} BTC @ 约${best_price:.1f} ({exit_type_reason})")
            close_result = await self.client.place_market_order(market=market, side=close_side, size=size, reduce_only=True)

        # 兜底平仓 - 从API获取实际持仓大小
        if not close_result:
            positions = await self.client.get_positions(market)
            if positions:
                actual_size = positions[0].get("size", size)
                # 验证size有效性
                try:
                    size_float = float(actual_size)
                    if size_float >= 0.00001:
                        log.info(f"[平仓] 兜底: 市价单 {close_side} {actual_size} BTC")
                        close_result = await self.client.place_market_order(market=market, side=close_side, size=str(actual_size), reduce_only=True)
                    else:
                        log.info(f"[平仓] 持仓已平完 (size={actual_size})")
                except (ValueError, TypeError):
                    log.warning(f"[平仓] 无效的持仓大小: {actual_size}")

        # 计算盈亏
        pnl = 0.0
        if close_result:
            self._record_trade()
            flags = close_result.get("flags", [])
            exit_type_str = "Maker" if is_maker_exit else "Taker"
            if "rpi" in [f.lower() for f in flags]:
                self.rpi_trades += 1
                log.info(f"  -> [{exit_type_str}] RPI! flags={flags}")

            if is_long:
                pnl = (actual_exit_price - entry_price) * float(size)
                pnl_pct = (actual_exit_price - entry_price) / entry_price * 100
            else:
                pnl = (entry_price - actual_exit_price) * float(size)
                pnl_pct = (entry_price - actual_exit_price) / entry_price * 100

            is_win = pnl > 0
            direction_cn = "多" if is_long else "空"
            log.info(f"  -> [{direction_cn}仓] PnL: ${pnl:.4f} ({pnl_pct:+.4f}%) | 出场: {exit_reason}")
            self._record_trade_result(pnl, is_win, direction)
        else:
            log.warning("  -> 平仓失败")

        return True, exit_reason, pnl

    async def run_rpi_cycle(self) -> tuple:
        """执行一个 RPI 交易周期 - 支持双向交易"""
        market = self.config.market
        base_size = self.config.trade_size

        # 限速检查
        can_trade, reason, usage = self._can_trade()
        if not can_trade:
            return False, f"限速: {reason}"

        # 时段检查
        time_ok, time_mult, time_reason = self._check_trading_time()
        if not time_ok:
            return False, f"时段限制: {time_reason}"
        if time_mult < 1.0:
            log.info(f"[时段] {time_reason}")

        # 余额检查
        log.info("[检查] 获取账户余额")
        balance = await self.client.get_balance()
        if not balance:
            return False, "无法获取余额"
        log.info(f"[检查] 余额: {balance:.2f} USDC")

        # 回撤检查
        drawdown_ok, drawdown_reason = self._check_drawdown(balance)
        if not drawdown_ok:
            return False, f"回撤限制: {drawdown_reason}"

        # 获取市场价格
        log.info("[检查] 获取市场价格")
        bbo = await self.client.get_bbo(market)
        if not bbo:
            return False, "无法获取市场价格"

        bid, ask = bbo["bid"], bbo["ask"]
        spread = ask - bid
        spread_pct = spread / bid * 100
        log.info(f"[检查] Spread: ${spread:.2f} ({spread_pct:.4f}%)")

        if spread_pct > self.config.max_spread_pct:
            return False, f"Spread 过大: {spread_pct:.4f}%"

        # 波动率检测
        micro_trend = "flat"
        if self.config.volatility_filter_enabled:
            log.info(f"[波动率] 检测中 (窗口={self.config.volatility_window}秒)")
            vol_data = await self.client.get_price_volatility(market, self.config.volatility_window, 5)
            if vol_data:
                volatility = vol_data["volatility_pct"]
                micro_trend = vol_data["trend"]
                log.info(f"[波动率] {volatility:.4f}% | 趋势: {micro_trend}")
                if volatility > self.config.max_volatility_pct:
                    return False, f"波动率过高: {volatility:.4f}%"
                bid = vol_data["latest_price"]
                self.price_history.extend(vol_data["prices"])
                if len(self.price_history) > 50:
                    self.price_history = self.price_history[-50:]
                new_bbo = await self.client.get_bbo(market)
                if new_bbo:
                    ask = new_bbo["ask"]

        # 获取方向信号
        imbalance = await self.client.get_orderbook_imbalance(market, depth=5)
        direction, confidence = self._get_direction_signal(imbalance)

        if imbalance is not None:
            log.info(f"[订单簿] 不平衡度: {imbalance:.2f} | 方向: {direction} | 置信度: {confidence:.2f}")

        # 方向判断
        if direction == "NEUTRAL":
            return False, f"无明确方向信号 (|{imbalance:.2f}| < {self.config.direction_signal_threshold})"

        # 做空检查
        if direction == "SHORT" and not self.config.enable_short:
            return False, f"做空已禁用，跳过空头信号 (imbalance={imbalance:.2f})"

        # 方向一致性过滤 (v3.2)
        direction_ok, direction_reason = self._check_direction_trend_confirm(direction, micro_trend)
        if not direction_ok:
            return False, f"方向过滤: {direction_reason}"

        # 入场信号确认
        entry_signal = False
        entry_reason = ""

        if direction == "LONG":
            if self.config.entry_mode in ["trend", "hybrid"]:
                if self.config.trend_filter_enabled:
                    prices = [bid]
                    for _ in range(2):
                        await asyncio.sleep(0.2)
                        bbo_check = await self.client.get_bbo(market)
                        if bbo_check:
                            prices.append(bbo_check["bid"])
                    if len(prices) >= 3:
                        trend_up = prices[1] >= prices[0] and prices[2] >= prices[1]
                        if trend_up:
                            entry_signal = True
                            entry_reason = f"趋势上涨+买压: imb={imbalance:.2f}"
                            bid = prices[2]
                            self.price_history.extend(prices)
                            if bbo_check:
                                ask = bbo_check["ask"]
                else:
                    entry_signal = True
                    entry_reason = f"买压信号: imb={imbalance:.2f}"

            if self.config.entry_mode in ["mean_reversion", "hybrid"] and not entry_signal:
                prices = [bid]
                for _ in range(2):
                    await asyncio.sleep(0.2)
                    bbo_check = await self.client.get_bbo(market)
                    if bbo_check:
                        prices.append(bbo_check["bid"])
                if len(prices) >= 3 and prices[2] < prices[0] and imbalance > 0.3:
                    entry_signal = True
                    entry_reason = f"均值回归做多: 跌后强买压"
                    bid = prices[2]
                    if bbo_check:
                        ask = bbo_check["ask"]

        elif direction == "SHORT":
            if self.config.entry_mode in ["trend", "hybrid"]:
                if self.config.trend_filter_enabled:
                    prices = [bid]
                    for _ in range(2):
                        await asyncio.sleep(0.2)
                        bbo_check = await self.client.get_bbo(market)
                        if bbo_check:
                            prices.append(bbo_check["bid"])
                    if len(prices) >= 3:
                        trend_down = prices[1] <= prices[0] and prices[2] <= prices[1]
                        if trend_down:
                            entry_signal = True
                            entry_reason = f"趋势下跌+卖压: imb={imbalance:.2f}"
                            bid = prices[2]
                            self.price_history.extend(prices)
                            if bbo_check:
                                ask = bbo_check["ask"]
                else:
                    entry_signal = True
                    entry_reason = f"卖压信号: imb={imbalance:.2f}"

            if self.config.entry_mode in ["mean_reversion", "hybrid"] and not entry_signal:
                prices = [bid]
                for _ in range(2):
                    await asyncio.sleep(0.2)
                    bbo_check = await self.client.get_bbo(market)
                    if bbo_check:
                        prices.append(bbo_check["bid"])
                if len(prices) >= 3 and prices[2] > prices[0] and imbalance < -0.3:
                    entry_signal = True
                    entry_reason = f"均值回归做空: 涨后强卖压"
                    bid = prices[2]
                    if bbo_check:
                        ask = bbo_check["ask"]

        if not entry_signal:
            return False, f"无入场信号 (方向: {direction}, 模式: {self.config.entry_mode})"

        log.info(f"[入场] {entry_reason}")

        # RSI + 动量信号确认
        if self.config.rsi_filter_enabled and len(self.price_history) >= self.config.rsi_period + 1:
            confirmed, filter_reason = self._is_signal_confirmed(self.price_history, direction)
            if not confirmed:
                return False, f"信号未确认: {filter_reason}"

        # 仓位计算
        size = base_size
        if self.config.kelly_enabled:
            kelly_size = self._calculate_kelly_size(balance, float(base_size))
            size = str(round(self._apply_position_smoothing(kelly_size), 5))  # Paradex要求5位小数

        if time_mult < 1.0:
            size = str(round(float(size) * time_mult, 5))  # Paradex要求5位小数
            log.info(f"[时段] 仓位调整: x{time_mult} -> {size}")

        # 仓位硬上限检查
        leverage = 50
        position_value = float(size) * ask
        max_position_value = balance * self.config.max_position_pct * leverage
        if position_value > max_position_value:
            original_size = size
            size = str(round(max_position_value / ask, 5))  # Paradex要求5位小数
            log.info(f"[仓位上限] 目标仓位超出上限，已缩减为 {size} BTC (原: {original_size} BTC, 上限: {self.config.max_position_pct*100:.0f}%余额)")

        # 余额检查
        required = float(size) * ask / leverage * 1.5
        if balance < required:
            return False, f"余额不足: {balance:.2f} < {required:.2f}"

        # 计算止损止盈
        if self.config.dynamic_stop_loss:
            dynamic_stop = spread_pct * self.config.stop_loss_spread_multiplier
            stop_loss_pct = max(self.config.dynamic_stop_loss_min, min(dynamic_stop, self.config.dynamic_stop_loss_max))
            log.info(f"[开仓] 动态止损={stop_loss_pct*100:.2f}% (spread={spread_pct:.4f}% x {self.config.stop_loss_spread_multiplier}, 范围={self.config.dynamic_stop_loss_min*100:.1f}%-{self.config.dynamic_stop_loss_max*100:.1f}%)")
        else:
            stop_loss_pct = self.config.stop_loss_pct

        take_profit_pct = stop_loss_pct * self.config.risk_reward_ratio
        log.info(f"[开仓] 止盈={take_profit_pct*100:.2f}%, 止损={stop_loss_pct*100:.2f}% (R/R={self.config.risk_reward_ratio})")

        # 执行交易
        success, exit_reason, pnl = await self._execute_trade(
            direction, market, size, bid, ask, stop_loss_pct, take_profit_pct
        )

        # 统计输出
        rpi_rate = (self.rpi_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        win_rate = sum(1 for t in self.recent_trades if t["win"]) / len(self.recent_trades) * 100 if self.recent_trades else 0
        log.info(f"[统计] 交易: {self.total_trades} (多:{self.long_trades}/空:{self.short_trades}) | "
                 f"RPI: {self.rpi_trades} ({rpi_rate:.1f}%) | 胜率: {win_rate:.1f}% | PnL: ${self.total_pnl:.4f}")

        return success, f"周期完成 ({exit_reason})"

    async def _cleanup_on_exit(self):
        log.info("=" * 50)
        log.info("执行退出清理")
        if self.account_manager:
            for idx, client in self.account_manager.clients.items():
                try:
                    if await client.ensure_authenticated():
                        await client.cancel_all_orders(self.config.market)
                        await client.close_all_positions(self.config.market)
                except Exception as e:
                    log.error(f"清理异常: {e}")
        else:
            try:
                await self.client.cancel_all_orders(self.config.market)
                await self.client.close_all_positions(self.config.market)
            except Exception as e:
                log.error(f"清理异常: {e}")
        log.info("清理完成")

    async def _switch_account(self) -> str:
        if not self.account_manager:
            return "switched"
        log.info(f"[{self.account_manager.get_current_account_name()}] 切换账号")
        await self.client.cancel_all_orders(self.config.market)
        await self.client.close_all_positions(self.config.market)
        self.account_manager.save_state()
        result = self.account_manager.switch_to_next_available_account()
        if result == "switched":
            new_client = self.account_manager.get_current_client()
            if new_client:
                self.client = new_client
                self.rate_state = self.account_manager.get_current_rate_state()
                await self.client.authenticate_interactive()
                log.info(f"已切换到 {self.account_manager.get_current_account_name()}")
        return result

    async def run(self):
        global _shutdown_requested
        self.start_time = time.time()

        log.info("=" * 60)
        log.info("RPI Bot v3.3 - Maker优先版")
        log.info("=" * 60)
        log.info(f"市场: {self.config.market}")
        log.info(f"交易大小: {self.config.trade_size} BTC")
        log.info("")
        log.info("v3.3 功能:")
        log.info(f"  - Maker优先入场: {'开启' if self.config.entry_order_type == 'limit' else '关闭'}")
        log.info(f"  - 入场超时重挂: {self.config.entry_order_timeout_seconds}s, 最多{self.config.entry_requote_max}次")
        log.info(f"  - Taker禁入阈值: spread > {self.config.market_spread_limit_pct:.4f}%")
        log.info(f"  - Maker优先平仓: {'开启' if self.config.exit_order_type == 'limit' else '关闭'}")
        log.info(f"  - 做空交易: {'开启' if self.config.enable_short else '关闭'}")
        log.info(f"  - 追踪止损: {'开启' if self.config.trailing_stop_enabled else '关闭'}")
        log.info(f"  - 方向一致性过滤: {'开启' if self.config.direction_trend_confirm else '关闭'}")
        log.info("=" * 60)

        if not await self.client.authenticate_interactive():
            log.error("初始认证失败!")
            return
        log.info("认证成功，开始交易")

        while not _shutdown_requested:
            try:
                log.info("[周期] 开始交易周期")
                success, msg = await self.run_rpi_cycle()

                if "all_accounts_exhausted" in msg:
                    log.warning("所有账号额度已用完")
                    await self._cleanup_on_exit()
                    await self._wait_until_tomorrow()
                    continue

                if "switch" in msg and self.account_manager:
                    result = await self._switch_account()
                    if result == "all_hour_limited":
                        log.info("等待 10 分钟")
                        await asyncio.sleep(600)
                    elif result == "all_day_limited":
                        await self._wait_until_tomorrow()
                    continue

                if not success:
                    log.info(f"[周期] 失败: {msg}")

                await asyncio.sleep(self.config.trade_interval if success else 0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"异常: {e}")
                await asyncio.sleep(1)

        if _shutdown_requested:
            await self._cleanup_on_exit()

        elapsed = time.time() - self.start_time
        log.info("")
        log.info("=" * 60)
        log.info("最终统计")
        log.info("=" * 60)
        log.info(f"运行时间: {elapsed/60:.1f} 分钟")
        log.info(f"总交易: {self.total_trades} (做多: {self.long_trades}, 做空: {self.short_trades})")
        log.info(f"RPI: {self.rpi_trades} ({self.rpi_trades/self.total_trades*100:.1f}%)" if self.total_trades else "RPI: 0")
        log.info(f"累计PnL: ${self.total_pnl:.4f}")
        log.info("=" * 60)

        if self.account_manager:
            self.account_manager.save_state()

    async def _wait_until_tomorrow(self):
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (tomorrow - now).total_seconds()
        log.info(f"等待 {wait_seconds/3600:.1f} 小时")
        await asyncio.sleep(wait_seconds + 60)


# =============================================================================
# 主入口
# =============================================================================

def parse_accounts(accounts_str: str) -> List[AccountInfo]:
    accounts = []
    if not accounts_str:
        return accounts
    for i, pair in enumerate(accounts_str.strip().split(";")):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) == 2:
            pk, addr = parts[0].strip(), parts[1].strip()
            if pk.startswith("0x") and addr.startswith("0x"):
                accounts.append(AccountInfo(pk, addr, f"账号#{i+1}"))
    return accounts


def print_final_config(config: RPIConfig):
    """打印最终生效配置"""
    log.info("")
    log.info("=" * 60)
    log.info("最终生效配置")
    log.info("=" * 60)
    log.info("")
    log.info("[核心交易参数]")
    log.info(f"  trade_size: {config.trade_size}")
    log.info(f"  trade_interval: {config.trade_interval}s")
    log.info(f"  max_spread_pct: {config.max_spread_pct}%")
    log.info(f"  max_wait_seconds: {config.max_wait_seconds}s")
    log.info(f"  check_interval: {config.check_interval}s")
    log.info("")
    log.info("[动态止损]")
    log.info(f"  dynamic_stop_loss: {config.dynamic_stop_loss}")
    log.info(f"  stop_loss_spread_multiplier: {config.stop_loss_spread_multiplier}")
    log.info(f"  dynamic_stop_loss_min: {config.dynamic_stop_loss_min*100:.2f}%")
    log.info(f"  dynamic_stop_loss_max: {config.dynamic_stop_loss_max*100:.2f}%")
    log.info(f"  risk_reward_ratio: {config.risk_reward_ratio}")
    log.info("")
    log.info("[追踪止损]")
    log.info(f"  trailing_stop_enabled: {config.trailing_stop_enabled}")
    log.info(f"  breakeven_activation: {config.breakeven_activation}")
    log.info(f"  trailing_stop_activation: {config.trailing_stop_activation}")
    log.info(f"  trailing_stop_callback: {config.trailing_stop_callback}%")
    log.info("")
    log.info("[波动率过滤]")
    log.info(f"  volatility_filter_enabled: {config.volatility_filter_enabled}")
    log.info(f"  volatility_window: {config.volatility_window}s")
    log.info(f"  max_volatility_pct: {config.max_volatility_pct}%")
    log.info("")
    log.info("[方向信号]")
    log.info(f"  direction_signal_threshold: {config.direction_signal_threshold}")
    log.info(f"  enable_short: {config.enable_short}")
    log.info("")
    log.info("[RSI过滤]")
    log.info(f"  rsi_filter_enabled: {config.rsi_filter_enabled}")
    log.info(f"  rsi_period: {config.rsi_period}")
    log.info(f"  rsi_trend_threshold: {config.rsi_trend_threshold}")
    log.info("")
    log.info("[时段过滤]")
    log.info(f"  time_filter_enabled: {config.time_filter_enabled}")
    log.info(f"  optimal_hours_start: {config.optimal_hours_start}")
    log.info(f"  optimal_hours_end: {config.optimal_hours_end}")
    log.info(f"  weekend_multiplier: {config.weekend_multiplier}")
    log.info("")
    log.info("[Kelly仓位管理]")
    log.info(f"  kelly_enabled: {config.kelly_enabled}")
    log.info(f"  kelly_fraction: {config.kelly_fraction}")
    log.info(f"  max_position_increase_pct: {config.max_position_increase_pct*100:.0f}%")
    log.info(f"  max_position_pct: {config.max_position_pct*100:.0f}% (仓位硬上限)")
    log.info("")
    log.info("[回撤控制]")
    log.info(f"  drawdown_control_enabled: {config.drawdown_control_enabled}")
    log.info(f"  max_daily_loss_pct: {config.max_daily_loss_pct*100:.2f}%")
    log.info(f"  max_total_loss_pct: {config.max_total_loss_pct*100:.2f}%")
    log.info("")
    log.info("[v3.3 Maker优先入场]")
    log.info(f"  entry_order_type: {config.entry_order_type}")
    log.info(f"  entry_post_only: {config.entry_post_only}")
    log.info(f"  entry_price_offset_ticks: {config.entry_price_offset_ticks}")
    log.info(f"  entry_order_timeout_seconds: {config.entry_order_timeout_seconds}s")
    log.info(f"  entry_requote_max: {config.entry_requote_max}")
    log.info(f"  market_spread_limit_pct: {config.market_spread_limit_pct:.4f}% (Taker禁入阈值)")
    log.info("")
    log.info("[v3.3 Maker优先平仓]")
    log.info(f"  exit_order_type: {config.exit_order_type}")
    log.info(f"  exit_post_only: {config.exit_post_only}")
    log.info(f"  exit_price_offset_ticks: {config.exit_price_offset_ticks}")
    log.info("")
    log.info("[v3.2 方向一致性与动量过滤]")
    log.info(f"  direction_trend_confirm: {config.direction_trend_confirm}")
    log.info(f"  price_momentum_window: {config.price_momentum_window}")
    log.info(f"  price_momentum_min_slope: {config.price_momentum_min_slope}%")
    log.info("")
    log.info("=" * 60)


async def main():
    load_dotenv()
    environment = os.getenv("PARADEX_ENVIRONMENT", "prod")
    market = os.getenv("MARKET", "BTC-USD-PERP")

    accounts = parse_accounts(os.getenv("PARADEX_ACCOUNTS", ""))
    account_manager = None
    client = None

    if accounts:
        log.info(f"多账号配置: {len(accounts)} 个")
        account_manager = AccountManager(accounts, environment)
        account_manager.load_state()
        if account_manager.is_account_hour_limited(account_manager.current_index):
            if account_manager.switch_to_next_available_account() == "all_day_limited":
                log.error("所有账号已用完!")
                sys.exit(1)
        client = account_manager.get_current_client()
        if not client:
            log.error("无法初始化账号!")
            sys.exit(1)
    else:
        pk = os.getenv("PARADEX_L2_PRIVATE_KEY")
        addr = os.getenv("PARADEX_L2_ADDRESS")
        if not pk or not addr:
            log.error("请配置账号")
            sys.exit(1)
        client = ParadexInteractiveClient(pk, addr, environment)

    config = RPIConfig(market=market)

    # 基础配置
    config.trade_size = os.getenv("TRADE_SIZE", "0.003").strip()
    config.trade_interval = float(os.getenv("TRADE_INTERVAL", "2.0"))
    config.max_spread_pct = float(os.getenv("MAX_SPREAD_PCT", "0.03"))
    config.max_wait_seconds = float(os.getenv("MAX_WAIT_SECONDS", "30.0"))
    config.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.015"))

    # 限速设置
    config.limits_per_second = int(os.getenv("LIMITS_PER_SECOND", "2"))
    config.limits_per_minute = int(os.getenv("LIMITS_PER_MINUTE", "30"))
    config.limits_per_hour = int(os.getenv("LIMITS_PER_HOUR", "300"))
    config.limits_per_day = int(os.getenv("LIMITS_PER_DAY", "1000"))

    # 任务2: 补齐.env参数读取
    config.check_interval = float(os.getenv("CHECK_INTERVAL", "0.5"))
    config.breakeven_activation = float(os.getenv("BREAKEVEN_ACTIVATION", "1.0"))
    config.volatility_window = int(os.getenv("VOLATILITY_WINDOW", "10"))
    config.early_exit_stagnation_threshold = float(os.getenv("EARLY_EXIT_STAGNATION_THRESHOLD", "0.02"))
    config.time_filter_enabled = os.getenv("TIME_FILTER_ENABLED", "true").lower() == "true"
    config.optimal_hours_start = int(os.getenv("OPTIMAL_HOURS_START", "8"))
    config.optimal_hours_end = int(os.getenv("OPTIMAL_HOURS_END", "21"))
    config.weekend_multiplier = float(os.getenv("WEEKEND_MULTIPLIER", "0.5"))
    config.kelly_fraction = float(os.getenv("KELLY_FRACTION", "0.25"))
    config.max_position_increase_pct = float(os.getenv("MAX_POSITION_INCREASE_PCT", "0.50"))
    config.max_daily_loss_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
    config.max_total_loss_pct = float(os.getenv("MAX_TOTAL_LOSS_PCT", "0.10"))
    config.exit_order_type = os.getenv("EXIT_ORDER_TYPE", "limit")

    # v3.3 Maker优先配置
    config.entry_order_type = os.getenv("ENTRY_ORDER_TYPE", "limit")
    config.entry_post_only = os.getenv("ENTRY_POST_ONLY", "true").lower() == "true"
    config.entry_price_offset_ticks = int(os.getenv("ENTRY_PRICE_OFFSET_TICKS", "0"))
    config.entry_order_timeout_seconds = float(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "3.0"))
    config.entry_requote_max = int(os.getenv("ENTRY_REQUOTE_MAX", "2"))
    config.market_spread_limit_pct = float(os.getenv("MARKET_SPREAD_LIMIT_PCT", "0.006"))
    config.exit_post_only = os.getenv("EXIT_POST_ONLY", "true").lower() == "true"
    config.exit_price_offset_ticks = int(os.getenv("EXIT_PRICE_OFFSET_TICKS", "0"))

    config.direction_trend_confirm = os.getenv("DIRECTION_TREND_CONFIRM", "true").lower() == "true"
    config.price_momentum_window = int(os.getenv("PRICE_MOMENTUM_WINDOW", "6"))
    config.price_momentum_min_slope = float(os.getenv("PRICE_MOMENTUM_MIN_SLOPE", "0.0"))

    client.client_id_format = os.getenv("CLIENT_ID_FORMAT", "rpi")
    config.trend_filter_enabled = os.getenv("TREND_FILTER", "true").lower() == "true"
    config.volatility_filter_enabled = os.getenv("VOLATILITY_FILTER", "true").lower() == "true"
    config.max_volatility_pct = float(os.getenv("MAX_VOLATILITY_PCT", "0.1"))
    config.entry_mode = os.getenv("ENTRY_MODE", "trend")

    # 动态止损
    config.dynamic_stop_loss = os.getenv("DYNAMIC_STOP_LOSS", "true").lower() == "true"
    config.stop_loss_spread_multiplier = float(os.getenv("STOP_LOSS_MULTIPLIER", "2.0"))
    config.dynamic_stop_loss_min = float(os.getenv("DYNAMIC_STOP_LOSS_MIN", "0.01"))
    config.dynamic_stop_loss_max = float(os.getenv("DYNAMIC_STOP_LOSS_MAX", "0.05"))

    # v3.0 增强
    config.trailing_stop_enabled = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
    config.trailing_stop_activation = float(os.getenv("TRAILING_STOP_ACTIVATION", "1.0"))
    config.trailing_stop_callback = float(os.getenv("TRAILING_STOP_CALLBACK", "0.5"))
    config.smart_hold_enabled = os.getenv("SMART_HOLD_ENABLED", "true").lower() == "true"
    config.hold_extension_seconds = float(os.getenv("HOLD_EXTENSION_SECONDS", "15.0"))
    config.rsi_filter_enabled = os.getenv("RSI_FILTER_ENABLED", "true").lower() == "true"
    config.rsi_period = int(os.getenv("RSI_PERIOD", "14"))
    config.rsi_trend_threshold = float(os.getenv("RSI_TREND_THRESHOLD", "50.0"))
    config.rsi_overbought = float(os.getenv("RSI_OVERBOUGHT", "70.0"))
    config.rsi_oversold = float(os.getenv("RSI_OVERSOLD", "30.0"))
    config.position_smoothing_enabled = os.getenv("POSITION_SMOOTHING_ENABLED", "true").lower() == "true"

    # v3.1 做空配置
    config.enable_short = os.getenv("ENABLE_SHORT", "true").lower() == "true"

    # 其他参数
    config.direction_signal_threshold = float(os.getenv("DIRECTION_SIGNAL_THRESHOLD", "0.3"))
    config.risk_reward_ratio = float(os.getenv("RISK_REWARD_RATIO", "2.0"))
    config.kelly_enabled = os.getenv("KELLY_ENABLED", "true").lower() == "true"
    config.max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.30"))
    config.drawdown_control_enabled = os.getenv("DRAWDOWN_CONTROL", "true").lower() == "true"

    # 任务5: 打印最终生效配置
    print_final_config(config)

    bot = RPIBot(client, config, account_manager)

    def signal_handler(sig, frame):
        global _shutdown_requested
        log.info("\n收到退出信号")
        _shutdown_requested = True

    signal.signal(signal.SIGINT, signal_handler)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal_handler)

    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("程序已退出")
