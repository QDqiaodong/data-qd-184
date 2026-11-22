# 资产类别 × 保管单位期末余额披露 v1（asset-balance-disclosure）

本文件是本仓自有的披露选择约定，不涉及任何外部会计准则或隐私框架。
`asset-balance-disclosure` 在 `asset-lineage` 的截止月设备月表之上，为园区
协作方挑选一张代价最低且符合披露口径的「资产类别 × 保管单位」期末账面
余额交叉表。它与既有命令共用严格 JSON 输入、8 MB 上限及“目标已存在即
退出 2、原子新建”的发布约定；既有五个命令的输入、输出与行为保持不变。

## 输入契约

根对象仅含 `lineage`、`categories`、`custodians`、`custodian_by_device`、
`cells` 五个字段。

- `lineage`：一份完整的 `asset-lineage` 输入（`from_month`、`as_of_month`、
  `assets`、`events`），沿用该命令的全部校验、回放与账务口径。本命令只取
  其截止月设备月表的 `closing_book_cents`：按每行自己的 `category` 与设备
  对应的保管单位汇总。改造后的设备以新设备 ID 和类别归集，不追到原始投入；
  截止月处置转出的价值不算期末余额（被出售设备当月期末为零）。
- `categories`、`custodians`：各 1–4 个不重复、非全空白、最长 100 字符的
  字符串，是要公开的行列标签；即使某行或列本期全零也保留。类别表须覆盖
  截止月所有设备行的类别。标签精确匹配，不裁剪或折叠大小写。
- `custodian_by_device`：设备 ID 到保管单位的对象，键恰好覆盖截止月月表
  列出的设备（包括余额为零的行），不得混入仅在以往月份列出的设备；每个
  值须在保管单位表内。此映射只表示本期汇报所用归口，不修改设备台账。
- `cells`：行列笛卡尔积中每格恰好一条政策，每条含 `category`、`custodian`、
  `mode`，除此之外只接受该模式的字段：
  - `protected`：不显示金额，另有整数 `min_span_cents`（1 至 10^12），
    规定该格公开后仍须保有的最小可能值跨度。
  - `optional`：可显示也可遮蔽，另有整数 `suppression_cost`（1 至 10^6），
    遮蔽时计入损失。
  - `public`：必须显示真值。
  - `structural_zero`：业务上已公开这一组合不承接任何余额，必须显示 0；
    若真值不为 0 则输入非法。普通交叉格本期真值为 0 并不赋予这个约束。
- 至少有一个 `protected` 格。列表顺序不代表优先级。所有余额、小计和计算
  均用精确整数分。禁止隐式浮点、布尔值、重复 JSON 键、未知字段与非有限
  常量。

## 哪一张表可以交付

一张候选表显示全部行小计、列小计、必须公开格和未遮蔽的可选格。用这些
显示的事实界定所有相容的非负整数分表：每个相容表的行列和必须相同，所有
能看到的格必须等于显示值。隐藏格没有其他上下界，不因为账上的真值是 0、
没有设备，或者格子标为敏感就排除某个可能值。这是单份报表的反算核查，不
将台账、政策、外部信息、其他月份报表或选表算法本身视为收件人的额外约束，
也不承诺对这些信息的防护。

每个敏感格都必须在这批相容表上的最大值减最小值达到自己的
`min_span_cents`。边界必须是能由整张表达到的值，不是单看一边小计得到的
界；不同敏感格的上下界不要求同时在同一张表上达到。所有敏感格要通过同一
套遮蔽选择。合格选择中先最小化所遮蔽可选格的损失之和，再最小化所遮蔽
可选格的数量；仍相同则把各自遮蔽的可选格键 `(category, custodian)` 升序
排列，取列表字典序最小者。不能以先遇到的可行表充当最优答案。

## 报告

成功报告只含 `status`、`as_of_month`、`row_totals`、`column_totals`、
`cells`。

- `status` 为 `"ready"`；`as_of_month` 即台账截止月（规范 `YYYY-MM`）。
- `row_totals`、`column_totals` 分别按类别、保管单位字典序排列，元素分别
  是 `{category, closing_book_cents}`、`{custodian, closing_book_cents}`。
- `cells` 按 `(category, custodian)` 二元键排序，含 `category`、
  `custodian`、`status`；可见格的状态为 `"shown"` 且给出
  `closing_book_cents`，遮蔽格统一为 `"hidden"` 且不带金额。

报告不外带设备行、敏感标记、费用、界值或其他内部数字，标准输出也只打印
`status` 与 `as_of_month`。如果没有合格选择，正常退出并只写
`{status: "blocked", as_of_month}`，不先发布小计再拒绝。

## 输出与失败约定

非法输入（包括台账校验与回放错误、归口映射缺漏、结构零与真值不符）或
输出目标已存在时退出码 2，不发布部分报告且保留旧目标；全部校验与计算
完成后，通过同目录临时文件原子硬链接新建一份完整 JSON 报告（`ready` 与
`blocked` 两种合法结果均如此），成功退出 0。

样例见 `fixtures/disclosure_input.json` 与
`fixtures/disclosure_expected.json`。
