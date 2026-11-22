"""收款核销、退扣、月末催收观察与交接包生成引擎。

不变量（任何时点成立，违约即 ValueError）：
- 每笔应收：累计到账净额 = 已核销本金 + 未用款；
- 已核销本金 + 尚欠本金 = 本金；各期欠款不超过该期约定本金。
- 每笔到账：到账金额 = 已退金额 + 未用款 + 尚未撤回的净核销份额。
退扣只动被引用到账自己的未用款与核销份额，不触碰其他到账，也不自动
动用别笔留下的未用款。
"""
from asset_schedule.model import name as month_name

from .state import CHARGEBACK_WINDOW, FORMAT_VERSION, MAX_TOTAL_CENTS


class Ledger:
    """持有全部应收与仍在退扣窗口内的到账明细。"""

    def __init__(self, accounts, receipts):
        self.accounts = accounts
        self.receipts = receipts

    def _account(self, sale_id):
        account = self.accounts.get(sale_id)
        if account is None:
            raise ValueError(f"receipt references unknown sale: {sale_id}")
        return account

    def _installment(self, account, due_month):
        for item in account["installments"]:
            if item["due_month"] == due_month:
                return item
        raise ValueError("application references unknown installment")

    def receive(self, row):
        account = self._account(row["sale_id"])
        remaining = row["amount_cents"]
        applications = []
        # 按到期月从早到晚清偿尚欠本金，未到期也可提前偿付。
        for installment in account["installments"]:
            if remaining == 0:
                break
            paid = min(remaining, installment["open_cents"])
            if paid:
                installment["open_cents"] -= paid
                applications.append({"due_month": installment["due_month"],
                                     "open_cents": paid})
                remaining -= paid
        applied = row["amount_cents"] - remaining
        account["gross_received"] += row["amount_cents"]
        account["applied"] += applied
        account["unapplied"] += remaining
        # 多余款项作为这笔到账的未用款保留，不冲抵别的应收。
        self.receipts[(row["month"], row["seq"])] = {
            "month": row["month"], "seq": row["seq"],
            "sale_id": row["sale_id"], "amount": row["amount_cents"],
            "chargeback": 0, "unapplied": remaining,
            "applications": applications}
        self._check_account(account)

    def charge_back(self, row):
        current = row["month"]
        key = (row["receipt_month"], row["receipt_seq"])
        receipt = self.receipts.get(key)
        if receipt is None:
            raise ValueError(
                "chargeback references a receipt that does not exist or is no "
                "longer within its chargeback window")
        if not receipt["month"] <= current <= receipt["month"] + CHARGEBACK_WINDOW:
            raise ValueError("chargeback references a receipt past its window")
        remaining_net = receipt["amount"] - receipt["chargeback"]
        if row["amount_cents"] > remaining_net:
            raise ValueError("chargeback exceeds the receipt's remaining net amount")
        account = self._account(receipt["sale_id"])
        remaining = row["amount_cents"]

        # 先从原到账的未用款退。
        from_unapplied = min(remaining, receipt["unapplied"])
        if from_unapplied:
            receipt["unapplied"] -= from_unapplied
            account["unapplied"] -= from_unapplied
            remaining -= from_unapplied

        # 再按该到账原先核销的相反次序撤回尚未退掉的份额。
        for application in reversed(receipt["applications"]):
            if remaining == 0:
                break
            withdrawn = min(remaining, application["open_cents"])
            if withdrawn:
                installment = self._installment(account,
                                                application["due_month"])
                if installment["open_cents"] + withdrawn \
                        > installment["principal_cents"]:
                    raise ValueError(
                        "chargeback would push an installment above its "
                        "contractual principal")
                installment["open_cents"] += withdrawn
                application["open_cents"] -= withdrawn
                account["applied"] -= withdrawn
                remaining -= withdrawn
        receipt["applications"] = [item for item in receipt["applications"]
                                   if item["open_cents"]]
        receipt["chargeback"] += row["amount_cents"]
        account["gross_chargeback"] += row["amount_cents"]
        if remaining != 0:
            raise ValueError("chargeback could not be fully allocated")
        self._check_account(account)
        self._check_receipt(receipt)

    def month_end(self, month):
        """返回本月催收指令行（可能为空列表），并推进逾期观察与生效委托。"""
        orders = []
        for sale_id in sorted(self.accounts):
            account = self.accounts[sale_id]
            current_overdue = {
                item["due_month"]: item["open_cents"]
                for item in account["installments"]
                if item["due_month"] < month and item["open_cents"] > 0}
            # 紧邻的上个月末与本月末都仍有正数逾期欠款，才允许交给催收中心。
            eligible = [
                {"due_month": due,
                 "open_cents": current_overdue[due]}
                for due in sorted(current_overdue)
                if account["prev_overdue"].get(due, 0) > 0]
            order = _compare_mandate(sale_id, account["mandate"], eligible)
            if order is not None:
                orders.append(order)
            account["prev_overdue"] = current_overdue
            account["mandate"] = eligible or None
            self._check_account(account)
        # 到 M+2 月业务结束即不可逆：M+2 月内仍可退扣，月末处理完后清除该
        # 到账的全部识别键、逐笔金额与核销明细，只留其对账户余额的汇总影响。
        for key in [key for key, receipt in self.receipts.items()
                    if receipt["month"] + CHARGEBACK_WINDOW <= month]:
            del self.receipts[key]
        return orders

    def report_row(self, sale_id):
        account = self.accounts[sale_id]
        outstanding = sum(item["open_cents"]
                          for item in account["installments"])
        return {
            "sale_id": sale_id,
            "received_net_cents": account["gross_received"]
            - account["gross_chargeback"],
            "applied_cents": account["applied"],
            "unapplied_cents": account["unapplied"],
            "outstanding_cents": outstanding,
            "installments": [
                {"due_month": month_name(item["due_month"]),
                 "open_cents": item["open_cents"]}
                for item in account["installments"]],
        }

    def checkpoint(self, through):
        accounts = []
        for sale_id in sorted(self.accounts):
            account = self.accounts[sale_id]
            accounts.append({
                "sale_id": sale_id,
                "principal_cents": account["principal"],
                "installments": [
                    {"due_month": month_name(item["due_month"]),
                     "open_cents": item["open_cents"],
                     "principal_cents": item["principal_cents"]}
                    for item in account["installments"]],
                "gross_received_cents": account["gross_received"],
                "gross_chargeback_cents": account["gross_chargeback"],
                "applied_cents": account["applied"],
                "unapplied_cents": account["unapplied"],
                "prev_overdue": {
                    month_name(due): amount
                    for due, amount in sorted(account["prev_overdue"].items())},
                "mandate": None if account["mandate"] is None else {
                    "installments": [
                        {"due_month": month_name(item["due_month"]),
                         "open_cents": item["open_cents"]}
                        for item in account["mandate"]]},
            })
        receipts = []
        for key in sorted(self.receipts):
            receipt = self.receipts[key]
            receipts.append({
                "month": month_name(receipt["month"]),
                "seq": receipt["seq"],
                "sale_id": receipt["sale_id"],
                "amount_cents": receipt["amount"],
                "chargeback_cents": receipt["chargeback"],
                "unapplied_cents": receipt["unapplied"],
                "applications": [
                    {"due_month": month_name(item["due_month"]),
                     "open_cents": item["open_cents"]}
                    for item in receipt["applications"]],
            })
        return {"format_version": FORMAT_VERSION,
                "through_month": month_name(through),
                "accounts": accounts, "receipts": receipts}

    def _check_account(self, account):
        for key in ("gross_received", "gross_chargeback", "applied",
                    "unapplied"):
            if not 0 <= account[key] <= MAX_TOTAL_CENTS:
                raise ValueError(f"account aggregate out of range: {key}")
        if account["gross_chargeback"] > account["gross_received"]:
            raise ValueError("chargeback exceeds gross receipts")
        net = account["gross_received"] - account["gross_chargeback"]
        if net != account["applied"] + account["unapplied"]:
            raise ValueError("net receipt must equal applied plus unapplied")
        outstanding = 0
        for item in account["installments"]:
            if not 0 <= item["open_cents"] <= item["principal_cents"]:
                raise ValueError("installment open amount out of range")
            outstanding += item["open_cents"]
        if account["applied"] + outstanding != account["principal"]:
            raise ValueError("applied plus outstanding must equal principal")

    def _check_receipt(self, receipt):
        applied = sum(item["open_cents"]
                      for item in receipt["applications"])
        if receipt["amount"] != receipt["chargeback"] + receipt["unapplied"] \
                + applied:
            raise ValueError("receipt parts must sum to its amount")


