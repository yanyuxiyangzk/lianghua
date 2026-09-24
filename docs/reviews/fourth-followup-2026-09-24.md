# 第四轮追加审查（2026-09-24）

## 本轮修复

- static_backtest的upto原先只限制调仓日期，收益仍引用截止日后的价格。现在先截断行情面板再计算标签，截止日前未成熟的收益不会进入报告。
- 自动买入position_rejection同时验证当日pending任务和逐股票selection_gate，覆盖place_order与_fill现有两个调用点。证据缺失/变化拒绝买入，不影响卖出。此处仅复核已实现的市场、板块、财务存在性及行情检查，不代表已实现全部财务质量与技术规则。
- 跌停卖出转限价原先使用当前价乘0.995，可能低于跌停下限。现在挂跌停价，仍等待后续可成交条件。

## 测试

validation_hardening 20、execution_gate 10、buy_atomicity 38、selection_gate 6、risk_schedule 3、strategy_backtest_integrity 1，共78项通过。修改模块编译及diff检查通过。

## 边界

未提交、未重启、未执行生产交易。尚未全库重验。历史数据版本、审批报告原文校验、日内风控超时、文件写入故障、多进程状态一致性仍需专项验收。成交前复核增加数据库读取，尚未验证生产锁等待和性能；不应宣称无剩余bug或过拟合已解决。
