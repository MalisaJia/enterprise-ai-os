"""断路器：把"厂商故障"从"业务错误"里隔离出来。

为什么网关必须有断路器：模型厂商的故障通常是**分钟级**的（限流策略调整、
区域网络抖动、上游扩容）。如果没有断路器，每个请求都要等到 60s 超时才失败，
网关的连接池和 worker 会被迅速耗尽——一家厂商挂掉会拖死整个网关。
断路器让第 6 个请求开始**立即失败**（<1ms），把降级链的切换成本降到可忽略。

熔断粒度的选择（重要设计决策）：
按 **model_id** 而非 provider 熔断。原因是同一厂商下不同模型的健康度可以独立——
``deepseek-reasoner`` 排队严重超时的时候 ``deepseek-chat`` 往往正常。
按 provider 熔断会把健康模型一起打掉，可用性反而更差。
代价是状态数量变多（12 个模型 × N 副本），但 Redis 承担这点开销毫无压力。

状态机（标准三态）::

    ┌────────┐  失败达阈值   ┌──────┐  冷却期到   ┌───────────┐
    │ CLOSED │ ───────────> │ OPEN │ ─────────> │ HALF_OPEN │
    └────────┘              └──────┘            └───────────┘
         ^                     ^                      │
         │  连续成功达阈值      │      任一失败         │
         └─────────────────────┴──────────────────────┘

哪些错误算"失败"（对应 config 的 ``count_as_failure``）：
只有 timeout / connection_error / server_error 计入。
``rate_limited`` **不计入**——限流是厂商在正常工作、只是我们超配额了，
熔断它会导致"稍微超速就整个模型不可用"，明显过度反应；正确处理是排队或降级。
``invalid_request`` / ``content_filter`` 更是业务侧问题，与厂商健康无关。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    """断路器状态。"""

    CLOSED = "closed"        # 正常放行
    OPEN = "open"            # 熔断中，直接拒绝
    HALF_OPEN = "half_open"   # 试探恢复，放行少量请求


class FailureKind(str, Enum):
    """失败分类。与 ``providers`` 层的错误类型一一对应。

    这里刻意重新定义一套枚举而不直接用异常类：断路器的状态需要序列化到 Redis，
    跨副本共享时传的是字符串；同时也让 config 里的 ``count_as_failure``
    可以直接用这些字面量配置。
    """

    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"
    INVALID_REQUEST = "invalid_request"
    CONTENT_FILTER = "content_filter"
    AUTH_ERROR = "auth_error"


@dataclass
class CircuitBreakerConfig:
    """断路器参数。字段与 ``config.yaml`` 的 ``circuit_breaker`` 段对齐。

    Attributes:
        failure_threshold: 滚动窗口内绝对失败次数阈值。
        failure_rate_threshold: 滚动窗口内失败率阈值（0~1）。
            两个阈值是 **或** 关系：低流量时靠绝对次数，高流量时靠失败率。
            只用失败率会导致冷启动误判（前 2 个请求失败即 100%）；
            只用绝对次数会导致高流量下迟迟不熔断（1000 QPS 时 5 次失败无意义）。
        min_requests_in_window: 失败率判定的最小样本量，配合上面解决冷启动。
        rolling_window_seconds: 滚动统计窗口。
        open_duration_seconds: OPEN 状态冷却时长。
        half_open_max_requests: HALF_OPEN 允许并发试探的请求数。
        half_open_success_threshold: HALF_OPEN 连续成功多少次才回到 CLOSED。
        count_as_failure: 计入失败的错误类型。
        ignore: 明确忽略的错误类型（既不计失败也不计成功）。
    """

    failure_threshold: int = 5
    failure_rate_threshold: float = 0.5
    min_requests_in_window: int = 20
    rolling_window_seconds: int = 60
    open_duration_seconds: int = 30
    half_open_max_requests: int = 3
    half_open_success_threshold: int = 2
    count_as_failure: tuple[str, ...] = (
        FailureKind.TIMEOUT.value,
        FailureKind.CONNECTION_ERROR.value,
        FailureKind.SERVER_ERROR.value,
    )
    ignore: tuple[str, ...] = (
        FailureKind.RATE_LIMITED.value,
        FailureKind.INVALID_REQUEST.value,
        FailureKind.CONTENT_FILTER.value,
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CircuitBreakerConfig":
        """从配置字典构造。

        TODO:
          1. 逐字段读取并做范围校验：
             - failure_rate_threshold 必须在 (0, 1]
             - open_duration_seconds 建议 >= 10，太短会形成"熔断-恢复"抖动
             - half_open_success_threshold <= half_open_max_requests，
               否则永远回不到 CLOSED（死锁式配置，必须启动时就报错而非运行时才发现）
          2. count_as_failure / ignore 若有交集 → 配置错误，抛异常
        """

        raise NotImplementedError

    def classify(self, kind: str) -> str:
        """判定一个错误类型的处理方式，返回 ``"failure"`` / ``"success"`` / ``"ignore"``。

        TODO: 按 ignore → count_as_failure → 默认 ignore 的顺序判定。
              默认落到 ignore 而非 failure 是刻意的：未知错误类型不应该
              触发熔断，宁可漏熔断也不要因为新增了一种业务错误码就误杀模型。
        """

        raise NotImplementedError


@dataclass
class CircuitSnapshot:
    """断路器某一时刻的状态快照。用于 ``/admin/circuit`` 接口与告警。"""

    key: str
    state: CircuitState
    failures: int = 0
    successes: int = 0
    total: int = 0
    opened_at: float | None = None
    last_failure_kind: str | None = None
    half_open_in_flight: int = 0

    @property
    def failure_rate(self) -> float:
        """窗口内失败率。"""

        return self.failures / self.total if self.total else 0.0

    @property
    def retry_after(self) -> float:
        """OPEN 状态下距离可以试探还剩多少秒。非 OPEN 返回 0。"""

        return 0.0


class CircuitOpenError(Exception):
    """断路器打开时抛出。

    调用方（``ModelRouter``）捕获它后应当走降级链，而**不是**返回 5xx。
    只有降级链全部耗尽才对外报错。

    携带 ``snapshot`` 是为了让上层能把 ``retry_after`` 写进响应头，
    也便于日志里直接看出是哪个模型、什么原因熔断的。
    """

    def __init__(self, snapshot: CircuitSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__(
            f"circuit open for {snapshot.key} "
            f"(last_failure={snapshot.last_failure_kind}, "
            f"retry_after={snapshot.retry_after:.1f}s)"
        )


class CircuitBreaker:
    """单个熔断单元（一个 model_id 对应一个实例）。

    状态存储在 Redis 而非进程内存，原因与限流一致：多副本必须共享判定结果。
    否则 3 副本各自独立计数，实际要 3 × threshold 次失败才全员熔断。

    Redis 数据结构设计：
    - ``cb:{key}:state``   —— String，值为状态名，OPEN 时带 TTL = open_duration，
      **TTL 自然过期即状态转 HALF_OPEN**，省掉一个定时任务
    - ``cb:{key}:stats``   —— Hash，滚动窗口内的 failures/total，TTL = window
    - ``cb:{key}:probe``   —— String 计数器，HALF_OPEN 期间的 in-flight 试探数
    """

    def __init__(
        self,
        key: str,
        config: CircuitBreakerConfig,
        redis: Any | None = None,
    ) -> None:
        self.key = key
        self.config = config
        self._redis = redis
        # 进程内的本地缓存：避免每个请求都打一次 Redis。
        # 权衡：牺牲最多 _local_ttl 秒的状态新鲜度，换掉 90% 的 Redis 往返。
        # 对熔断这种"分钟级"决策，1 秒的滞后完全可以接受。
        self._local_state: CircuitState = CircuitState.CLOSED
        self._local_expires_at: float = 0.0
        self._local_ttl: float = 1.0

    # ---------------------------------------------------------------- 判定

    async def allow(self) -> None:
        """请求前置检查。放行则正常返回，否则抛 :class:`CircuitOpenError`。

        设计成"抛异常"而不是"返回 bool"：熔断是异常路径，用返回值容易被
        调用方忽略（忘了判断 if 就等于没接断路器），异常则强制处理。

        TODO:
          1. 本地缓存未过期且为 CLOSED → 直接返回（快路径，绝大多数请求走这里）
          2. 读 Redis state：
             - 不存在 → CLOSED，放行
             - OPEN → 抛 CircuitOpenError（附 retry_after = key 的剩余 TTL）
             - HALF_OPEN → INCR probe 计数，超过 half_open_max_requests
               则抛异常；否则放行。注意 INCR 要带 TTL 兜底，
               避免请求异常退出没 DECR 导致试探额度永久占满
          3. 刷新本地缓存
        """

        raise NotImplementedError

    async def record_success(self, latency_ms: float | None = None) -> None:
        """记录一次成功。

        TODO:
          1. HINCRBY stats total 1，并给 stats 打上窗口 TTL
          2. 若当前是 HALF_OPEN：
             - INCR 连续成功数；达到 half_open_success_threshold → 转 CLOSED
               并清空 stats（不清的话旧失败记录会让它立刻再次熔断）
             - DECR probe
          3. latency_ms 透传给 registry.record_latency，供延迟优先路由使用
        """

        raise NotImplementedError

    async def record_failure(self, kind: str) -> None:
        """记录一次失败。

        TODO:
          1. config.classify(kind)：
             - "ignore"  → 直接返回，连 total 都不加
               （限流请求不该污染失败率的分母，否则被限流越多越容易误熔断）
             - "success" → 转调 record_success
          2. HINCRBY stats failures/total，打窗口 TTL
          3. HALF_OPEN 下**任一失败**立即回 OPEN 并重置冷却计时——
             试探失败说明故障未恢复，不给第二次机会，避免恢复期反复冲击上游
          4. CLOSED 下判定是否该熔断：
             failures >= failure_threshold
             或 (total >= min_requests_in_window 且 failure_rate >= failure_rate_threshold)
          5. 转 OPEN 时：SET state=open EX open_duration_seconds，
             记录 last_failure_kind，并打 WARNING 日志 + 上报告警
             （熔断是必须让人知道的事件，不能只留在 metrics 里）
        """

        raise NotImplementedError

    # ---------------------------------------------------------------- 运维

    async def snapshot(self) -> CircuitSnapshot:
        """读取当前状态快照。供 ``/admin/circuit`` 与 ``/metrics`` 使用。

        TODO: 一次 pipeline 读齐 state/stats/probe/TTL，避免 N 次往返。
        """

        raise NotImplementedError

    async def reset(self) -> None:
        """人工强制复位到 CLOSED。

        存在的意义：故障已确认修复但冷却期还没到时，运维需要一个立即恢复的手段，
        否则只能干等。属于 ``/admin`` 权限接口。

        TODO: DEL state/stats/probe 三个 key，清本地缓存，打 INFO 审计日志
              （谁在什么时候复位了哪个模型，事后必须可追溯）。
        """

        raise NotImplementedError

    async def force_open(self, duration_seconds: int | None = None) -> None:
        """人工强制熔断。

        用于计划内维护：已知某厂商要停机 2 小时，提前手动摘掉，
        免得靠失败累积自动熔断（那期间的请求都是白白浪费的超时）。

        TODO: SET state=open EX (duration or open_duration_seconds)。
        """

        raise NotImplementedError


class CircuitBreakerRegistry:
    """断路器集合。按 model_id 懒创建并复用实例。

    为什么要有这层：``ModelRouter`` 需要在候选模型间挑一个"没熔断的"，
    这要求它能按 model_id 快速拿到断路器；同时熔断状态还要反写到
    ``ModelRegistry.update_health()``，让路由策略在**打分阶段**就排除掉
    熔断模型——而不是选完了才发现不可用再降级。后者会白白多一次降级跳转。
    """

    def __init__(self, config: CircuitBreakerConfig, redis: Any | None = None) -> None:
        self.config = config
        self._redis = redis
        self._breakers: dict[str, CircuitBreaker] = {}

    @classmethod
    def from_config(cls, config: dict[str, Any], redis: Any | None = None) -> "CircuitBreakerRegistry":
        """从全局配置构造。

        TODO:
          1. 取 config["circuit_breaker"]，enabled=false 时返回 NullCircuitBreakerRegistry
          2. CircuitBreakerConfig.from_dict 解析参数
        """

        raise NotImplementedError

    def get(self, model_id: str) -> CircuitBreaker:
        """取（或懒创建）指定模型的断路器。

        TODO: dict.setdefault 式懒创建。注意这里不加锁——
              asyncio 单线程下 dict 操作原子，多创建一个实例也无害
              （状态在 Redis，实例本身无状态）。
        """

        raise NotImplementedError

    async def unhealthy_models(self) -> set[str]:
        """返回当前处于 OPEN 状态的 model_id 集合。

        ``ModelRouter`` 在候选筛选阶段调用它做批量排除，比逐个 ``allow()``
        高效得多（一次 MGET vs N 次 GET）。

        TODO: pipeline MGET 所有已知 key 的 state，筛出 open。
        """

        raise NotImplementedError

    async def snapshot_all(self) -> list[CircuitSnapshot]:
        """全量快照。``GET /admin/circuit`` 的数据源。

        TODO: 并发收集各断路器 snapshot。
        """

        raise NotImplementedError

    async def sync_to_registry(self, model_registry: Any) -> None:
        """把熔断状态同步到 :class:`~src.registry.models.ModelRegistry`。

        由后台任务周期性调用（建议 5s 一次）。

        为什么要周期同步而不是熔断时立即推送：网关多副本，A 副本熔断了
        B 副本的内存 registry 并不知道。周期性从 Redis 拉是最简单的收敛方式，
        5s 的滞后对路由决策无实质影响（熔断本身持续 30s）。

        TODO: unhealthy_models() → 逐个 model_registry.update_health(id, ModelHealth.UNHEALTHY)，
              不在集合里的恢复为 HEALTHY。
        """

        raise NotImplementedError


class NullCircuitBreakerRegistry(CircuitBreakerRegistry):
    """恒放行实现。用于本地开发与 ``circuit_breaker.enabled=false``。

    与 ``NullRateLimiter`` 同样的理由：让调用方不必到处判空。
    """

    def __init__(self) -> None:
        super().__init__(CircuitBreakerConfig(), redis=None)

    def get(self, model_id: str) -> CircuitBreaker:
        """返回一个永不熔断的断路器。"""

        return _NullCircuitBreaker(model_id, self.config)

    async def unhealthy_models(self) -> set[str]:
        """恒空。"""

        return set()

    async def snapshot_all(self) -> list[CircuitSnapshot]:
        """恒空。"""

        return []

    async def sync_to_registry(self, model_registry: Any) -> None:
        """无操作。"""


class _NullCircuitBreaker(CircuitBreaker):
    """恒 CLOSED 的断路器。"""

    async def allow(self) -> None:
        """恒放行。"""

    async def record_success(self, latency_ms: float | None = None) -> None:
        """无操作。"""

    async def record_failure(self, kind: str) -> None:
        """无操作。"""

    async def snapshot(self) -> CircuitSnapshot:
        """恒返回 CLOSED 快照。"""

        return CircuitSnapshot(key=self.key, state=CircuitState.CLOSED)

    async def reset(self) -> None:
        """无操作。"""

    async def force_open(self, duration_seconds: int | None = None) -> None:
        """无操作。"""
