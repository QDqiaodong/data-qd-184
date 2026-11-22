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
