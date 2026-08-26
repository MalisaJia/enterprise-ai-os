"""分布式限流（Redis 令牌桶）。

为什么必须是分布式的：网关会多副本部署，本地计数器意味着
实际放行量 = 单副本阈值 × 副本数，限流形同虚设。

为什么是令牌桶而非固定窗口：LLM 流量天然突发（一次批量摄入触发上千次调用）。
令牌桶允许在长期速率受控的前提下容忍短时突发，固定窗口会在窗口边界产生
两倍瞬时流量（经典的边界问题）。

三级限流的分工：
- **tenant**：防"邻居噪音"，一个租户不能吃掉全部厂商配额
- **user**：防单人误用（写死循环调 API）
- **model**：防打爆厂商侧配额，是最后一道保险
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class RateLimitTier(str, Enum):
    """限流层级。三级独立判定，任一超限即拒绝。"""

    TENANT = "tenant"
    USER = "user"
    MODEL = "model"
    SERVICE = "service"     # 内部服务（如 rag-pipeline）单独配额


class ExceedAction(str, Enum):
    """超限行为。"""

    REJECT = "reject"       # 直接 429
    QUEUE = "queue"         # 短暂排队等令牌
    DEGRADE = "degrade"     # 降级到廉价模型继续服务


@dataclass
class RateLimitRule:
    """一条限流规则。

    Attributes:
        rpm: 每分钟请求数上限。
        tpm: 每分钟 token 数上限。LLM 场景 tpm 比 rpm 更重要——
            一个 100K token 的请求造成的负载是普通请求的百倍。
        concurrent: 并发请求数上限。流式长连接场景靠这个兜底。
        burst_multiplier: 突发容量 = rpm × 该倍数。
    """

    tier: RateLimitTier
    rpm: int | None = None
    tpm: int | None = None
    concurrent: int | None = None
    burst_multiplier: float = 1.5

    @property
    def enabled(self) -> bool:
        """是否有任何有效限制。"""

        return any(v is not None for v in (self.rpm, self.tpm, self.concurrent))


@dataclass
class RateLimitResult:
    """限流判定结果。

    ``retry_after`` 与 ``remaining`` 会写入标准 ``RateLimit-*`` 响应头，
    让客户端能自适应退避，而不是盲目重试加剧拥塞。
    """

    allowed: bool
    tier: RateLimitTier | None = None
    limit: int = 0
    remaining: int = 0
    retry_after_seconds: float = 0.0
    reason: str = ""

    def to_headers(self) -> dict[str, str]:
        """转为 RateLimit 响应头（draft-ietf-httpapi-ratelimit-headers）。"""

        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(int(self.retry_after_seconds) + 1)
        return headers


class RateLimiter:
    """Redis 令牌桶限流器。

    实现要点：判定必须是**原子**的。"读余量 → 判断 → 扣减" 三步在并发下
    会超发，必须用 Lua 脚本在 Redis 内一次完成。
    """

    #: 令牌桶 Lua 脚本骨架。
    #: KEYS[1]=桶键  ARGV=[容量, 填充速率, 当前时间戳, 请求令牌数]
    #: 返回 [是否允许, 剩余令牌, 需等待秒数]
    TOKEN_BUCKET_LUA = """
    -- TODO: 实现令牌桶
    -- 1. HMGET 取上次剩余令牌与上次填充时间
    -- 2. 按经过时间补充令牌，上限为容量
    -- 3. 令牌足够则扣减返回 1，否则返回 0 与需等待时间
    -- 4. HMSET 回写并设置 EXPIRE（防止冷键堆积）
    return {1, 0, 0}
    """

    def __init__(
        self,
        redis_url: str,
        *,
        rules: dict[RateLimitTier, RateLimitRule] | None = None,
        on_exceed: ExceedAction = ExceedAction.REJECT,
        max_queue_wait_seconds: float = 5.0,
        key_prefix: str = "gw:rl:",
    ) -> None:
        self.redis_url = redis_url
        self.rules = rules or {}
        self.on_exceed = on_exceed
        self.max_queue_wait_seconds = max_queue_wait_seconds
        self.key_prefix = key_prefix
        self._redis: Any | None = None
        self._script_sha: str | None = None
        #: 特定主体的配额覆盖（如 service:rag-pipeline）
        self.overrides: dict[str, RateLimitRule] = {}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> RateLimiter:
        """从 ``rate_limit`` 配置段构造。

        TODO: 解析 tiers 与 overrides；enabled=false 时返回一个恒放行的空实现
              （用 NullRateLimiter 而非到处判 if enabled）。
        """

        raise NotImplementedError

    async def startup(self) -> None:
        """连接 Redis 并预加载 Lua 脚本。

        TODO: redis.asyncio.from_url + SCRIPT LOAD 缓存 sha，
              后续用 EVALSHA 减少网络开销。
        """

        raise NotImplementedError

    async def close(self) -> None:
        """关闭 Redis 连接。"""

        # TODO: if self._redis: await self._redis.aclose()

    async def check(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        model_id: str | None = None,
        service: str | None = None,
        estimated_tokens: int = 0,
    ) -> RateLimitResult:
        """三级限流判定。

        Args:
            estimated_tokens: 预估 token 数，用于 tpm 判定。
                请求阶段只能估算（输出长度未知），实际用量在响应后用
                ``settle`` 校正。宁可先按上限预扣，也不要放行后超支。

        Returns:
            首个被拒的层级信息；全部通过时 ``allowed=True``。

        TODO:
          1. 按 tenant → user → model 顺序逐级 EVALSHA 判定
          2. 任一级拒绝立即返回（不继续扣减其他级的令牌，避免误扣）
          3. 命中 overrides 时用覆盖规则
        """

        raise NotImplementedError

    async def settle(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        model_id: str | None,
        actual_tokens: int,
        estimated_tokens: int,
    ) -> None:
        """用实际用量校正预扣的令牌。

        流式响应结束后调用。预估偏差在 LLM 场景可能很大（输出长度不可知），
        不校正会导致 tpm 限流长期不准。

        TODO: 差额为正则补扣，为负则返还。
        """

        raise NotImplementedError

    async def acquire_concurrency(self, subject: str, limit: int) -> bool:
        """获取并发额度（进入时 +1）。

        TODO: Redis INCR + EXPIRE 兜底；超限返回 False。
              必须有 TTL——进程崩溃时计数器不会被释放，没有 TTL 会永久占用额度。
        """

        raise NotImplementedError

    async def release_concurrency(self, subject: str) -> None:
        """释放并发额度（离开时 -1）。必须放在 finally 中调用。"""

        raise NotImplementedError

    def _key(self, tier: RateLimitTier, subject: str, dimension: str) -> str:
        """构造 Redis 键。

        含时间分片以便自然过期，避免手工清理。
        """

        window = int(time.time() // 60)
        return f"{self.key_prefix}{tier.value}:{dimension}:{subject}:{window}"


class NullRateLimiter(RateLimiter):
    """恒放行实现。用于本地开发与 ``rate_limit.enabled=false``。

    存在的意义：让调用方无需到处写 ``if limiter is not None``。
    """

    def __init__(self) -> None:
        super().__init__(redis_url="")

    async def startup(self) -> None:
        """无需连接。"""

    async def close(self) -> None:
        """无需清理。"""

    async def check(self, **kwargs: Any) -> RateLimitResult:
        """恒放行。"""

        return RateLimitResult(allowed=True, limit=0, remaining=0)

    async def settle(self, **kwargs: Any) -> None:
        """无操作。"""

    async def acquire_concurrency(self, subject: str, limit: int) -> bool:
        """恒允许。"""

        return True

    async def release_concurrency(self, subject: str) -> None:
        """无操作。"""
