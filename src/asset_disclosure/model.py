"""asset-balance-disclosure 输入校验：披露政策、归口映射与行列标签。

台账部分（`lineage` 字段）是一份完整的 asset-lineage 输入，由该命令自己的
校验与回放负责；本模块只校验披露层面的静态结构。设备层面的交叉核对
（归口映射恰好覆盖截止月设备、类别覆盖、结构零真值）在 service 装配时完成。
"""
from asset_schedule.io import fields, integer, rows

MODES = ("protected", "optional", "public", "structural_zero")


def _labels(value, name):
    """1–4 个不重复、非全空白、最长 100 字符的字符串标签，精确匹配不裁剪。"""
    result = []
    for item in rows(value, name, 1, 4):
        if not isinstance(item, str) or not item.strip() or len(item) > 100:
            raise ValueError(f"{name} labels require nonempty string <=100")
        if item in result:
            raise ValueError(f"duplicate {name} label: {item}")
        result.append(item)
    return result


def _cells(value, categories, custodians):
    """行列笛卡尔积每格恰好一条政策；返回 (category, custodian) -> 政策。"""
    expected = len(categories) * len(custodians)
    entries = rows(value, "cells", expected, expected)
    category_set = set(categories)
    custodian_set = set(custodians)
    policies = {}
    protected = 0
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("cell must be an object")
        mode = raw.get("mode")
        if mode not in MODES:
            raise ValueError(f"unknown cell mode: {mode!r}")
        if mode == "protected":
            fields(raw, ("category", "custodian", "mode", "min_span_cents"))
            policy = {"mode": mode,
                      "min_span_cents": integer(raw["min_span_cents"],
                                                "min_span_cents", 1, 10**12)}
            protected += 1
        elif mode == "optional":
            fields(raw, ("category", "custodian", "mode", "suppression_cost"))
            policy = {"mode": mode,
                      "suppression_cost": integer(raw["suppression_cost"],
                                                  "suppression_cost", 1, 10**6)}
        else:
            fields(raw, ("category", "custodian", "mode"))
            policy = {"mode": mode}
        category = raw["category"]
        custodian = raw["custodian"]
        if not isinstance(category, str) or category not in category_set:
            raise ValueError(f"cell category not listed in categories: {category!r}")
        if not isinstance(custodian, str) or custodian not in custodian_set:
            raise ValueError(f"cell custodian not listed in custodians: {custodian!r}")
        key = (category, custodian)
        if key in policies:
            raise ValueError(f"duplicate cell policy: {key}")
        policies[key] = policy
    if protected == 0:
        raise ValueError("at least one protected cell is required")
    return policies


def _mapping(value, custodians):
    """设备 ID 到保管单位的归口对象；值必须在保管单位表内，键集留待回放后核对。"""
    if not isinstance(value, dict):
        raise ValueError("custodian_by_device must be an object")
    custodian_set = set(custodians)
    for device_id, custodian in value.items():
        if not isinstance(custodian, str) or custodian not in custodian_set:
            raise ValueError(
                f"custodian_by_device[{device_id}] not listed in custodians")
    return dict(value)


def validate(data):
    """校验根对象与披露政策，返回 (类别表, 保管单位表, 政策, 归口映射)。"""
    fields(data, ("lineage", "categories", "custodians",
                  "custodian_by_device", "cells"))
    if not isinstance(data["lineage"], dict):
        raise ValueError("lineage must be an object")
    categories = _labels(data["categories"], "categories")
    custodians = _labels(data["custodians"], "custodians")
    policies = _cells(data["cells"], categories, custodians)
    mapping = _mapping(data["custodian_by_device"], custodians)
    return {"categories": categories, "custodians": custodians,
            "policies": policies, "mapping": mapping}
