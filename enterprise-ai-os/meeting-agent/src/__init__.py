"""meeting-agent —— Enterprise AI OS 的会议 Agent。

包结构::

    src/
      main.py           FastAPI 应用装配与配置加载
      transcription/    ASR 对接、说话人分离、术语纠错
      summarization/    分段摘要与归并、议题切分
      extraction/       决策抽取与 Action Item 抽取
      distribution/     分发到 octo-matter / octo-adapters / RAG / 记忆 / 审批
      api/              REST 与 WebSocket 路由

设计约束：
- 不在本进程做 ASR 推理，一律经 ``octo-speech``；不直连模型厂商，一律经
  ``model-gateway``。本服务是编排层，不持有 GPU 也不持有凭证。
- **会议进行中不做抽取与分发。** 前半段说"交给张三"后半段可能改成李四，
  边开边派任务会派出马上作废的任务。落地动作一律等会议结束。
- 分发默认需人工确认。分发有外部副作用，撤回的社交成本远高于多点一次确认。
- 责任人解析不上时留空待指派，绝不猜。派给错误的人比不派更糟。

它同时是整个 Enterprise AI OS 的垂直场景验证：把 rag-pipeline、
model-gateway、memory-service、workflow-engine 串起来跑通一个真实闭环。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
