# data-qd-184 · 固定资产折旧与处置

从设备入账、使用期和处置事实生成逐月折旧价值轨迹、处置损益和类别汇总。

## 独立安装

Python >=3.11，实际验证使用本地CPython3.12.14。运行与unittest仅用标准库，不依赖外部服务。每仓建立独立虚拟环境；构建工具锁定版本，公共包缓存可复用。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

离线安装需要已安装的兼容Python及缓存中的setuptools==80.9.0、wheel==0.45.1。若缓存缺失，应先准备公共构建工具，不能把以上命令当空缓存可用承诺。修改源码后须重新安装本仓。

## 演示

在仓库根目录执行，目标路径须不存在；重复演示使用新文件名。

```sh
.venv/bin/asset-schedule --source fixtures/input.json --output run/report.json
```

预期：3项资产、1项已处置、期末账面4000分、累计折旧9000分、处置利得500分。

成功退出0；业务质量提示或诊断不等于输入失败，见summary。非法输入或已有目标退出2，保留旧目标。JSON最多8MB，拒绝重复键、未知字段及非有限常量。先完整验证与计算，再同目录临时文件原子硬链接新建一个JSON报告。run/已忽略。

## 关账流水与可对账月报（asset-close）

`asset-close` 在不改写历史关账数字的前提下，按“已知月份”逐版吸收资产修订，
用与 `asset-schedule` 同一套整月折旧与处置规则生成关账流水，并给出
`booked`（按发布月的历史口径）与 `restated`（按最终版本、经济月重算）两种
月报，截至截止月两种口径按类别对平。旧 `asset-schedule` 的输入、输出和行为
保持不变。字段、归属键、排序与失败约定详见[关账契约](docs/close_contract.md)。

```sh
.venv/bin/asset-close --source fixtures/close_input.json --output run/close_report.json
```

样例含跨月成本/折旧补记、出售更正、未来处置提前撤销、类别替换以及一条截止月
后才知道（须完整校验但不影响报告）的修订；截止月为 2026-06，生成 6 个期间。
输入同样遵守严格 JSON 与 8 MB 上限，非法输入或目标已存在退出 2 并保留旧目标。

## 资产可用性、维护履约与事故归因（asset-availability）

`asset-availability` 按自然日生成设备级状态段、维护订单履约指标与
按月的类别汇总，全部用标准库实现，与财务命令互不影响。字段、逐日优先级、
事故归因与排序约定详见[可用性契约](docs/availability_contract.md)。

```sh
.venv/bin/asset-availability --source fixtures/availability_input.json --output run/availability.json
```

样例报告区间为 `[2026-01-15, 2026-04-10)`，含 3 项资产、8 个维护订单、
3 条事故；生成 2026-01 至 2026-04 四个月（含空月份保留），预期结果见
`fixtures/availability_expected.json`。输入同样遵守严格 JSON 与 8 MB
上限，非法输入或目标已存在退出 2 并保留旧目标。

## 待排检修与连续用机联合调度（asset-dispatch）

`asset-dispatch` 在 `asset-availability` 的固定事实之上，把有检修窗口的
待排检修排进日历，并按“高优先级接单数、普通接单数、检修偏移天数、开工日
向量、分配向量”的字典序目标决定接下哪些连续用机请求；对主计划每笔落选
请求各做一次独立加单分析，说明强制承接后检修如何改期、其余请求如何改派。
字段、校验、目标与决胜顺序详见[调度契约](docs/dispatch_contract.md)。

```sh
.venv/bin/asset-dispatch --source fixtures/dispatch_input.json --output run/dispatch.json
```

样例窗口为 `[2026-05-04, 2026-05-08)`，含 3 台设备、1 个固定维护订单、
1 条事故、2 项待排检修、4 笔请求；主计划接 3 笔、落选 1 笔，预期结果见
`fixtures/dispatch_expected.json`。输入同样遵守严格 JSON 与 8 MB 上限，
非法输入或目标已存在退出 2 并保留旧目标；检修本身不可行时写出
`{"status": "infeasible"}` 并正常退出 0。

