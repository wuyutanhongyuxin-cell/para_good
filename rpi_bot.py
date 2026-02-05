#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RPI Bot - 100% RPI 触发交易机器人 (v3.0 增强版)

核心原理:
    RPI (Rebate Point Index) 只出现在 TAKER 订单上
    使用市价单进行快速买入/卖出，每笔交易都是 TAKER
    配合 interactive token 实现 0 手续费 + RPI 积分

v3.0 增强功能:
    1. 追踪止损 - 浮盈达标后动态上移止损价
    2. 智能持仓 - 根据趋势延长或提前平仓
    3. RSI信号过滤 - 增强入场确认
    4. 仓位平滑 - 避免仓位陡增

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

# 全局退出标志
_shutdown_requested = False

# 配置日志
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

    # 核心参数: 固定交易大小 (BTC)
    trade_size: str = "0.003"

    # 交易间隔 (秒)
    trade_interval: float = 5.0

    # 市场
    market: str = "BTC-USD-PERP"

    # 限速设置
    limits_per_second: int = 2
    limits_per_minute: int = 30
    limits_per_hour: int = 300
    limits_per_day: int = 1000

    # Spread 优化配置
    max_spread_pct: float = 0.03

    # 止盈止损配置
    stop_loss_pct: float = 0.015

    # 止盈配置
    min_profit_pct: float = 0.005
    max_wait_seconds: float = 30.0  # 从15秒增加到30秒
    check_interval: float = 0.5

    # 趋势过滤
    trend_filter_enabled: bool = True
    orderbook_imbalance_threshold: float = 0.0

    # 波动率过滤
    volatility_filter_enabled: bool = True
    max_volatility_pct: float = 0.1
    volatility_window: int = 10

    # 入场模式
    entry_mode: str = "trend"

    # 动态止损 (默认开启)
    dynamic_stop_loss: bool = True
    stop_loss_spread_multiplier: float = 2.0
    dynamic_stop_loss_min: float = 0.01  # 1%
    dynamic_stop_loss_max: float = 0.05  # 5%

    # ===== 追踪止损配置 =====
    trailing_stop_enabled: bool = True
    trailing_stop_activation: float = 1.0  # 激活倍数
    trailing_stop_callback: float = 0.5  # 回调比例 (%)
    breakeven_activation: float = 1.0

    # ===== 智能持仓延长配置 =====
    smart_hold_enabled: bool = True
    hold_extension_seconds: float = 15.0
    early_exit_stagnation_threshold: float = 0.02

    # ===== RSI 信号过滤配置 =====
    rsi_filter_enabled: bool = True
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    rsi_trend_threshold: float = 50.0

    # ===== 研究优化参数 =====
    exit_order_type: str = "limit"
    direction_signal_threshold: float = 0.3
    risk_reward_ratio: float = 2.0

    # 时段过滤
    time_filter_enabled: bool = True
    optimal_hours_start: int = 8
    optimal_hours_end: int = 21
    weekend_multiplier: float = 0.5

    # Kelly准则仓位管理
    kelly_enabled: bool = True
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.30  # 从0.20提高到0.30

    # ===== 仓位平滑配置 =====
    position_smoothing_enabled: bool = True
    max_position_increase_pct: float = 0.50  # 50%

    # 回撤控制
    drawdown_control_enabled: bool = True
    max_daily_loss_pct: float = 0.03
    max_total_loss_pct: float = 0.10

    enabled: bool = True


@dataclass
class RateLimitState:
    """限速状态"""
    day: str = ""
    trades: List[int] = field(default_factory=list)


@dataclass
class AccountInfo:
    """账号信息"""
    l2_private_key: str
    l2_address: str
    name: str = ""


# =============================================================================
# Paradex API 客户端
# =============================================================================

