# 第五轮追加审查（2026-09-24）

## 本轮修复

- 审批证据：walk-forward的n_periods必须为整数，max_drawdown必须位于[-0.25,0]，不接受小数样本或正回撤。
- 选股：财务fetched_at增加完整时间比较，不能仅凭同日日期放行未来时刻采集的记录。
- 持仓/交易：AI委托及成交核对股票与关联持仓任务code一致，避免借用其他股票的任务资格。买入必须关联任务；旧版无position_id的存量卖单继续按可卖股数约束成交并对账，不因新校验堵塞退出。

## 测试

execution_gate 11、selection_gate 7、buy_atomicity 39、validation_hardening 20，共77项通过。交易测试曾发现无任务编号的旧卖单被误拦，修复兼容条件后39项全部通过。模块编译及git diff --check通过。

续接复核：重新运行上述77项，并追加risk_schedule 3项、strategy_backtest_integrity 1项，共81项全部通过；git diff --check再次通过。

## 未完成验证

selection_gate._query和library.list_strategies仍使用带schema初始化/迁移的连接，成交前查询性能、锁等待和连接生命周期尚需专项整改，不能称为纯只读实现。未运行生产压力测试。审批原文核验、长期财务/板块数据新鲜度、完整质量阈值、风控日内超时与文件失败仍未关闭。

未提交、未推送、未重启服务，未操作生产交易。保留前几轮工作区修改。
