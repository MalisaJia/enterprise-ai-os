"""memory-service 测试。

测试分层
--------
1. **纯函数层**（无外部依赖，必须全覆盖）
   - ``ScopePolicy.from_dict`` 的非法组合拒绝（auto + tenant_members）
   - ``MemoryMerger.literal_similarity`` 的中文否定词处理：
     "我用 pnpm" 与 "我不用 pnpm" 必须**不**被判为重复。
     这是中文场景最容易踩的坑，字符级相似度对否定词几乎不敏感。
   - ``/context`` 的预算分配与余量转移

2. **判定层**（打桩 LLM，用 respx 拦 httpx）
   - 四种 op 的判定正确性，尤其是"补充 vs 取代"的区分
   - LLM 返回非法 JSON / target_index 越界 / 指向低相似度记忆时，
     必须降级为 ADD 而非抛异常或误删
   - 影子模式下库里确实没变

3. **存储层**（testcontainers 起真实 PG + pgvector）
   - 向量检索的多因子打分是否与预期一致
   - UPDATE 时 embedding 是否同步更新（漏掉会导致"改对了但搜不到"）
   - 衰减的幂等性：同一批数据跑两次 decay，结果不应是跑一次的平方
   - RLS 是否真的拦住跨租户查询——这条必须测，
     应用层漏一处就是隐私泄露

4. **端到端**（起 app + 真 PG + 假 LLM）
   - 抽取 → 判定 → 写入 → 检索 → /context 全链路
   - 团队层 propose → confirm 的幂等：重复 confirm 不应产生第二次 UPDATE

约定
----
- 不连真实 LLM。抽取与判定的输出用固定 fixture，
  否则测试会随模型版本变化而随机失败。
- 单测用 fakeredis，集成测试用 testcontainers。
- 涉及时间的测试（衰减、pending 过期）用可注入的时钟，
  不要 sleep——衰减的半衰期是 90 天，没法真等。
"""
