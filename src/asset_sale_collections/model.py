"""输入校验：首次形态（lineage/terms）与接续形态（checkpoint）互斥。

银行行只接受契约列出的字段；金额为 1..10^12 的整数分，月份为规范
YYYY-MM。接续批次还要完整复核交接包内部的格式、引用与金额一致性。
"""
from asset_schedule.io import fields, integer, label, rows
from asset_schedule.model import month as month_index
from asset_schedule.model import name as month_name

from .state import (
    CHARGEBACK_WINDOW,
    FORMAT_VERSION,
    MAX_AMOUNT,
    MAX_BANK_ROWS_PER_MONTH,
    MAX_BATCH_MONTHS,
    MAX_INSTALLMENTS,
    MAX_SEQ,
    MAX_TERMS,
    MAX_TOTAL_CENTS,
)

FIRST_FIELDS = ("lineage", "terms", "through_month", "bank_rows")
NEXT_FIELDS = ("checkpoint", "through_month", "bank_rows")
ROW_KINDS = ("receipt", "chargeback")


def _term(value):
    fields(value, ("sale_id", "installments"))
    sale_id = label(value["sale_id"], "terms.sale_id")
    installments = rows(value["installments"], "terms.installments",
                        1, MAX_INSTALLMENTS)
    parsed = []
    seen_months = set()
    for item in installments:
        if not isinstance(item, dict):
            raise ValueError("installment must be an object")
        fields(item, ("due_month", "amount_cents"))
        due = month_index(item["due_month"])
        if due in seen_months:
            raise ValueError(f"duplicate due_month for sale {sale_id}")
        seen_months.add(due)
        amount = integer(item["amount_cents"], "installment.amount_cents",
                         1, MAX_AMOUNT)
        parsed.append((due, amount))
    total = sum(amount for _due, amount in parsed)
    if total > MAX_TOTAL_CENTS:
        raise ValueError("installment total exceeds supported integer range")
    return sale_id, parsed, total


def validate_terms(value):
    """返回 {sale_id: [(due_month_index, amount_cents), ...]}，校验一对一与分期总额。"""
    terms = {}
    for raw in rows(value, "terms", 1, MAX_TERMS):
        if not isinstance(raw, dict):
            raise ValueError("term must be an object")
        sale_id, installments, total = _term(raw)
        if sale_id in terms:
            raise ValueError(f"duplicate terms for sale {sale_id}")
        terms[sale_id] = {"installments": installments, "total": total}
    return terms


def _validate_bank_rows(value, first_month, last_month):
    parsed = []
    seen_keys = set()
    per_month_count = {}
    for raw in rows(value, "bank_rows", 0, MAX_BANK_ROWS_PER_MONTH * MAX_BATCH_MONTHS):
        if not isinstance(raw, dict):
            raise ValueError("bank row must be an object")
        kind = raw.get("kind")
        if kind not in ROW_KINDS:
            raise ValueError(f"unknown bank row kind: {kind!r}")
        row_month = month_index(raw.get("month"))
        if not first_month <= row_month <= last_month:
            raise ValueError("bank row month outside batch range")
        seq = integer(raw.get("seq"), "seq", 1, MAX_SEQ)
        if (row_month, seq) in seen_keys:
            raise ValueError(f"duplicate bank row (month, seq): "
                             f"{month_name(row_month)} {seq}")
        seen_keys.add((row_month, seq))
        per_month_count[row_month] = per_month_count.get(row_month, 0) + 1
        if per_month_count[row_month] > MAX_BANK_ROWS_PER_MONTH:
            raise ValueError(
                f"at most {MAX_BANK_ROWS_PER_MONTH} bank rows per month")
        if kind == "receipt":
            fields(raw, ("month", "seq", "kind", "sale_id", "amount_cents"))
            sale_id = label(raw["sale_id"], "receipt.sale_id")
            amount = integer(raw["amount_cents"], "amount_cents", 1, MAX_AMOUNT)
            parsed.append({"month": row_month, "seq": seq, "kind": "receipt",
                           "sale_id": sale_id, "amount_cents": amount})
        else:
            fields(raw, ("month", "seq", "kind", "receipt_month",
                         "receipt_seq", "amount_cents"))
            receipt_month = month_index(raw["receipt_month"])
            receipt_seq = integer(raw["receipt_seq"], "receipt_seq", 1, MAX_SEQ)
            amount = integer(raw["amount_cents"], "amount_cents", 1, MAX_AMOUNT)
            parsed.append({"month": row_month, "seq": seq, "kind": "chargeback",
                           "receipt_month": receipt_month,
                           "receipt_seq": receipt_seq, "amount_cents": amount})
    return parsed