def _compare_mandate(sale_id, old, new):
    old = old or []
    if not old and not new:
        return None
    if old and not new:
        return {"sale_id": sale_id, "action": "withdraw", "installments": []}
    if not old and new:
        action = "open"
    elif old != new:
        action = "replace"
    else:
        return None
    return {"sale_id": sale_id, "action": action,
            "installments": [
                {"due_month": month_name(item["due_month"]),
                 "open_cents": item["open_cents"]} for item in new]}


def run_batch(validated):
    """按月回放整个批次，返回 (periods, checkpoint)。"""
    ledger = Ledger(validated["accounts"], validated["receipts"])
    first_month = validated["first_month"]
    through = validated["through"]
    by_month = {}
    for row in validated["bank_rows"]:
        by_month.setdefault(row["month"], []).append(row)

    periods = []
    for current in range(first_month, through + 1):
        for row in sorted(by_month.get(current, ()), key=lambda item: item["seq"]):
            if row["kind"] == "receipt":
                ledger.receive(row)
            else:
                ledger.charge_back(row)
        orders = ledger.month_end(current)
        periods.append({
            "month": month_name(current),
            "accounts": [ledger.report_row(sale_id)
                         for sale_id in sorted(ledger.accounts)],
            "collection_orders": orders,
        })
    return periods, ledger.checkpoint(through)
