# 资产可用性、维护履约与事故归因 v1（asset-availability）

本文件是本仓自有的自然日计数约定，不涉及外部会计、时区或 SLA 标准。
`asset-availability` 与 `asset-schedule`、`asset-close` 共用严格 JSON 读取、
8 MB 输入上限、成功时“目标不存在才原子新建”的发布方式，以及非法输入或目标
已存在退出码 2 并保留旧目标的约定；旧命令的输入、输出与行为保持不变。

## 日期与区间

- 所有日期均为规范 `YYYY-MM-DD` 字符串；`2026-1-01`、`2026-13-01`
  等非规范形式非法。
- 报告区间为左闭右开 `[from_date, to_date)`，跨度至少 1 天、至多 3660 个
  自然日。资产有效期、计划维护、执行维护与事故区间同样左闭右开。
- 区间长度按自然日计算：`[2026-01-05, 2026-01-08)` 是 3 天。
- 只有资产有效区间与报告区间的交集内的日期参与统计，并生成状态段。
  有效区间外的日期不计入任何字段，也不产生段。

## 输入契约

根对象仅含 `from_date`、`to_date`、`assets`、`maintenance_orders`、
`incidents` 五个字段。

- `assets`：数组，每项仅含 `id`、`category`、`active_from`、
  `active_until`。`id` 唯一且非空；`category` 是非空字符串；
  `active_until` 为 `null` 表示没有已知结束日，否则必须晚于
  `active_from`。资产有效区间必须与报告区间相交。
- `maintenance_orders`：数组，每项仅含 `id`、`asset_id`、
  `planned_start`、`planned_end`、`executed_start`、`executed_end`。
  - `id` 在订单内唯一，`asset_id` 必须引用存在的资产。
  - 计划区间非空，且完整落在所引用资产有效区间与报告区间内。
  - `executed_start` 与 `executed_end` 必须同时为 `null` 或同时为日期。
  - 已执行区间非空，且完整落在资产有效区间与报告区间内；它可以在计划
    之前、之后或两侧外溢，只要不越出资产有效期与报告区间。
  - `executed_*` 为 `null` 表示截至报告结束仍无执行记录。
- `incidents`：数组，每项仅含 `id`、`asset_id`、`start`、`end`。
  `id` 在事故内唯一；区间非空且完整落在资产有效区间与报告区间内。
  事故可与计划或执行维护任意重叠。

容量按 500 项资产、10000 个维护订单、20000 个事故设计并测试；实现不设
更小的上限，仍以 8 MB 源文件为外层限制。所有对象都拒绝缺失字段、未知字段、
重复 JSON 键、布尔冒充日期/字符串、非有限数值、非法日期、重复 ID、
未知资产与上述区间错误。

## 逐日状态

对每项资产先求 `[active_from, min(active_until, to_date))` 与报告区间的交集。
交集内某一天：

1. 被任一事故区间覆盖 → `incident`；
2. 否则被任一计划区间或执行区间覆盖 → `maintenance`；
3. 否则 → `available`。

事故优先于维护；事故覆盖维护日仍是 `incident`，真实事故不会因为存在计划而
从状态段中消失。重叠订单与重叠事故只按日期集合计一次。

每项资产的 `segments` 仅覆盖其有效交集，段为 `{start, end, status}`，
段间首尾相邻、互不重叠，按 `start` 升序；相邻且状态相同的日期必须合并为
一段（包括两个首尾相接的维护区间）。资产对象同时回显 `id`、`category`、
`active_from`、`active_until`。

## 维护履约

每个订单独立输出 `planned_days`、`executed_days`、`late_start_days`、
`outside_plan_days` 与 `status`，并回显 `id`、`asset_id`、
`planned_start`、`planned_end`、`executed_start`、`executed_end`：

- `planned_days`：计划区间自然日数。
- `executed_days`：执行区间自然日数；无执行记录为 0。
- `late_start_days`：`max(0, executed_start - planned_start)` 的天数；
  提前执行为 0；无执行记录为 0。
- `outside_plan_days`：执行区间减去计划区间后的自然日数；无执行记录为 0。
- `status` 只能是：
  - `completed_within_window`：有执行记录，且
    `planned_start <= executed_start` 且 `executed_end <= planned_end`；
  - `completed_outside_window`：有执行记录但不满足上述包含关系；
  - `missed`：无执行记录，且计划区间不延伸到报告右边界；
  - `pending`：无执行记录，且计划区间延伸到报告右边界
    （由于计划必须在报告区间内，这只出现在 `planned_end == to_date`）。

区间重叠的订单不会重复增加资产状态天数，但每个订单的履约指标仍按自身区间
独立计算。订单按 `id` 字典序稳定输出。

## 月度汇总与事故归因

`periods` 按报告区间涉及的自然月升序排列，包含没有资产或事件的空月份；
每个月的日期只统计该月与报告区间相交的部分。月对象为
`{month: "YYYY-MM", categories: {类别: {...}}}`，类别按字典序稳定输出。

类别项含七个字段，均为该月该类别下逐资产逐日集合后的天数：

- `active_days`：资产有效交集内的天数。
- `available_days` / `maintenance_days` / `incident_days`：三种状态各自
  的天数，同一资产同一天只属于一种，三者之和等于 `active_days`，并与
  状态段完全一致。
- `unplanned_incident_days`：事故覆盖、但没有任何计划维护区间覆盖的
  天数。事故与计划重叠的日期算作计划已解释。
- `planned_maintenance_days`：计划维护区间并集覆盖的天数。
- `executed_maintenance_days`：执行维护区间并集覆盖的天数。

重叠订单、重叠事故先按区间并集去重再计数。全字段为零的类别可省略；
空月份仍保留为 `categories: {}`。

## 汇总与稳定顺序

`summary` 给出 `assets`、`orders`、`incidents` 与五个总天数
`active_days`、`available_days`、`maintenance_days`、`incident_days`、
`unplanned_incident_days`，并与所有资产状态段及全部月份类别汇总对平。

资产按 `id`、订单按 `id`、事故对输出的影响按日期集合、类别按字典序稳定
输出；输入数组顺序不影响任何结果。

## 输出与失败约定

报告顶层仅含 `from_date`、`to_date`、`assets`、`maintenance_orders`、
`periods`、`summary`，不包含关账流水或其他财务结果。非法输入或输出目标
已存在时退出码 2 并保留旧目标；全部校验与计算完成后，通过同目录临时文件
原子硬链接新建一份完整 JSON 报告，成功退出 0，并在标准输出打印 `summary`。

样例见 `fixtures/availability_input.json` 与 `fixtures/availability_expected.json`。