def _validate_first(data):
    fields(data, FIRST_FIELDS)
    # 完整 lineage 交给现有资产承继台账校验并计算；其失败直接抛出。
    from asset_lineage.service import run as run_lineage

    lineage_report = run_lineage(data["lineage"])
    terms = validate_terms(data["terms"])
    through = month_index(data["through_month"])
    first_month = month_index(lineage_report["as_of_month"]) + 1
    span = through - first_month + 1
    if not 1 <= span <= MAX_BATCH_MONTHS:
        raise ValueError(f"batch must span 1..{MAX_BATCH_MONTHS} months")
    # 以每条正出售所得的 sell 结果建立一笔应收。
    principal = {}
    sell_months = {}
    for event in lineage_report["events"]:
        if event["kind"] != "sell":
            continue
        sale_id = event["id"]
        proceeds = event["proceeds_cents"]
        if proceeds <= 0:
            continue  # 零所得出售不建应收。
        if sale_id in principal:
            raise ValueError(f"duplicate sale event ID: {sale_id}")
        principal[sale_id] = proceeds
        sell_months[sale_id] = month_index(event["month"])
    if not principal:
        raise ValueError("no positive-proceeds sale to open a receivable")
    if set(terms) != set(principal):
        raise ValueError("terms must pair one-to-one with positive-proceeds sales")
    accounts = {}
    for sale_id in sorted(principal):
        spec = terms[sale_id]
        if spec["total"] != principal[sale_id]:
            raise ValueError(
                f"installment total for {sale_id} does not equal proceeds_cents")
        earliest = min(due for due, _amount in spec["installments"])
        if earliest < sell_months[sale_id]:
            raise ValueError(f"installment due before sale month for {sale_id}")
        accounts[sale_id] = {
            "principal": principal[sale_id],
            "installments": [{"due_month": due, "open_cents": amount,
                              "principal_cents": amount}
                             for due, amount in sorted(spec["installments"])],
            "gross_received": 0,
            "gross_chargeback": 0,
            "applied": 0,
            "unapplied": 0,
            "prev_overdue": {},
            "mandate": None,
        }
    bank_rows = _validate_bank_rows(data["bank_rows"], first_month, through)
    _assert_known_sales(bank_rows, accounts)
    return {"form": "first", "first_month": first_month, "through": through,
            "accounts": accounts, "receipts": {}, "bank_rows": bank_rows}


def _checkpoint_integer(value, label_text, maximum=MAX_TOTAL_CENTS):
    return integer(value, label_text, 0, maximum)


def _validate_checkpoint(value):
    if not isinstance(value, dict):
        raise ValueError("checkpoint must be an object")
    fields(value, ("format_version", "through_month", "accounts", "receipts"))
    if value["format_version"] != FORMAT_VERSION:
        raise ValueError(f"unsupported checkpoint format_version: "
                         f"{value['format_version']!r}")
    through = month_index(value["through_month"])
    accounts = {}
    if not isinstance(value["accounts"], list) or not value["accounts"]:
        raise ValueError("checkpoint.accounts requires a nonempty array")
    if len(value["accounts"]) > MAX_TERMS:
        raise ValueError("too many checkpoint accounts")
    for raw in value["accounts"]:
        if not isinstance(raw, dict):
            raise ValueError("checkpoint account must be an object")
        fields(raw, ("sale_id", "principal_cents", "installments",
                     "gross_received_cents", "gross_chargeback_cents",
                     "applied_cents", "unapplied_cents", "prev_overdue",
                     "mandate"))
        sale_id = label(raw["sale_id"], "checkpoint.sale_id")
        if sale_id in accounts:
            raise ValueError(f"duplicate checkpoint account: {sale_id}")
        principal = _checkpoint_integer(raw["principal_cents"],
                                        "principal_cents", MAX_AMOUNT)
        if principal <= 0:
            raise ValueError("checkpoint principal must be positive")
        installments = rows(raw["installments"], "checkpoint.installments",
                            1, MAX_INSTALLMENTS)
        parsed_installments = []
        seen_due = set()
        principal_check = 0
        for item in installments:
            if not isinstance(item, dict):
                raise ValueError("checkpoint installment must be an object")
            fields(item, ("due_month", "open_cents", "principal_cents"))
            due = month_index(item["due_month"])
            if due in seen_due:
                raise ValueError("duplicate checkpoint installment due_month")
            seen_due.add(due)
            open_cents = _checkpoint_integer(item["open_cents"],
                                             "installment.open_cents", MAX_AMOUNT)
            installment_principal = _checkpoint_integer(
                item["principal_cents"], "installment.principal_cents", MAX_AMOUNT)
            if installment_principal <= 0:
                raise ValueError("installment principal must be positive")
            if open_cents > installment_principal:
                raise ValueError("installment open amount exceeds its principal")
            principal_check += installment_principal
            parsed_installments.append({"due_month": due,
                                        "open_cents": open_cents,
                                        "principal_cents": installment_principal})
        parsed_installments.sort(key=lambda item: item["due_month"])
        gross_received = _checkpoint_integer(raw["gross_received_cents"],
                                             "gross_received_cents")
        gross_chargeback = _checkpoint_integer(raw["gross_chargeback_cents"],
                                               "gross_chargeback_cents")
        applied = _checkpoint_integer(raw["applied_cents"], "applied_cents")
        unapplied = _checkpoint_integer(raw["unapplied_cents"], "unapplied_cents")
        if gross_chargeback > gross_received:
            raise ValueError(f"chargeback exceeds receipts for {sale_id}")
        net = gross_received - gross_chargeback
        # 每笔应收的累计到账净额始终等于已核销本金加未用款。
        if net != applied + unapplied:
            raise ValueError(f"net receipt mismatch for {sale_id}")
        outstanding = sum(item["open_cents"] for item in parsed_installments)
        # 已核销本金加尚欠本金等于本金。
        if applied + outstanding != principal:
            raise ValueError(f"applied plus outstanding mismatch for {sale_id}")
        # 各期约定本金之和须等于应收本金；open 不得高于约定本金。
        if principal_check != principal:
            raise ValueError(f"installment principals do not sum to principal "
                             f"for {sale_id}")
        prev_overdue = _validate_overdue_map(raw["prev_overdue"], sale_id,
                                             parsed_installments)
        mandate = _validate_mandate(raw["mandate"], sale_id, parsed_installments)
        accounts[sale_id] = {
            "installments": parsed_installments,
            "principal": principal,
            "gross_received": gross_received,
            "gross_chargeback": gross_chargeback,
            "applied": applied,
            "unapplied": unapplied,
            "prev_overdue": prev_overdue,
            "mandate": mandate,
        }
    receipts = _validate_receipts(value["receipts"], accounts, through)
    _assert_receipt_totals(accounts, receipts)
    return through, accounts, receipts


