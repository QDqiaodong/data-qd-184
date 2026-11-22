# 待排检修与连续用机联合调度 v1（asset-dispatch）

本文件是本仓自有的联合调度约定，不涉及外部排产标准。`asset-dispatch` 与
`asset-schedule`、`asset-close`、`asset-availability` 共用严格 JSON 读取、
8 MB 输入上限、成功时“目标不存在才原子新建”的发布方式，以及非法输入或目标
已存在退出码 2 并保留旧目标的约定；三个旧命令的输入、输出与行为保持不变。

## 日期与区间

- 所有日期均为规范 `YYYY-MM-DD` 字符串；所有区间左闭右开，与
  `asset-availability` 的自然日约定一致。
- 规划窗口即 `availability` 的报告区间 `[from_date, to_date)`，本命令限制为
  1–28 个自然日、至多 12 台设备。

## 输入契约

根对象仅含 `availability`、`crew_by_date`、`flexible_orders`、`requests`
四个字段。

- `availability`：一份完整的 `asset-availability` 输入（`from_date`、
  `to_date`、`assets`、`maintenance_orders`、`incidents`），沿用该命令的
  全部校验。其中的维护订单与事故是固定事实，按现有逐日状态口径（事故优先、
  其次计划或执行维护、否则可用）确定各设备在窗口内的基础可用日；只有状态为
  `available` 的日子可以安排新增检修或用机。设备有效区间外的日期没有状态，
  不可安排。
- `crew_by_date`：以规范日期为键的对象，必须恰好包含窗口内的每一天，值是
  0–12 的整数，表示扣除固定工作后当天可分给待排检修的剩余班组数。
- `flexible_orders`：至多 10 条待排检修，每条仅含 `id`、`asset_id`、
  `window_start`、`window_end`、`duration_days`、`preferred_start`。
  - `id` 非空、非全空白，在待排检修内唯一，且不与固定维护订单 ID 冲突。
  - `asset_id` 必须引用存在的设备。
  - 检修窗口 `[window_start, window_end)` 非空且完整落在规划窗口内。
  - `duration_days` 是 1 到检修窗口长度（自然日数）之间的整数。
  - `preferred_start` 必须能让整个工期落在检修窗口内，即
    `window_start <= preferred_start` 且
    `preferred_start + duration_days <= window_end`；该日的基础状态或班组
    余量是否实际可行不影响校验。
- `requests`：至多 12 笔用机请求，每条仅含 `id`、`category`、`start`、
  `end`、`priority`。
  - `id` 非空、非全空白，在请求内唯一；请求 ID 与维护订单 ID 是各自的
    命名空间，可以相同。
  - `category` 非空、非全空白，与设备类别按字符串精确匹配；允许在设备池
    中没有匹配项（该请求只能落选）。
  - 用机区间 `[start, end)` 非空且完整落在规划窗口内。
  - `priority` 只能是 `"high"` 或 `"normal"`。

各数组允许为空，顺序没有业务含义。所有对象都拒绝缺失字段、未知字段、
重复 JSON 键、非规范日期、布尔冒充数值/字符串、非有限数值以及上述约束
违例。

## 联合调度

每项待排检修必须在其窗口内为指定设备排出一次完整、连续的工期；所占每一天
消耗一个班组名额，全池同一天的新增检修数不得超过该日 `crew_by_date` 余量。
同一设备同一天不能发生两项检修，也不能同时检修与用机。固定维护与事故占用
的日子不是 `available`，新增检修与用机都不能使用。

每笔请求可以全程由一台匹配类别的设备承接，也可以整笔落选（`asset_id` 为
`null`）。承接设备必须在请求的每一天基础可用，且没有被新增检修或其他已接
请求占用；两个区间只在端点相接时可以复用同一台设备。请求开始日、结束日与
检修工期都是硬约束。

在所有满足待排检修约束的计划中，按以下顺序择优：

1. 最大化 `high` 接单数；
2. 最大化 `normal` 接单数；
3. 最小化全体待排检修实际开工日与首选开工日之间绝对天数之和
   （`maintenance_shift_days`）；
4. 按检修 ID 字符串升序排列的开工日期向量，取字典序最小者；
5. 按请求 ID 字符串升序排列的分配向量，取字典序最小者；向量中的设备按
   ID 字符串升序比较，落选（`null`）排在任何设备之后。

目标与决胜顺序使最优计划唯一，输入数组顺序不影响结果。

## 报告

可行时输出 `status: "planned"`，并含以下字段；`maintenance`、
`assignments`、`alternatives` 及各变化数组均按相应 ID 字符串升序输出。

- `score`：`high_accepted`、`normal_accepted`、`maintenance_shift_days`。
- `maintenance`：每项待排检修的 `order_id`、`asset_id`、`start`、`end`
  （`end = start + duration_days`）。
- `assignments`：每笔请求的 `request_id` 与 `asset_id`，落选为 `null`。
- `alternatives`：主计划每笔落选请求各一项，互不影响、分别独立求解。
  对该项，在原输入约束下强制承接该笔请求，允许重排全部待排检修和其余
  请求，并沿用主计划的完整目标与决胜顺序。
  - 无解：`{"request_id", "status": "impossible"}`。
  - 有解：`{"request_id", "status": "feasible", "score",
    "maintenance_changes", "assignment_changes"}`。前一组列出每项改期检修
    的 `order_id`、`old_start`、`new_start`；后一组列出每笔分配发生变化
    的请求的 `request_id`、`old_asset_id`、`new_asset_id`，含设备更换、
    落选与新接单。两组只列与同一份主计划有差异的行。

若连放弃所有用机请求也无法安排完整的待排检修，成功写出只含
`{"status": "infeasible"}` 的报告，退出码仍为 0，供调度员调整班组或检修
窗口。若检修可行但接单为零，正常返回 `planned` 及逐项落选分析（此时各
加单分析必然均为 `impossible`：任何能被承接的请求都会进入主计划）。

## 输出与失败约定

非法输入或输出目标已存在时退出码 2 并保留旧目标；全部校验与计算完成后，
通过同目录临时文件原子硬链接新建一份完整 JSON 报告，成功退出 0，并在
标准输出打印 `status` 与（可行时的）`score` 摘要。

样例见 `fixtures/dispatch_input.json` 与 `fixtures/dispatch_expected.json`。