## 资产拆并改造的价值承继与按月台账（asset-lineage）

`asset-lineage` 从原始入账与改造、出售事件生成价值承继清单和按月台账。
设备拆分、合装或一换一时，尚未消耗的账面价值按**原始投入 ID** 与**原应计
月份**（残值始终单列）在设备间转移：合装把同源同月份额并成一笔，拆分对
每笔未计提金额按权重分配整数分（先取整数部分，余分按小数部分降序、并列
按 ID 字典序）；出售按事件时点的全部剩余价值转出并确认处置损益。不改造
时折旧与处置口径与 `asset-schedule` 完全一致。字段、生效顺序、取整与对账
约定详见[价值承继契约](docs/lineage_contract.md)。

```sh
.venv/bin/asset-lineage --source fixtures/lineage_input.json --output run/lineage.json
```

样例覆盖 1 月投入 A、2 月投入 B，2 月内 A 等权拆成 L/R、与 B 合装为 M、
M 再等权拆成 X/Y，3 月出售 X，报告到 4 月；预期结果见
`fixtures/lineage_expected.json`。输入同样遵守严格 JSON 与 8 MB 上限，
非法输入（包括回放较晚才发现的引用错误）或目标已存在退出 2 并保留旧目标。

## 资产余额交叉表披露（asset-balance-disclosure）

`asset-balance-disclosure` 在 `asset-lineage` 的截止月设备月表之上，按
「资产类别 × 保管单位」汇总期末账面余额，为园区协作方挑选代价最低且符合
披露口径的交叉表：两侧小计、必须公开格与未遮蔽可选格公开后，每个敏感格
在全部相容非负整数表上的可达跨度仍须达到各自的最小跨度；合格选择中依次
最小化遮蔽损失之和、遮蔽格数与遮蔽键列表字典序。字段、相容表界定与决胜
顺序详见[披露契约](docs/disclosure_contract.md)。

```sh
.venv/bin/asset-balance-disclosure --source fixtures/disclosure_input.json --output run/disclosure.json
```

样例台账含 2 月合装与出售（出售转出价值不计入期末、仅以往月份出现的设备
不进归口映射），三行 A/B/C、三列 X/Y/Z，只有 A/X 敏感；预期结果见
`fixtures/disclosure_expected.json`。输入同样遵守严格 JSON 与 8 MB 上限，
非法输入或目标已存在退出 2 并保留旧目标；没有合格选择时正常退出并只写
`{"status": "blocked", "as_of_month": ...}`。

## 处置后银行收款核销、退扣与催收交接（asset-sale-collections）

`asset-sale-collections` 把 `asset-lineage` 认列的正出售所得按出售事件
建立应收（键为出售事件 ID，本金等于 `proceeds_cents`，零所得不建账），
再按银行到账依到期月从早到晚核销分期；多余款项留作该笔到账自己的未用款，
不冲抵别的应收。退扣只能引用退扣窗口（M、M+1、M+2 月）内的原到账，先退
未用款再按原核销相反次序撤回；窗口关闭后只保留对余额的汇总影响，清除
识别键与逐笔明细。每月末按“紧邻上、本月末均仍逾期”的双月末规则比较
生效委托，发出 `open`/`replace`/`withdraw` 指令；报告同时给出可直接
复制续跑的自包含交接包，任意切点拆批与一次处理的合并结果一致。字段、
不变量、窗口与失败约定详见[收款催收契约](docs/sale_collections_contract.md)。

```sh
.venv/bin/asset-sale-collections --source fixtures/sale_collections_input.json --output run/sale_collections.json
```

样例含两笔正所得出售与一笔零所得出售（后者不建应收）、含未用款的到账、
同月与跨月连续部分退扣、窗口关闭后的明细清除，以及 7 月末首次发出的两条
`open` 委托；预期结果见 `fixtures/sale_collections_expected.json`。输入
同样遵守严格 JSON 与 8 MB 上限，非法输入（含自相矛盾的交接包）或目标已
存在退出 2 并保留旧目标。

## 数据契约和边界

使用文档明确的整月直线法和整数分累计分配，不代表任何地区法定税务或会计准则。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