def _validate_overdue_map(value, sale_id, installments):
    if not isinstance(value, dict):
        raise ValueError("prev_overdue must be an object")
    due_set = {item["due_month"] for item in installments}
    result = {}
    for key, amount in value.items():
        due = month_index(key)
        if due not in due_set:
            raise ValueError(f"prev_overdue references unknown installment: {sale_id}")
        amount = _checkpoint_integer(amount, "prev_overdue amount", MAX_AMOUNT)
        if amount <= 0:
            raise ValueError("prev_overdue amounts must be positive")
        open_cents = next(item["open_cents"] for item in installments
                          if item["due_month"] == due)
        if amount > open_cents:
            raise ValueError("prev_overdue exceeds installment open amount")
        result[due] = amount
    return result


def _validate_mandate(value, sale_id, installments):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("mandate must be null or an object")
    fields(value, ("installments",))
    entries = rows(value["installments"], "mandate.installments", 1, MAX_INSTALLMENTS)
    due_set = {item["due_month"] for item in installments}
    mandate = []
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("mandate installment must be an object")
        fields(item, ("due_month", "open_cents"))
        due = month_index(item["due_month"])
        if due in seen:
            raise ValueError("duplicate mandate installment")
        seen.add(due)
        if due not in due_set:
            raise ValueError(f"mandate references unknown installment: {sale_id}")
        amount = _checkpoint_integer(item["open_cents"], "mandate.open_cents",
                                     MAX_AMOUNT)
        if amount <= 0:
            raise ValueError("mandate amounts must be positive")
        open_cents = next(row["open_cents"] for row in installments
                          if row["due_month"] == due)
        if amount > open_cents:
            raise ValueError("mandate amount exceeds installment open amount")
        mandate.append({"due_month": due, "open_cents": amount})
    mandate.sort(key=lambda item: item["due_month"])
    return mandate