class ParadexInteractiveClient:
    """Paradex API 客户端"""

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
                        token_usage = decoded.get("token_usage", "unknown")
                        log.info(f"认证成功! token_usage={token_usage}")
                        return True
                    else:
                        error = await resp.text()
                        log.error(f"认证失败: {resp.status} - {error}")
                        return False
        except Exception as e:
            log.error(f"认证异常: {e}")
            return False

    async def ensure_authenticated(self) -> bool:
        now = int(time.time())
        if self.jwt_token and self.jwt_expires_at > now + 60:
            return True
        log.info("Token 已过期或不存在，重新认证...")
        return await self.authenticate_interactive()

    def _generate_client_id(self) -> str:
        import uuid
        ts = int(time.time() * 1000)
        if self.client_id_format == "rpi":
            return f"rpi_{ts}"
        elif self.client_id_format == "timestamp":
            return str(ts)
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
                url = f"{self.base_url}/balance"
                async with session.get(url, headers=self._get_auth_headers()) as resp:
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
                url = f"{self.base_url}/positions"
                async with session.get(url, headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        positions = data.get("results", [])
                        if market:
                            positions = [p for p in positions if p.get("market") == market]
                        return [p for p in positions if p.get("status") != "CLOSED" and float(p.get("size", 0)) > 0]
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
                url = f"{self.base_url}/orderbook/{market}?depth=1"
                async with session.get(url, headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bids = data.get("bids", [])
                        asks = data.get("asks", [])
                        best_bid = data.get("best_bid_api") or (bids[0] if bids else None)
                        best_ask = data.get("best_ask_api") or (asks[0] if asks else None)
                        if best_bid and best_ask:
                            return {
                                "bid": float(best_bid[0]), "ask": float(best_ask[0]),
                                "bid_size": float(best_bid[1]), "ask_size": float(best_ask[1]),
                            }
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
                url = f"{self.base_url}/orderbook/{market}?depth={depth}"
                async with session.get(url, headers=self._get_auth_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bids = data.get("bids", [])
                        asks = data.get("asks", [])
                        total_bid_size = sum(float(b[1]) for b in bids[:depth])
                        total_ask_size = sum(float(a[1]) for a in asks[:depth])
                        if total_bid_size + total_ask_size == 0:
                            return 0
                        return (total_bid_size - total_ask_size) / (total_bid_size + total_ask_size)
            return None
        except Exception as e:
            log.error(f"获取订单簿不平衡度失败: {e}")
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
            min_price, max_price = min(prices), max(prices)
            avg_price = sum(prices) / len(prices)
            volatility_pct = (max_price - min_price) / avg_price * 100
            price_change = prices[-1] - prices[0]
            trend = "up" if price_change > 0 else "down" if price_change < 0 else "flat"
            return {
                "volatility_pct": volatility_pct, "trend": trend, "price_change": price_change,
                "price_change_pct": (price_change / prices[0]) * 100 if prices[0] > 0 else 0,
                "prices": prices, "latest_price": prices[-1]
            }
        except Exception as e:
            log.error(f"获取波动率失败: {e}")
            return None

    async def place_market_order(self, market: str, side: str, size: str, reduce_only: bool = False) -> Optional[Dict]:
        try:
            if not await self.ensure_authenticated():
                return None
            from paradex_py.common.order import Order, OrderSide, OrderType
            from decimal import Decimal
            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell
            order = Order(
                market=market, order_type=OrderType.Market, order_side=order_side,
                size=Decimal(size), client_id=self._generate_client_id(),
                reduce_only=reduce_only, signature_timestamp=int(time.time() * 1000),
            )
            order.signature = self.paradex.account.sign_order(order)
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = f"{self.base_url}/orders"
                payload = order.dump_to_dict()
                async with session.post(url, headers=self._get_auth_headers(), json=payload) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        log.info(f"市价单成功: {side} {size} BTC, order_id={result.get('id')}")
                        return result
                    else:
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
            from decimal import Decimal
            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell
            instruction = "POST_ONLY" if post_only else "GTC"
            order = Order(
                market=market, order_type=OrderType.Limit, order_side=order_side,
                size=Decimal(size), limit_price=Decimal(price), client_id=self._generate_client_id(),
                reduce_only=reduce_only, instruction=instruction, signature_timestamp=int(time.time() * 1000),
            )
            order.signature = self.paradex.account.sign_order(order)
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = f"{self.base_url}/orders"
                payload = order.dump_to_dict()
                async with session.post(url, headers=self._get_auth_headers(), json=payload) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        log.info(f"限价单成功: {side} {size} @ ${price}, order_id={result.get('id')}")
                        return result
                    else:
                        error = await resp.text()
                        log.error(f"限价单失败: {resp.status} - {error}")
                        return None
        except Exception as e:
            log.error(f"限价单失败: {e}")
            return None

    async def wait_order_fill(self, order_id: str, timeout_seconds: float = 5.0) -> Optional[Dict]:
        try:
            import aiohttp
            start_time = time.time()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds + 2)) as session:
                while time.time() - start_time < timeout_seconds:
                    url = f"{self.base_url}/orders/{order_id}"
                    async with session.get(url, headers=self._get_auth_headers()) as resp:
                        if resp.status == 200:
                            order = await resp.json()
                            status = order.get("status", "")
                            if status == "CLOSED":
                                return order
                            elif status in ["CANCELED", "REJECTED"]:
                                return None
                    await asyncio.sleep(0.2)
            await self.cancel_order(order_id)
            return None
        except Exception as e:
            log.error(f"等待订单成交失败: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                url = f"{self.base_url}/orders/{order_id}"
                async with session.delete(url, headers=self._get_auth_headers()) as resp:
                    return resp.status in [200, 204]
        except Exception as e:
            log.error(f"取消订单失败: {e}")
            return False

    async def cancel_all_orders(self, market: str = None) -> int:
        try:
            if not await self.ensure_authenticated():
                return 0
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = f"{self.base_url}/orders"
                params = {"status": "OPEN"}
                if market:
                    params["market"] = market
                async with session.get(url, headers=self._get_auth_headers(), params=params) as resp:
                    if resp.status != 200:
                        return 0
                    data = await resp.json()
                    orders = data.get("results", [])
                if not orders:
                    return 0
                cancelled = 0
                for order in orders:
                    order_id = order.get("id")
                    if order_id:
                        cancel_url = f"{self.base_url}/orders/{order_id}"
                        async with session.delete(cancel_url, headers=self._get_auth_headers()) as cancel_resp:
                            if cancel_resp.status in [200, 204]:
                                cancelled += 1
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
            if not positions:
                return 0
            closed = 0
            for pos in positions:
                pos_market = pos.get("market")
                if market and pos_market != market:
                    continue
                size = pos.get("size", "0")
                side = pos.get("side", "")
                if float(size) == 0:
                    continue
                close_side = "SELL" if side == "LONG" else "BUY"
                result = await self.place_market_order(market=pos_market, side=close_side, size=size, reduce_only=True)
                if result:
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
        self.rate_states: Dict[int, RateLimitState] = {}
        self.daily_limits = 1000
        for i in range(len(accounts)):
            self.rate_states[i] = RateLimitState()
        log.info(f"账号管理器: 共 {len(accounts)} 个账号")

    def get_current_client(self) -> Optional[ParadexInteractiveClient]:
        if self.current_index >= len(self.accounts):
            return None
        if self.current_index not in self.clients:
            account = self.accounts[self.current_index]
            try:
                client = ParadexInteractiveClient(
                    l2_private_key=account.l2_private_key,
                    l2_address=account.l2_address,
                    environment=self.environment
                )
                self.clients[self.current_index] = client
                log.info(f"已加载账号 #{self.current_index + 1}: {account.name or account.l2_address[:10]}...")
            except Exception as e:
                log.error(f"加载账号 #{self.current_index + 1} 失败: {e}")
                return None
        return self.clients[self.current_index]

    def get_current_rate_state(self) -> RateLimitState:
        return self.rate_states[self.current_index]

    def get_current_account_name(self) -> str:
        if self.current_index >= len(self.accounts):
            return "无可用账号"
        account = self.accounts[self.current_index]
        return account.name or f"账号#{self.current_index + 1}"

    def _count_hour_trades(self, account_index: int) -> int:
        state = self.rate_states[account_index]
        cutoff = int(time.time() * 1000) - 3600000
        return len([t for t in state.trades if t > cutoff])

    def is_account_hour_limited(self, account_index: int) -> bool:
        return self._count_hour_trades(account_index) >= 300

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
        has_day_available = any(
            self.rate_states[i].day != today or len(self.rate_states[i].trades) < self.daily_limits
            for i in range(len(self.accounts))
        )
        return "all_hour_limited" if has_day_available else "all_day_limited"

    def all_accounts_exhausted(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        return all(
            self.rate_states[i].day == today and len(self.rate_states[i].trades) >= self.daily_limits
            for i in range(len(self.accounts))
        )

    def save_state(self, filepath: str = "rpi_account_states.json"):
        data = {
            "current_index": self.current_index,
            "rate_states": {
                str(i): {"day": state.day, "trades": state.trades[-1000:]}
                for i, state in self.rate_states.items()
            }
        }
        try:
            with open(filepath, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            log.error(f"保存状态失败: {e}")

    def load_state(self, filepath: str = "rpi_account_states.json"):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    self.current_index = data.get("current_index", 0)
                    for i_str, state_data in data.get("rate_states", {}).items():
                        i = int(i_str)
                        if i < len(self.accounts):
                            self.rate_states[i] = RateLimitState(
                                day=state_data.get("day", ""),
                                trades=state_data.get("trades", [])
                            )
                log.info(f"已加载状态，当前账号: {self.get_current_account_name()}")
        except Exception as e:
            log.warning(f"加载状态失败: {e}")


# =============================================================================
# RPI 交易机器人 (v3.0 增强版)
# =============================================================================

class RPIBot:
    """RPI 机器人 v3.0 增强版"""

    def __init__(self, client: ParadexInteractiveClient, config: RPIConfig,
                 account_manager: Optional[AccountManager] = None):
        self.client = client
        self.config = config
        self.account_manager = account_manager
        self.rate_state = RateLimitState()

        self.total_trades = 0
        self.rpi_trades = 0
        self.start_time = None
        self.session_start_balance: Optional[float] = None
        self.daily_start_balance: Optional[float] = None
        self.current_day: str = ""
        self.recent_trades: List[Dict] = []
        self.total_pnl: float = 0.0

        # v3.0: 仓位平滑
        self.last_position_size: Optional[float] = None
        # v3.0: RSI价格历史
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
            usage["account"] = self.account_manager.get_current_account_name()
            if usage["day"] >= self.config.limits_per_day:
                if self.account_manager.all_accounts_exhausted():
                    return False, "all_accounts_exhausted", usage
                return False, "day_limit_switch", usage
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

    def _record_trade_result(self, pnl: float, win: bool):
        self.recent_trades.append({"time": time.time(), "pnl": pnl, "win": win})
        self.total_pnl += pnl
        if len(self.recent_trades) > 100:
            self.recent_trades = self.recent_trades[-100:]

    def _check_trading_time(self) -> tuple:
        if not self.config.time_filter_enabled:
            return True, 1.0, "时段过滤关闭"
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        hour = now.hour
        weekday = now.weekday()
        if weekday >= 5:
            mult = self.config.weekend_multiplier
            return True, mult, f"周末, 仓位x{mult}"
        if self.config.optimal_hours_start <= hour < self.config.optimal_hours_end:
            if 13 <= hour < 17:
                return True, 1.0, "黄金时段"
            return True, 0.8, "良好时段"
        return False, 0.3, f"低流动性时段 (UTC {hour}:00)"

    def _calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """计算RSI指标"""
        if len(prices) < period + 1:
            return None
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        recent_deltas = deltas[-(period):]
        gains = [d if d > 0 else 0 for d in recent_deltas]
        losses = [-d if d < 0 else 0 for d in recent_deltas]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _is_signal_confirmed(self, prices: List[float], direction: str, imbalance: Optional[float] = None) -> tuple:
        """确认入场信号 (RSI过滤)"""
        if not self.config.rsi_filter_enabled:
            return True, "RSI过滤已关闭"
        rsi = self._calculate_rsi(prices, self.config.rsi_period)
        if rsi is None:
            log.info(f"[信号过滤] 价格数据不足，无法计算RSI")
            return True, "RSI数据不足，跳过过滤"

        if self.config.entry_mode == "trend":
            if direction == "LONG":
                if rsi > self.config.rsi_trend_threshold:
                    if len(prices) >= 3:
                        recent_change = prices[-1] - prices[-2]
                        prev_change = prices[-2] - prices[-3]
                        if recent_change >= prev_change * 0.5:
                            log.info(f"[信号过滤] RSI={rsi:.1f} 满足做多条件，确认入场")
                            return True, f"RSI={rsi:.1f} 满足做多条件"
                        else:
                            log.info(f"[信号过滤] RSI={rsi:.1f} 但趋势放缓，不确认")
                            return False, f"RSI={rsi:.1f} 趋势放缓"
                    log.info(f"[信号过滤] RSI={rsi:.1f} 满足做多条件，确认入场")
                    return True, f"RSI={rsi:.1f} 满足做多条件"
                else:
                    log.info(f"[信号过滤] RSI={rsi:.1f} < {self.config.rsi_trend_threshold}，不满足做多条件")
                    return False, f"RSI={rsi:.1f} 低于阈值"
        elif self.config.entry_mode == "mean_reversion":
            if direction == "LONG" and rsi < self.config.rsi_oversold:
                log.info(f"[信号过滤] RSI={rsi:.1f} 超卖，确认做多反转")
                return True, f"RSI={rsi:.1f} 超卖反转"
            log.info(f"[信号过滤] RSI={rsi:.1f} 不在超买超卖区域")
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
        p = win_rate
        q = 1 - p
        kelly = (b * p - q) / b if b > 0 else 0
        adjusted_kelly = kelly * self.config.kelly_fraction
        if adjusted_kelly <= 0:
            log.warning(f"[Kelly] 负期望值! 胜率={win_rate:.1%}, R/R={b:.2f}")
            return base_size * 0.5
        log.info(f"[Kelly] 胜率={win_rate:.1%}, R/R={b:.2f}, Kelly={adjusted_kelly:.2%}")
        return min(base_size * (1 + adjusted_kelly), base_size * 2)

    def _apply_position_smoothing(self, target_size: float) -> float:
        """应用仓位平滑机制"""
        if not self.config.position_smoothing_enabled:
            return target_size
        if self.last_position_size is None:
            self.last_position_size = target_size
            return target_size
        increase_ratio = (target_size - self.last_position_size) / self.last_position_size if self.last_position_size > 0 else 0
        if increase_ratio > self.config.max_position_increase_pct:
            smoothed_size = self.last_position_size * (1 + self.config.max_position_increase_pct)
            log.info(f"[仓位管理] Kelly建议仓位{target_size:.6f} BTC，平滑后实际下单{smoothed_size:.6f} BTC")
            self.last_position_size = smoothed_size
            return smoothed_size
        self.last_position_size = target_size
        return target_size

    def _check_drawdown(self, current_balance: float) -> tuple:
        if not self.config.drawdown_control_enabled:
            return True, ""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.current_day != today:
            self.current_day = today
            self.daily_start_balance = current_balance
        if self.session_start_balance is None:
            self.session_start_balance = current_balance
            self.daily_start_balance = current_balance
        if self.daily_start_balance and self.daily_start_balance > 0:
            daily_loss = (self.daily_start_balance - current_balance) / self.daily_start_balance
            if daily_loss >= self.config.max_daily_loss_pct:
                return False, f"日内回撤 {daily_loss:.1%} >= {self.config.max_daily_loss_pct:.1%}"
        if self.session_start_balance and self.session_start_balance > 0:
            total_loss = (self.session_start_balance - current_balance) / self.session_start_balance
            if total_loss >= self.config.max_total_loss_pct:
                return False, f"总回撤 {total_loss:.1%} >= {self.config.max_total_loss_pct:.1%}"
        return True, ""

    def _get_direction_signal(self, imbalance: Optional[float]) -> tuple:
        if imbalance is None:
            return "NEUTRAL", 0.0
        threshold = self.config.direction_signal_threshold
        if imbalance > threshold:
            return "LONG", abs(imbalance)
        elif imbalance < -threshold:
            return "SHORT", abs(imbalance)
        return "NEUTRAL", abs(imbalance)

    async def run_rpi_cycle(self) -> tuple:
        """执行一个 RPI 交易周期 (v3.0 增强版)"""
        market = self.config.market
        base_size = self.config.trade_size

        can_trade, reason, usage = self._can_trade()
        if not can_trade:
            return False, f"限速: {reason}"

        time_ok, time_mult, time_reason = self._check_trading_time()
        if not time_ok:
            return False, f"时段限制: {time_reason}"
        if time_mult < 1.0:
            log.info(f"[时段] {time_reason}")

        log.info("[检查] 获取账户余额...")
        balance = await self.client.get_balance()
        if not balance:
            return False, "无法获取余额"
        log.info(f"[检查] 余额: {balance:.2f} USDC")

        drawdown_ok, drawdown_reason = self._check_drawdown(balance)
        if not drawdown_ok:
            return False, f"回撤限制: {drawdown_reason}"

        log.info("[检查] 获取市场价格...")
        bbo = await self.client.get_bbo(market)
        if not bbo:
            return False, "无法获取市场价格"

        bid, ask = bbo["bid"], bbo["ask"]
        spread = ask - bid
        spread_pct = spread / bid * 100
        log.info(f"[检查] Spread: ${spread:.2f} ({spread_pct:.4f}%) | 限制: {self.config.max_spread_pct}%")

        if spread_pct > self.config.max_spread_pct:
            return False, f"Spread 过大: {spread_pct:.4f}%"

        if self.config.volatility_filter_enabled:
            log.info(f"[波动率] 检测中 ({self.config.volatility_window}s)...")
            vol_data = await self.client.get_price_volatility(market, window_seconds=self.config.volatility_window, samples=5)
            if vol_data:
                volatility = vol_data["volatility_pct"]
                trend = vol_data["trend"]
                price_change_pct = vol_data["price_change_pct"]
                log.info(f"[波动率] {volatility:.4f}% | 趋势: {trend} | 变化: {price_change_pct:+.4f}%")
                if volatility > self.config.max_volatility_pct:
                    return False, f"波动率过高: {volatility:.4f}% > {self.config.max_volatility_pct}%"
                bid = vol_data["latest_price"]
                self.price_history.extend(vol_data["prices"])
                if len(self.price_history) > 50:
                    self.price_history = self.price_history[-50:]
                new_bbo = await self.client.get_bbo(market)
                if new_bbo:
                    ask = new_bbo["ask"]

        imbalance = await self.client.get_orderbook_imbalance(market, depth=5)
        direction, confidence = self._get_direction_signal(imbalance)

        if imbalance is not None:
            log.info(f"[订单簿] 不平衡度: {imbalance:.2f} | 方向: {direction} | 置信度: {confidence:.2f}")
            if direction == "NEUTRAL":
                return False, f"无明确方向信号 (|{imbalance:.2f}| < {self.config.direction_signal_threshold})"
            if direction == "SHORT":
                return False, f"卖压过大，跳过 (imbalance={imbalance:.2f})"

        entry_signal = False
        entry_reason = ""

        if self.config.entry_mode in ["trend", "hybrid"]:
            if self.config.trend_filter_enabled:
                prices = [bid]
                for i in range(2):
                    await asyncio.sleep(0.2)
                    bbo_check = await self.client.get_bbo(market)
                    if bbo_check:
                        prices.append(bbo_check["bid"])
                if len(prices) >= 3:
                    trend_up = prices[1] >= prices[0] and prices[2] >= prices[1]
                    total_change = prices[2] - prices[0]
                    if trend_up and total_change >= 0:
                        entry_signal = True
                        entry_reason = f"趋势上涨+买压: +${total_change:.2f}, imb={imbalance:.2f}"
                        bid = prices[2]
                        self.price_history.extend(prices)
                        if len(self.price_history) > 50:
                            self.price_history = self.price_history[-50:]
                        if bbo_check:
                            ask = bbo_check["ask"]
            else:
                entry_signal = True
                entry_reason = f"买压信号: imb={imbalance:.2f}"

        if self.config.entry_mode in ["mean_reversion", "hybrid"] and not entry_signal:
            prices = [bid]
            for i in range(2):
                await asyncio.sleep(0.2)
                bbo_check = await self.client.get_bbo(market)
                if bbo_check:
                    prices.append(bbo_check["bid"])
            if len(prices) >= 3:
                price_dropped = prices[2] < prices[0]
                if price_dropped and imbalance is not None and imbalance > 0.3:
                    entry_signal = True
                    entry_reason = f"均值回归: 跌${prices[0]-prices[2]:.2f}, 强买压={imbalance:.2f}"
                    bid = prices[2]
                    self.price_history.extend(prices)
                    if len(self.price_history) > 50:
                        self.price_history = self.price_history[-50:]
                    if bbo_check:
                        ask = bbo_check["ask"]

        if not entry_signal:
            return False, f"无入场信号 (模式: {self.config.entry_mode})"

        log.info(f"[入场] {entry_reason}")

        # RSI信号过滤
        if self.config.rsi_filter_enabled and len(self.price_history) >= self.config.rsi_period + 1:
            signal_confirmed, filter_reason = self._is_signal_confirmed(self.price_history, direction, imbalance)
            if not signal_confirmed:
                return False, f"信号未确认: {filter_reason}"

        # Kelly仓位计算 + 仓位平滑
        size = base_size
        if self.config.kelly_enabled:
            kelly_size = self._calculate_kelly_size(balance, float(base_size))
            smoothed_size = self._apply_position_smoothing(kelly_size)
            size = str(round(smoothed_size, 6))

        if time_mult < 1.0:
            adjusted_size = float(size) * time_mult
            size = str(round(adjusted_size, 6))
            log.info(f"[时段] 仓位调整: x{time_mult} -> {size}")

        leverage = 50
        required = float(size) * ask / leverage * 1.5
        if balance < required:
            return False, f"余额不足: {balance:.2f} < {required:.2f} USD"

        # 动态止损 (限制在1%-5%)
        if self.config.dynamic_stop_loss:
            dynamic_stop_pct = spread_pct * self.config.stop_loss_spread_multiplier
            stop_loss_pct = max(self.config.dynamic_stop_loss_min, min(dynamic_stop_pct, self.config.dynamic_stop_loss_max))
            log.info(f"[开仓] 计算动态止损={stop_loss_pct*100:.2f}% (限制在{self.config.dynamic_stop_loss_min*100:.0f}%-{self.config.dynamic_stop_loss_max*100:.0f}%)")
        else:
            stop_loss_pct = self.config.stop_loss_pct

        take_profit_pct = stop_loss_pct * self.config.risk_reward_ratio
        log.info(f"[开仓] 止盈={take_profit_pct*100:.2f}%, 止损={stop_loss_pct*100:.2f}% (R/R={self.config.risk_reward_ratio})")

        log.info(f"[开仓] 市价买入 {size} BTC @ ~${ask:.1f}...")
        buy_result = await self.client.place_market_order(market=market, side="BUY", size=size, reduce_only=False)

        if not buy_result:
            return False, "买入失败"

        self._record_trade()
        entry_price = ask
        buy_flags = buy_result.get("flags", [])
        if "rpi" in [f.lower() for f in buy_flags]:
            self.rpi_trades += 1
            log.info(f"  -> RPI! flags={buy_flags}")
        else:
            log.info(f"  -> flags={buy_flags}")

        # 等待出场 (含追踪止损和智能持仓延长)
        best_bid = bid
        exit_reason = "instant"
        highest_price = entry_price

        if self.config.max_wait_seconds <= 0:
            log.info("[极速] 立即平仓模式")
        else:
            target_price = entry_price * (1 + take_profit_pct)
            stop_price = entry_price * (1 - stop_loss_pct)
            breakeven_activated = False
            trailing_activated = False

            log.info(f"[等待] 止盈: ${target_price:.1f} (+{take_profit_pct*100:.2f}%) | 止损: ${stop_price:.1f} (-{stop_loss_pct*100:.2f}%)")

            wait_start = time.time()
            max_wait = self.config.max_wait_seconds
            exit_reason = "timeout"
            extension_used = False

            while time.time() - wait_start < max_wait:
                if _shutdown_requested:
                    exit_reason = "shutdown"
                    break

                new_bbo = await self.client.get_bbo(market)
                if new_bbo:
                    best_bid = new_bbo["bid"]
                    if best_bid > highest_price:
                        highest_price = best_bid

                    current_profit_pct = (best_bid - entry_price) / entry_price
                    profit_ratio = current_profit_pct / stop_loss_pct if stop_loss_pct > 0 else 0

                    # 追踪止损逻辑
                    if self.config.trailing_stop_enabled and current_profit_pct > 0:
                        if not breakeven_activated and profit_ratio >= self.config.breakeven_activation:
                            stop_price = entry_price
                            breakeven_activated = True
                            log.info(f"[追踪止损] 浮盈达{profit_ratio:.1f}倍止损，止损上移至保本价 ${stop_price:.1f}")

                        if profit_ratio >= self.config.trailing_stop_activation:
                            trailing_activated = True
                            trailing_stop = highest_price * (1 - self.config.trailing_stop_callback / 100)
                            if trailing_stop > stop_price:
                                locked_profit_pct = (trailing_stop - entry_price) / entry_price * 100
                                log.info(f"[追踪止损] 浮盈达{current_profit_pct*100:.2f}%，上调止损价至${trailing_stop:.1f} (锁定盈利{locked_profit_pct:.2f}%)")
                                stop_price = trailing_stop

                    if best_bid >= target_price:
                        log.info(f"[止盈] ${best_bid:.1f} >= ${target_price:.1f}")
                        exit_reason = "take_profit"
                        break

                    if best_bid <= stop_price:
                        if trailing_activated or breakeven_activated:
                            log.info(f"[追踪止损触发] ${best_bid:.1f} <= ${stop_price:.1f}")
                            exit_reason = "trailing_stop"
                        else:
                            log.info(f"[止损] ${best_bid:.1f} <= ${stop_price:.1f}")
                            exit_reason = "stop_loss"
                        break

                    # 智能持仓延长逻辑
                    elapsed = time.time() - wait_start
                    remaining = max_wait - elapsed

                    if self.config.smart_hold_enabled and remaining < 3 and not extension_used:
                        price_range = abs(highest_price - entry_price) / entry_price * 100
                        is_stagnant = price_range < self.config.early_exit_stagnation_threshold

                        if current_profit_pct > 0 and not is_stagnant:
                            max_wait += self.config.hold_extension_seconds
                            extension_used = True
                            log.info(f"[延长持仓] 临近超时但盈利趋势良好 (+{current_profit_pct*100:.2f}%)，延长等待{self.config.hold_extension_seconds:.0f}秒...")
                        elif is_stagnant and current_profit_pct <= 0:
                            log.info(f"[提前平仓] 行情停滞 (波动<{self.config.early_exit_stagnation_threshold}%)，提前退出")
                            exit_reason = "early_exit_stagnant"
                            break

                await asyncio.sleep(self.config.check_interval)
            else:
                log.info(f"[超时] {max_wait:.0f}s, Bid: ${best_bid:.1f}, 最高价: ${highest_price:.1f}")

        # 平仓
        sell_result = None
        actual_exit_price = best_bid

        if self.config.exit_order_type == "limit" and exit_reason not in ["stop_loss", "trailing_stop"]:
            log.info(f"[平仓] 限价单卖出 {size} BTC @ ${best_bid:.1f} (POST_ONLY)...")
            limit_result = await self.client.place_limit_order(
                market=market, side="SELL", size=size, price=str(round(best_bid, 1)),
                post_only=True, reduce_only=True
            )
            if limit_result:
                order_id = limit_result.get("id")
                fill_result = await self.client.wait_order_fill(order_id, timeout_seconds=5.0)
                if fill_result:
                    sell_result = fill_result
                    actual_exit_price = float(fill_result.get("avg_fill_price", best_bid))
                    log.info(f"  -> 限价单成交 @ ${actual_exit_price:.1f}")
                else:
                    log.info("  -> 限价单超时，切换市价单...")

        if not sell_result:
            log.info(f"[平仓] 市价卖出 {size} BTC @ ~${best_bid:.1f}...")
            sell_result = await self.client.place_market_order(market=market, side="SELL", size=size, reduce_only=True)

        if not sell_result:
            positions = await self.client.get_positions(market)
            if positions:
                actual_size = positions[0].get("size", size)
                sell_result = await self.client.place_market_order(market=market, side="SELL", size=str(actual_size), reduce_only=True)

        if sell_result:
            self._record_trade()
            sell_flags = sell_result.get("flags", [])
            if "rpi" in [f.lower() for f in sell_flags]:
                self.rpi_trades += 1
                log.info(f"  -> RPI! flags={sell_flags}")
            pnl = (actual_exit_price - entry_price) * float(size)
            pnl_pct = (actual_exit_price - entry_price) / entry_price * 100
            is_win = pnl > 0
            log.info(f"  -> PnL: ${pnl:.4f} ({pnl_pct:+.4f}%) | 出场: {exit_reason}")
            self._record_trade_result(pnl, is_win)
        else:
            log.warning("  -> 平仓失败")

        rpi_rate = (self.rpi_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        win_rate = sum(1 for t in self.recent_trades if t["win"]) / len(self.recent_trades) * 100 if self.recent_trades else 0
        log.info(f"[统计] 交易: {self.total_trades} | RPI: {self.rpi_trades} ({rpi_rate:.1f}%) | 胜率: {win_rate:.1f}% | 累计PnL: ${self.total_pnl:.4f}")

        return True, f"周期完成 ({exit_reason})"

    async def _cleanup_on_exit(self):
        log.info("=" * 50)
        log.info("执行退出清理...")
        if self.account_manager:
            for idx, client in self.account_manager.clients.items():
                account_name = self.account_manager.accounts[idx].name or f"账号#{idx + 1}"
                log.info(f"[{account_name}] 清理中...")
                try:
                    if await client.ensure_authenticated():
                        await client.cancel_all_orders(self.config.market)
                        await client.close_all_positions(self.config.market)
                except Exception as e:
                    log.error(f"[{account_name}] 清理异常: {e}")
        else:
            try:
                await self.client.cancel_all_orders(self.config.market)
                await self.client.close_all_positions(self.config.market)
            except Exception as e:
                log.error(f"清理异常: {e}")
        log.info("清理完成")
        log.info("=" * 50)

    async def _switch_account(self) -> str:
        if not self.account_manager:
            return "switched"
        current = self.account_manager.get_current_account_name()
        log.info(f"[{current}] 切换账号...")
        await self.client.cancel_all_orders(self.config.market)
        await self.client.close_all_positions(self.config.market)
        self.account_manager.save_state()
        result = self.account_manager.switch_to_next_available_account()
        if result == "switched":
            new_client = self.account_manager.get_current_client()
            if new_client:
                self.client = new_client
                self.rate_state = self.account_manager.get_current_rate_state()
                if await self.client.authenticate_interactive():
                    log.info(f"已切换到 {self.account_manager.get_current_account_name()}")
                else:
                    log.error("新账号认证失败!")
        return result

    async def run(self):
        global _shutdown_requested
        self.start_time = time.time()
        log.info("=" * 60)
        log.info("RPI Bot - 100% RPI 触发交易机器人 (v3.0 增强版)")
        log.info("=" * 60)
        log.info(f"市场: {self.config.market}")
        log.info(f"交易大小: {self.config.trade_size} BTC")
        log.info(f"交易间隔: {self.config.trade_interval} 秒")
        log.info("")
        log.info("v3.0 增强功能:")
        log.info(f"  - 追踪止损: {'开启' if self.config.trailing_stop_enabled else '关闭'}")
        log.info(f"  - 智能持仓: {'开启' if self.config.smart_hold_enabled else '关闭'}")
        log.info(f"  - RSI过滤: {'开启' if self.config.rsi_filter_enabled else '关闭'}")
        log.info(f"  - 仓位平滑: {'开启' if self.config.position_smoothing_enabled else '关闭'}")
        log.info("=" * 60)

        if self.account_manager:
            log.info(f"多账号模式: {len(self.account_manager.accounts)} 个账号")
            log.info(f"当前: {self.account_manager.get_current_account_name()}")
        else:
            log.info("单账号模式")
        log.info("=" * 60)

        if not await self.client.authenticate_interactive():
            log.error("初始认证失败!")
            return
        log.info("认证成功，开始 RPI 交易...")

        while not _shutdown_requested:
            try:
                log.info("[周期] 开始 RPI 交易周期...")
                success, msg = await self.run_rpi_cycle()
                if "all_accounts_exhausted" in msg:
                    log.warning("所有账号今日额度已用完，等待明天...")
                    await self._cleanup_on_exit()
                    await self._wait_until_tomorrow()
                    continue
                if "switch" in msg and self.account_manager:
                    result = await self._switch_account()
                    if result == "all_hour_limited":
                        log.info("所有账号小时限制已满，等待 10 分钟...")
                        await asyncio.sleep(600)
                    elif result == "all_day_limited":
                        await self._wait_until_tomorrow()
                    continue
                if not success:
                    log.info(f"[周期] 失败: {msg}")
                if success:
                    await asyncio.sleep(self.config.trade_interval)
                else:
                    await asyncio.sleep(0.5)
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
        log.info(f"总交易数: {self.total_trades}")
        log.info(f"RPI 交易数: {self.rpi_trades}")
        if self.total_trades > 0:
            log.info(f"RPI 比例: {self.rpi_trades/self.total_trades*100:.1f}%")
        log.info("=" * 60)
        if self.account_manager:
            self.account_manager.save_state()

    async def _wait_until_tomorrow(self):
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (tomorrow - now).total_seconds()
        log.info(f"等待 {wait_seconds/3600:.1f} 小时后重新开始...")
        await asyncio.sleep(wait_seconds + 60)


# =============================================================================
# 主入口
# =============================================================================

def parse_accounts(accounts_str: str) -> List[AccountInfo]:
    accounts = []
    if not accounts_str:
        return accounts
    pairs = accounts_str.strip().split(";")
    for i, pair in enumerate(pairs):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        private_key = parts[0].strip()
        address = parts[1].strip()
        if not private_key.startswith("0x") or not address.startswith("0x"):
            continue
        accounts.append(AccountInfo(l2_private_key=private_key, l2_address=address, name=f"账号#{i+1}"))
    return accounts


async def main():
    load_dotenv()
    environment = os.getenv("PARADEX_ENVIRONMENT", "prod")
    market = os.getenv("MARKET", "BTC-USD-PERP")

    accounts_str = os.getenv("PARADEX_ACCOUNTS", "")
    accounts = parse_accounts(accounts_str)

    account_manager = None
    client = None

    if accounts:
        log.info(f"检测到多账号配置: {len(accounts)} 个账号")
        account_manager = AccountManager(accounts, environment)
        account_manager.load_state()
        if account_manager.is_account_hour_limited(account_manager.current_index):
            log.info("当前账号已达小时限制，尝试切换...")
            result = account_manager.switch_to_next_available_account()
            if result == "all_day_limited":
                log.error("所有账号今日额度已用完!")
                sys.exit(1)
        client = account_manager.get_current_client()
        if not client:
            log.error("无法初始化账号!")
            sys.exit(1)
    else:
        l2_private_key = os.getenv("PARADEX_L2_PRIVATE_KEY")
        l2_address = os.getenv("PARADEX_L2_ADDRESS")
        if not l2_private_key or not l2_address:
            log.error("请在 .env 文件中配置账号")
            sys.exit(1)
        client = ParadexInteractiveClient(l2_private_key=l2_private_key, l2_address=l2_address, environment=environment)

    config = RPIConfig(market=market)

    # 基础配置
    config.trade_size = os.getenv("TRADE_SIZE", "0.003").strip()
    config.trade_interval = float(os.getenv("TRADE_INTERVAL", "2.0"))
    config.max_spread_pct = float(os.getenv("MAX_SPREAD_PCT", "0.03"))
    config.max_wait_seconds = float(os.getenv("MAX_WAIT_SECONDS", "30.0"))
    config.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.015"))

    log.info(f"交易大小: {config.trade_size} BTC")
    log.info(f"优化参数: spread<{config.max_spread_pct}%, 止损>{config.stop_loss_pct}%, 等待<{config.max_wait_seconds}s")

    client.client_id_format = os.getenv("CLIENT_ID_FORMAT", "rpi")
    config.trend_filter_enabled = os.getenv("TREND_FILTER", "true").lower() == "true"
    config.volatility_filter_enabled = os.getenv("VOLATILITY_FILTER", "true").lower() == "true"
    config.max_volatility_pct = float(os.getenv("MAX_VOLATILITY_PCT", "0.1"))
    config.volatility_window = int(os.getenv("VOLATILITY_WINDOW", "10"))
    config.entry_mode = os.getenv("ENTRY_MODE", "trend")

    # 动态止损
    config.dynamic_stop_loss = os.getenv("DYNAMIC_STOP_LOSS", "true").lower() == "true"
    config.stop_loss_spread_multiplier = float(os.getenv("STOP_LOSS_MULTIPLIER", "2.0"))
    config.dynamic_stop_loss_min = float(os.getenv("DYNAMIC_STOP_LOSS_MIN", "0.01"))
    config.dynamic_stop_loss_max = float(os.getenv("DYNAMIC_STOP_LOSS_MAX", "0.05"))
    log.info(f"动态止损: {'开启' if config.dynamic_stop_loss else '关闭'} (范围: {config.dynamic_stop_loss_min*100:.0f}%-{config.dynamic_stop_loss_max*100:.0f}%)")

    # v3.0 增强参数
    log.info("")
    log.info("=== v3.0 增强参数 ===")

    config.trailing_stop_enabled = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
    config.trailing_stop_activation = float(os.getenv("TRAILING_STOP_ACTIVATION", "1.0"))
    config.trailing_stop_callback = float(os.getenv("TRAILING_STOP_CALLBACK", "0.5"))
    config.breakeven_activation = float(os.getenv("BREAKEVEN_ACTIVATION", "1.0"))
    log.info(f"追踪止损: {'开启' if config.trailing_stop_enabled else '关闭'} (激活: {config.trailing_stop_activation}x止损, 回调: {config.trailing_stop_callback}%)")

    config.smart_hold_enabled = os.getenv("SMART_HOLD_ENABLED", "true").lower() == "true"
    config.hold_extension_seconds = float(os.getenv("HOLD_EXTENSION_SECONDS", "15.0"))
    config.early_exit_stagnation_threshold = float(os.getenv("EARLY_EXIT_STAGNATION", "0.02"))
    log.info(f"智能持仓: {'开启' if config.smart_hold_enabled else '关闭'} (延长: {config.hold_extension_seconds}s)")

    config.rsi_filter_enabled = os.getenv("RSI_FILTER_ENABLED", "true").lower() == "true"
    config.rsi_period = int(os.getenv("RSI_PERIOD", "14"))
    config.rsi_trend_threshold = float(os.getenv("RSI_TREND_THRESHOLD", "50.0"))
    log.info(f"RSI过滤: {'开启' if config.rsi_filter_enabled else '关闭'} (周期: {config.rsi_period}, 阈值: {config.rsi_trend_threshold})")

    config.position_smoothing_enabled = os.getenv("POSITION_SMOOTHING_ENABLED", "true").lower() == "true"
    config.max_position_increase_pct = float(os.getenv("MAX_POSITION_INCREASE_PCT", "0.50"))
    log.info(f"仓位平滑: {'开启' if config.position_smoothing_enabled else '关闭'} (最大增幅: {config.max_position_increase_pct*100:.0f}%)")

    # 研究优化参数
    config.exit_order_type = os.getenv("EXIT_ORDER_TYPE", "limit")
    config.direction_signal_threshold = float(os.getenv("DIRECTION_SIGNAL_THRESHOLD", "0.3"))
    config.risk_reward_ratio = float(os.getenv("RISK_REWARD_RATIO", "2.0"))
    config.time_filter_enabled = os.getenv("TIME_FILTER", "true").lower() == "true"
    config.kelly_enabled = os.getenv("KELLY_ENABLED", "true").lower() == "true"
    config.kelly_fraction = float(os.getenv("KELLY_FRACTION", "0.25"))
    config.max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.30"))
    config.drawdown_control_enabled = os.getenv("DRAWDOWN_CONTROL", "true").lower() == "true"
    config.max_daily_loss_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
    config.max_total_loss_pct = float(os.getenv("MAX_TOTAL_LOSS_PCT", "0.10"))

    log.info(f"Kelly准则: {'开启' if config.kelly_enabled else '关闭'} (最大仓位: {config.max_position_pct:.0%})")
    log.info("=" * 30)

    bot = RPIBot(client, config, account_manager)

    def signal_handler(sig, frame):
        global _shutdown_requested
        log.info("\n收到 Ctrl+C，准备退出...")
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
