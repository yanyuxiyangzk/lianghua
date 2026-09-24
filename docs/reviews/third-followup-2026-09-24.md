# 第三轮追加审查（2026-09-24）

## 新修复

- strategy_backtest：部分因子求值异常时拒绝报告，不再回测剩余因子冒充原策略；选中股票未来收益缺失时返回失败，不再跳过该股后计算均值。
- combo_backtest：选中股票缺失未来收益时显式拒绝不完整回放。
- account_risk_level：None、非法字符串、NaN、Infinity不能分类为normal，返回red。
- 开盘前/盘中风控任务：风险计算抛异常时写暂停买入状态；开盘前ok=False也不再保留旧放行状态。

## 验证

validation_hardening 19、risk_schedule 3、selection_gate 6、execution_gate 9、buy_atomicity 37、experience 6、strategy_backtest_integrity 1，共81项通过。修改模块编译与diff检查通过。非全仓测试。

## 未关闭问题

自动选股检查主要在创建pending时执行，成交前尚未重新验证所有财务/板块/技术证据；审批虽在成交前复核，但不等于逐股票证据已重新验证。风险文件写入本身失败、风控状态日内超时、回放成本/基准统一、数据版本时点、审批报告实际内容核验仍需继续。策略回放仍不构成执行资格。

本次未操作交易、重启服务、提交或推送；前两轮未提交修改保留。