def _validate_receipts(value, accounts, through):
    if not isinstance(value, list):
        raise ValueError("checkpoint.receipts must be an array")
    if len(value) > MAX_BANK_ROWS_PER_MONTH * 3:
        raise ValueError("too many live receipts in checkpoint")
    receipts = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("checkpoint receipt must be an object")
        fields(raw, ("month", "seq", "sale_id", "amount_cents",
                     "chargeback_cents", "unapplied_cents", "applications"))
        receipt_month = month_index(raw["month"])
        # 交接包只允许保留仍在退扣窗口（M、M+1、M+2）内的到账；M+2 当月
        # 仍可退扣，故在截止月 M+2 的交接包里该到账必须还在。
        if receipt_month + CHARGEBACK_WINDOW < through:
            raise ValueError("checkpoint retains a receipt past its chargeback window")
        seq = integer(raw["seq"], "receipt.seq", 1, MAX_SEQ)
        sale_id = label(raw["sale_id"], "receipt.sale_id")
        if sale_id not in accounts:
            raise ValueError(f"receipt references unknown sale: {sale_id}")
        amount = integer(raw["amount_cents"], "receipt.amount_cents",
                         1, MAX_AMOUNT)
        chargeback = _checkpoint_integer(raw["chargeback_cents"],
                                         "receipt.chargeback_cents", MAX_AMOUNT)
        unapplied = _checkpoint_integer(raw["unapplied_cents"],
                                        "receipt.unapplied_cents", MAX_AMOUNT)
        if chargeback > amount:
            raise ValueError("receipt chargeback exceeds its amount")
        applications = rows(raw["applications"], "receipt.applications",
                            0, MAX_INSTALLMENTS)
        due_set = {item["due_month"]
                   for item in accounts[sale_id]["installments"]}
        parsed_apps = []
        seen_due = set()
        applied_total = 0
        for item in applications:
            if not isinstance(item, dict):
                raise ValueError("receipt application must be an object")
            fields(item, ("due_month", "open_cents"))
            due = month_index(item["due_month"])
            if due in seen_due:
                raise ValueError("duplicate receipt application installment")
            seen_due.add(due)
            if due not in due_set:
                raise ValueError("receipt application references unknown installment")
            app_amount = integer(item["open_cents"], "application.open_cents",
                                 1, MAX_AMOUNT)
            applied_total += app_amount
            parsed_apps.append({"due_month": due, "open_cents": app_amount})
        parsed_apps.sort(key=lambda item: item["due_month"])
        # 到账金额 = 已退 + 未用 + 净核销；净核销为逐期份额之和。
        if amount != chargeback + unapplied + applied_total:
            raise ValueError(f"receipt {sale_id} parts do not sum to its amount")
        key = (receipt_month, seq)
        if key in receipts:
            raise ValueError("duplicate checkpoint receipt key")
        receipts[key] = {
            "month": receipt_month, "seq": seq, "sale_id": sale_id,
            "amount": amount, "chargeback": chargeback,
            "unapplied": unapplied, "applications": parsed_apps,
        }
    return receipts


def _assert_receipt_totals(accounts, receipts):
    """逐笔到账的净核销/未用/退扣加总必须等于各应收汇总数。"""
    per_account = {sale_id: {"gross": 0, "chargeback": 0,
                             "applied": 0, "unapplied": 0}
                   for sale_id in accounts}
    for receipt in receipts.values():
        slot = per_account[receipt["sale_id"]]
        slot["gross"] += receipt["amount"]
        slot["chargeback"] += receipt["chargeback"]
        slot["unapplied"] += receipt["unapplied"]
        slot["applied"] += sum(item["open_cents"]
                               for item in receipt["applications"])
    for sale_id, account in accounts.items():
        slot = per_account[sale_id]
        # 已过退扣窗口的到账只剩汇总影响，因此活明细加总不得超过汇总数。
        if slot["gross"] > account["gross_received"]:
            raise ValueError(f"receipt gross exceeds account total for {sale_id}")
        if slot["chargeback"] > account["gross_chargeback"]:
            raise ValueError(f"receipt chargeback exceeds account total for {sale_id}")
        if slot["applied"] > account["applied"]:
            raise ValueError(f"receipt applied exceeds account total for {sale_id}")
        if slot["unapplied"] > account["unapplied"]:
            raise ValueError(f"receipt unapplied exceeds account total for {sale_id}")


def _assert_known_sales(bank_rows, accounts):
    for row in bank_rows:
        if row["kind"] == "receipt" and row["sale_id"] not in accounts:
            raise ValueError(f"receipt references unknown sale: {row['sale_id']}")


def validate(data):
    """返回引擎所需的标准化批次状态，任何矛盾均抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("root must be an object")
    keys = set(data)
    first = set(FIRST_FIELDS)
    next_ = set(NEXT_FIELDS)
    if keys == first:
        return _validate_first(data)
    if keys == next_:
        through = month_index(data["through_month"])
        checkpoint_through, accounts, receipts = _validate_checkpoint(
            data["checkpoint"])
        first_month = checkpoint_through + 1
        span = through - first_month + 1
        if not 1 <= span <= MAX_BATCH_MONTHS:
            raise ValueError(f"batch must span 1..{MAX_BATCH_MONTHS} months")
        bank_rows = _validate_bank_rows(data["bank_rows"], first_month, through)
        _assert_known_sales(bank_rows, accounts)
        return {"form": "next", "first_month": first_month, "through": through,
                "accounts": accounts, "receipts": receipts, "bank_rows": bank_rows}
    raise ValueError("root must be exactly the initial or continuation field set")
