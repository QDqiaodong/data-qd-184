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

## 数据契约和边界

使用文档明确的整月直线法和整数分累计分配，不代表任何地区法定税务或会计准则。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
