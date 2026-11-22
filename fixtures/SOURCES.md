本目录为本项目原创合成样例，数据与代码采用MIT许可。不是观测/生产/个人真实数据，不需网络或外部服务。

`close_input.json` 为 `asset-close` 的原创合成关账样例：设备编号、金额与修订
时点均为手工构造，专门覆盖跨月更正、出售撤销与未来已知版本等边界，不对应任何
真实资产。

`availability_input.json` 为 `asset-availability` 的原创合成运维样例，覆盖
跨月报告区间、资产有效期边界、相邻维护区间合并、执行偏移、未执行订单、
事故覆盖计划维护以及多订单重叠；`availability_expected.json` 是同一输入的
完整预期报告，可由 `asset-availability` 直接复现。设备编号与事件均为手工
构造，不对应任何真实资产或事故。

`dispatch_input.json` 为 `asset-dispatch` 的原创合成调度样例，覆盖跨设备
共享班组冲突、固定维护与事故占用、多日工期、高优先级优先接单、落选请求
及其加单后的检修改期与请求改派；`dispatch_expected.json` 是同一输入的
完整预期报告，可由 `asset-dispatch` 直接复现。设备、班组与请求均为手工
构造，不对应任何真实资产或计划。

`lineage_input.json` 为 `asset-lineage` 的原创合成价值承继样例，覆盖同月
链式拆分与合装、同源份额合并、按权重整数分分配（含小数部分并列时的 ID
决胜）、出售前转出全部剩余价值以及残值跨改造承继；`lineage_expected.json`
是同一输入的完整预期报告，可由 `asset-lineage` 直接复现。设备编号、类别
与金额均为手工构造，不对应任何真实资产。

`disclosure_input.json` 为 `asset-balance-disclosure` 的原创合成披露样例：
台账覆盖 2 月合装（lathe/mill 并为 cell-a）、2 月出售 old-saw（转出价值
不计入期末，零余额设备仍列入月表并须归口）以及仅在 1 月出现、不进归口
映射的 junk；三行 A/B/C、三列 X/Y/Z 的真值为 A=(6,4,0)、B=(4,6,0)、
C=(4,0,6)，只有 A/X 敏感。`disclosure_expected.json` 是同一输入的完整
预期报告，可由 `asset-balance-disclosure` 直接复现。设备编号、类别、保管
单位与金额均为手工构造，不对应任何真实资产。

`sale_collections_input.json` 为 `asset-sale-collections` 的原创合成收款
催收样例：3 月三笔出售中一笔零所得（不建应收），两笔正所得分别约定 2 期
与 3 期分期；银行流水覆盖带未用款的大额到账、同一到账的连续部分退扣
（先退未用款再按相反次序撤回核销）、跨月退扣与窗口关闭后的明细清除，
7 月末首次对两笔应收各发一条 `open` 委托。`sale_collections_expected.json`
是同一输入逐期手工对账后的完整预期报告（含可直接续跑的交接包），可由
`asset-sale-collections` 直接复现。出售事件、设备编号与金额均为手工
构造，不对应任何真实资产或银行凭证。
