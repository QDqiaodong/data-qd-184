import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from asset_sale_collections.service import run
from asset_sale_collections.state import FORMAT_VERSION

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


# ---------------------------------------------------------------------------
# 任务书验收场景的最小业务事实（2025-12 出售 S，所得 100；1 月 60、2 月 40）。
# ---------------------------------------------------------------------------

def lineage_one(proceeds=100):
    return {
        "from_month": "2025-12", "as_of_month": "2025-12",
        "assets": [{"id": "D", "category": "equipment", "cost_cents": 250,
                    "salvage_cents": 50, "life_months": 4,
                    "in_service_month": "2025-12", "disposal": None}],
        "events": [{"id": "S", "month": "2025-12", "seq": 1, "kind": "sell",
                    "asset_id": "D", "proceeds_cents": proceeds}]}


TERMS_S = [{"sale_id": "S", "installments": [
    {"due_month": "2026-01", "amount_cents": 60},
    {"due_month": "2026-02", "amount_cents": 40}]}]


def receipt(month, seq, sale_id, amount):
    return {"month": month, "seq": seq, "kind": "receipt",
            "sale_id": sale_id, "amount_cents": amount}


def chargeback(month, seq, receipt_month, receipt_seq, amount):
    return {"month": month, "seq": seq, "kind": "chargeback",
            "receipt_month": receipt_month, "receipt_seq": receipt_seq,
            "amount_cents": amount}


def continuation(checkpoint, through, rows):
    return {"checkpoint": checkpoint, "through_month": through,
            "bank_rows": rows}


def account_row(report, month, sale_id="S"):
    period = next(p for p in report["periods"] if p["month"] == month)
    return next(a for a in period["accounts"] if a["sale_id"] == sale_id)


def orders(report, month):
    return next(p for p in report["periods"] if p["month"] == month)[
        "collection_orders"]


class AcceptanceExampleTests(unittest.TestCase):
    """任务书验收例子，全部预期值独立手工推导。"""

    def test_february_end_balances_after_two_receipts(self):
        report = run({"lineage": lineage_one(), "terms": TERMS_S,
                      "through_month": "2026-02",
                      "bank_rows": [receipt("2026-01", 1, "S", 90),
                                    receipt("2026-02", 1, "S", 20)]})
        row = account_row(report, "2026-02")
        # 2 月末应收已清、净到账 110、已核销 100、未用 10。
        self.assertEqual((row["received_net_cents"], row["applied_cents"],
                          row["unapplied_cents"], row["outstanding_cents"]),
                         (110, 100, 10, 0))
        self.assertEqual([i["open_cents"] for i in row["installments"]], [0, 0])
        self.assertEqual(orders(report, "2026-01"), [])
        self.assertEqual(orders(report, "2026-02"), [])

    def _february_checkpoint(self):
        return run({"lineage": lineage_one(), "terms": TERMS_S,
                    "through_month": "2026-02",
                    "bank_rows": [receipt("2026-01", 1, "S", 90),
                                  receipt("2026-02", 1, "S", 20)]})["checkpoint"]

    def test_march_chargeback_reopens_installments_and_purges_p(self):
        march = run(continuation(self._february_checkpoint(), "2026-03",
                                 [chargeback("2026-03", 1, "2026-01", 1, 35)]))
        row = account_row(march, "2026-03")
        # 净到账 75、已核销 65、未用 10；第一期欠 5、第二期欠 30。
        self.assertEqual((row["received_net_cents"], row["applied_cents"],
                          row["unapplied_cents"], row["outstanding_cents"]),
                         (75, 65, 10, 35))
        self.assertEqual([i["open_cents"] for i in row["installments"]], [5, 30])
        # 首次再次观察到逾期，不发委托。
        self.assertEqual(orders(march, "2026-03"), [])
        # 交接包不再保留 P 的逐笔信息，仍认得 Q。
        keys = {(r["month"], r["seq"])
                for r in march["checkpoint"]["receipts"]}
        self.assertEqual(keys, {("2026-02", 1)})

    def test_april_chargeback_of_expired_p_rejected(self):
        march = run(continuation(self._february_checkpoint(), "2026-03",
                                 [chargeback("2026-03", 1, "2026-01", 1, 35)]))
        with self.assertRaisesRegex(ValueError, "no longer within"):
            run(continuation(march["checkpoint"], "2026-04",
                             [chargeback("2026-04", 1, "2026-01", 1, 1)]))

    def test_april_q_chargeback_opens_two_installment_mandate(self):
        march = run(continuation(self._february_checkpoint(), "2026-03",
                                 [chargeback("2026-03", 1, "2026-01", 1, 35)]))
        april = run(continuation(march["checkpoint"], "2026-04",
                                 [chargeback("2026-04", 1, "2026-02", 1, 12)]))
        row = account_row(april, "2026-04")
        # 先退 Q 的未用 10，再撤回第二期 2。
        self.assertEqual((row["received_net_cents"], row["applied_cents"],
                          row["unapplied_cents"]), (63, 63, 0))
        self.assertEqual(orders(april, "2026-04"), [
            {"sale_id": "S", "action": "open", "installments": [
                {"due_month": "2026-01", "open_cents": 5},
                {"due_month": "2026-02", "open_cents": 32}]}])
        self.assertEqual(april["checkpoint"]["receipts"], [])

    def test_thirty_cent_variant_authorizes_only_second_installment(self):
        # 只把 3 月对 P 的退扣改为 30。
        march = run(continuation(self._february_checkpoint(), "2026-03",
                                 [chargeback("2026-03", 1, "2026-01", 1, 30)]))
        april = run(continuation(march["checkpoint"], "2026-04",
                                 [chargeback("2026-04", 1, "2026-02", 1, 12)]))
        self.assertEqual(orders(april, "2026-04"), [
            {"sale_id": "S", "action": "open", "installments": [
                {"due_month": "2026-02", "open_cents": 32}]}])

    def test_may_replace_and_june_withdraw(self):
        march = run(continuation(self._february_checkpoint(), "2026-03",
                                 [chargeback("2026-03", 1, "2026-01", 1, 35)]))
        april = run(continuation(march["checkpoint"], "2026-04",
                                 [chargeback("2026-04", 1, "2026-02", 1, 12)]))
        may = run(continuation(april["checkpoint"], "2026-05",
                               [receipt("2026-05", 1, "S", 5)]))
        self.assertEqual(orders(may, "2026-05"), [
            {"sale_id": "S", "action": "replace", "installments": [
                {"due_month": "2026-02", "open_cents": 32}]}])
        june = run(continuation(may["checkpoint"], "2026-06",
                                [receipt("2026-06", 1, "S", 32)]))
        self.assertEqual(orders(june, "2026-06"), [
            {"sale_id": "S", "action": "withdraw", "installments": []}])
        self.assertEqual(account_row(june, "2026-06")["outstanding_cents"], 0)

    def test_receipt_ordering_variant_same_balances_different_orders(self):
        # P 移到 2 月且排在 Q 之前。
        base = {"lineage": lineage_one(), "terms": TERMS_S,
                "through_month": "2026-02",
                "bank_rows": [receipt("2026-02", 1, "S", 90),
                              receipt("2026-02", 2, "S", 20)]}
        feb = run(base)
        # 路径 A：P 的 35 退扣在 3 月。
        a3 = run(continuation(feb["checkpoint"], "2026-03",
                              [chargeback("2026-03", 1, "2026-02", 1, 35)]))
        a4 = run(continuation(a3["checkpoint"], "2026-04",
                              [chargeback("2026-04", 1, "2026-02", 2, 12)]))
        # 路径 B：P 的 35 退扣在 4 月，且在 Q 的退扣之前。
        b4 = run(continuation(feb["checkpoint"], "2026-04", [
            chargeback("2026-04", 1, "2026-02", 1, 35),
            chargeback("2026-04", 2, "2026-02", 2, 12)]))
        # 两路 4 月应收余额完全相同。
        self.assertEqual(account_row(a4, "2026-04"), account_row(b4, "2026-04"))
        self.assertEqual(orders(a4, "2026-04")[0]["action"], "open")
        self.assertEqual(orders(b4, "2026-04"), [])
        # 没有银行行的 5 月也推进催收观察：首次满足连续两月末逾期 -> open。
        b5 = run(continuation(b4["checkpoint"], "2026-05", []))
        self.assertEqual(orders(b5, "2026-05")[0]["action"], "open")
        self.assertEqual(account_row(b5, "2026-05"), account_row(b4, "2026-04"))


class SplitEquivalenceTests(unittest.TestCase):
    """同一连续月份：一次处理与任意切点拆批结果一致。"""

    ROWS = [
        receipt("2026-01", 1, "S", 90),
        receipt("2026-02", 1, "S", 20),
        chargeback("2026-03", 1, "2026-01", 1, 35),
        chargeback("2026-04", 1, "2026-02", 1, 12),
        receipt("2026-05", 1, "S", 5),
        receipt("2026-06", 1, "S", 32)]

    def _full(self):
        return run({"lineage": lineage_one(), "terms": TERMS_S,
                    "through_month": "2026-06", "bank_rows": self.ROWS})

    def test_monthly_splits_match_single_batch(self):
        full = self._full()
        first = run({"lineage": lineage_one(), "terms": TERMS_S,
                     "through_month": "2026-02", "bank_rows": self.ROWS[:2]})
        periods = list(first["periods"])
        checkpoint = first["checkpoint"]
        for month in ("2026-03", "2026-04", "2026-05", "2026-06"):
            part = run(continuation(
                checkpoint, month,
                [row for row in self.ROWS if row["month"] == month]))
            periods.extend(part["periods"])
            checkpoint = part["checkpoint"]
        self.assertEqual(periods, full["periods"])
        self.assertEqual(checkpoint, full["checkpoint"])

    def test_arbitrary_cut_points_match_single_batch(self):
        full = self._full()
        cut1 = run({"lineage": lineage_one(), "terms": TERMS_S,
                    "through_month": "2026-02", "bank_rows": self.ROWS[:2]})
        cut2 = run(continuation(cut1["checkpoint"], "2026-04", self.ROWS[2:4]))
        cut3 = run(continuation(cut2["checkpoint"], "2026-06", self.ROWS[4:]))
        self.assertEqual(cut1["periods"] + cut2["periods"] + cut3["periods"],
                         full["periods"])
        self.assertEqual(cut3["checkpoint"], full["checkpoint"])

    def test_empty_months_are_kept(self):
        report = run({"lineage": lineage_one(), "terms": TERMS_S,
                      "through_month": "2026-04", "bank_rows": []})
        self.assertEqual([p["month"] for p in report["periods"]],
                         ["2026-01", "2026-02", "2026-03", "2026-04"])
        for period in report["periods"]:
            self.assertEqual(len(period["accounts"]), 1)


class ReceiptAndChargebackTests(unittest.TestCase):
    def setUp(self):
        self.lineage = {
            "from_month": "2026-01", "as_of_month": "2026-01",
            "assets": [{"id": "A", "category": "equipment", "cost_cents": 100,
                        "salvage_cents": 0, "life_months": 1,
                        "in_service_month": "2026-01", "disposal": None}],
            "events": [{"id": "S1", "month": "2026-01", "seq": 1, "kind": "sell",
                        "asset_id": "A", "proceeds_cents": 100}]}
        self.terms = [{"sale_id": "S1", "installments": [
            {"due_month": "2026-02", "amount_cents": 60},
            {"due_month": "2026-03", "amount_cents": 40}]}]

    def test_same_month_seq_order_governs_application(self):
        # seq1=50 先清第一期，seq2=30 再清第一期 10 与第二期 20。
        report = run({"lineage": self.lineage, "terms": self.terms,
                      "through_month": "2026-02",
                      "bank_rows": [receipt("2026-02", 2, "S1", 30),
                                    receipt("2026-02", 1, "S1", 50)]})
        row = account_row(report, "2026-02", "S1")
        self.assertEqual([i["open_cents"] for i in row["installments"]], [0, 20])

    def test_successive_partial_chargebacks_then_overcharge_rejected(self):
        data = {"lineage": self.lineage, "terms": self.terms,
                "through_month": "2026-03",
                "bank_rows": [
                    receipt("2026-02", 1, "S1", 50),
                    receipt("2026-02", 2, "S1", 30),
                    chargeback("2026-03", 1, "2026-02", 1, 20),
                    chargeback("2026-03", 2, "2026-02", 1, 25),
                    chargeback("2026-03", 3, "2026-02", 1, 6)]}
        with self.assertRaisesRegex(ValueError, "remaining net amount"):
            run(data)
        data["bank_rows"][-1]["amount_cents"] = 5
        report = run(data)
        row = account_row(report, "2026-03", "S1")
        # seq1 原核销 50 全撤回：第一期恢复欠 50；seq2 净核销 30 仍在，
        # 故第二期仍欠 40-20=20；净到账 80-50=30 全部对应 seq2 的核销。
        self.assertEqual((row["received_net_cents"], row["applied_cents"],
                          row["unapplied_cents"], row["outstanding_cents"]),
                         (30, 30, 0, 70))
        self.assertEqual([i["open_cents"] for i in row["installments"]], [50, 20])

    def test_chargeback_takes_unapplied_before_applications(self):
        report = run({"lineage": self.lineage, "terms": self.terms,
                      "through_month": "2026-03",
                      "bank_rows": [
                          receipt("2026-02", 1, "S1", 150),
                          chargeback("2026-03", 1, "2026-02", 1, 70)]})
        # 到账 150：核销 100、未用 50；退 70：先退未用 50，再撤回第二期 20。
        row = account_row(report, "2026-03", "S1")
        self.assertEqual((row["received_net_cents"], row["applied_cents"],
                          row["unapplied_cents"], row["outstanding_cents"]),
                         (80, 80, 0, 20))
        self.assertEqual([i["open_cents"] for i in row["installments"]], [0, 20])

    def test_chargeback_never_touches_other_receipts_unapplied(self):
        report = run({"lineage": self.lineage, "terms": self.terms,
                      "through_month": "2026-03",
                      "bank_rows": [
                          receipt("2026-02", 1, "S1", 150),
                          receipt("2026-02", 2, "S1", 10),
                          chargeback("2026-03", 1, "2026-02", 2, 10)]})
        row = account_row(report, "2026-03", "S1")
        # 第二笔 10 全未用并被退光；第一笔留下的未用 50 不动。
        self.assertEqual((row["applied_cents"], row["unapplied_cents"]), (100, 50))

    def test_invalid_and_expired_chargeback_references_rejected(self):
        base = {"lineage": self.lineage, "terms": self.terms,
                "through_month": "2026-02",
                "bank_rows": [receipt("2026-02", 1, "S1", 50)]}
        with self.assertRaisesRegex(ValueError, "no longer within"):
            run({"lineage": self.lineage, "terms": self.terms,
                 "through_month": "2026-02",
                 "bank_rows": [chargeback("2026-02", 1, "2026-02", 9, 5)]})
        # 窗口外（M+3）退扣拒绝。
        feb = run(base)
        with self.assertRaisesRegex(ValueError, "no longer within"):
            run(continuation(feb["checkpoint"], "2026-05",
                             [chargeback("2026-05", 1, "2026-02", 1, 5)]))

    def test_expired_receipt_keeps_aggregate_unapplied_only(self):
        report = run({"lineage": self.lineage, "terms": self.terms,
                      "through_month": "2026-05",
                      "bank_rows": [receipt("2026-02", 1, "S1", 130)]})
        row = account_row(report, "2026-05", "S1")
        self.assertEqual((row["applied_cents"], row["unapplied_cents"]), (100, 30))
        self.assertEqual(report["checkpoint"]["receipts"], [])
        # 新到账不自动补用旧未用：无欠款时 7 分全部沉淀为新未用。
        nxt = run(continuation(report["checkpoint"], "2026-06",
                                [receipt("2026-06", 1, "S1", 7)]))
        row = account_row(nxt, "2026-06", "S1")
        self.assertEqual((row["applied_cents"], row["unapplied_cents"]), (100, 37))


class CollectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.lineage = {
            "from_month": "2026-01", "as_of_month": "2026-01",
            "assets": [{"id": "A", "category": "equipment", "cost_cents": 100,
                        "salvage_cents": 0, "life_months": 1,
                        "in_service_month": "2026-01", "disposal": None}],
            "events": [{"id": "S1", "month": "2026-01", "seq": 1, "kind": "sell",
                        "asset_id": "A", "proceeds_cents": 100}]}
        self.terms = [{"sale_id": "S1", "installments": [
            {"due_month": "2026-02", "amount_cents": 60},
            {"due_month": "2026-03", "amount_cents": 40}]}]

    def test_two_consecutive_month_ends_required_and_no_order_when_unchanged(self):
        report = run({"lineage": self.lineage, "terms": self.terms,
                      "through_month": "2026-06", "bank_rows": []})
        self.assertEqual(orders(report, "2026-02"), [])  # 到期当月不算逾期
        self.assertEqual(orders(report, "2026-03"), [])  # 首次观察
        self.assertEqual(orders(report, "2026-04"), [
            {"sale_id": "S1", "action": "open", "installments": [
                {"due_month": "2026-02", "open_cents": 60}]}])
        # 5 月第二期也满足连续两月末逾期：replace 整份清单。
        self.assertEqual(orders(report, "2026-05"), [
            {"sale_id": "S1", "action": "replace", "installments": [
                {"due_month": "2026-02", "open_cents": 60},
                {"due_month": "2026-03", "open_cents": 40}]}])
        # 清单完全相同则不发。
        self.assertEqual(orders(report, "2026-06"), [])

    def test_partial_payment_replaces_and_final_payment_withdraws(self):
        first = run({"lineage": self.lineage, "terms": self.terms,
                     "through_month": "2026-04", "bank_rows": []})
        may = run(continuation(first["checkpoint"], "2026-05", []))
        self.assertEqual(orders(may, "2026-05")[0]["action"], "replace")
        june = run(continuation(may["checkpoint"], "2026-06",
                                [receipt("2026-06", 1, "S1", 90)]))
        # 90 先清第一期 60，再清第二期 30，第二期仍欠 10。
        self.assertEqual(orders(june, "2026-06"), [
            {"sale_id": "S1", "action": "replace", "installments": [
                {"due_month": "2026-03", "open_cents": 10}]}])
        july = run(continuation(june["checkpoint"], "2026-07",
                                [receipt("2026-07", 1, "S1", 10)]))
        self.assertEqual(orders(july, "2026-07"), [
            {"sale_id": "S1", "action": "withdraw", "installments": []}])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.lineage = {
            "from_month": "2026-01", "as_of_month": "2026-01",
            "assets": [
                {"id": "A", "category": "equipment", "cost_cents": 100,
                 "salvage_cents": 0, "life_months": 1,
                 "in_service_month": "2026-01", "disposal": None},
                {"id": "B", "category": "tools", "cost_cents": 50,
                 "salvage_cents": 0, "life_months": 1,
                 "in_service_month": "2026-01", "disposal": None}],
            "events": [
                {"id": "S1", "month": "2026-01", "seq": 1, "kind": "sell",
                 "asset_id": "A", "proceeds_cents": 100},
                {"id": "S2", "month": "2026-01", "seq": 2, "kind": "sell",
                 "asset_id": "B", "proceeds_cents": 0}]}
        self.terms = [{"sale_id": "S1", "installments": [
            {"due_month": "2026-02", "amount_cents": 60},
            {"due_month": "2026-03", "amount_cents": 40}]}]

    def assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def test_zero_proceeds_sale_opens_no_receivable(self):
        report = run({"lineage": self.lineage, "terms": self.terms,
                      "through_month": "2026-02", "bank_rows": []})
        self.assertEqual([a["sale_id"] for a in
                          report["periods"][0]["accounts"]], ["S1"])

    def test_root_forms_are_mutually_exclusive(self):
        good = {"lineage": self.lineage, "terms": self.terms,
                "through_month": "2026-02", "bank_rows": []}
        bad = dict(good, checkpoint={})
        self.assert_rejected(bad, "initial or continuation")
        self.assert_rejected({"through_month": "2026-02", "bank_rows": []},
                             "initial or continuation")

    def test_terms_rules(self):
        base = {"lineage": self.lineage, "through_month": "2026-02",
                "bank_rows": []}
        # 与正所得出售不一对一。
        self.assert_rejected(dict(base, terms=self.terms + [{
            "sale_id": "S2",
            "installments": [{"due_month": "2026-02", "amount_cents": 1}]}]),
            "one-to-one")
        # 分期总额不等于本金。
        self.assert_rejected(dict(base, terms=[{"sale_id": "S1",
            "installments": [{"due_month": "2026-02", "amount_cents": 60},
                             {"due_month": "2026-03", "amount_cents": 39}]}]),
            "does not equal")
        # 到期月早于出售月。
        self.assert_rejected(dict(base, terms=[{"sale_id": "S1",
            "installments": [{"due_month": "2025-12",
                              "amount_cents": 100}]}]), "before sale month")
        # 同笔到期月重复 / 期数超限 / 金额非正。
        self.assert_rejected(dict(base, terms=[{"sale_id": "S1",
            "installments": [{"due_month": "2026-02", "amount_cents": 50},
                             {"due_month": "2026-02", "amount_cents": 50}]}]),
            "duplicate due_month")
        self.assert_rejected(
            {"lineage": self.lineage, "through_month": "2026-08",
             "bank_rows": [], "terms": [{"sale_id": "S1", "installments": [
                 {"due_month": f"2026-0{i}", "amount_cents": 1}
                 for i in range(2, 9)]}]}, "installments")
        self.assert_rejected(dict(base, terms=[{"sale_id": "S1",
            "installments": [{"due_month": "2026-02", "amount_cents": 0},
                             {"due_month": "2026-03", "amount_cents": 100}]}]),
            "amount_cents")

    def test_bank_row_rules(self):
        base = {"lineage": self.lineage, "terms": self.terms,
                "through_month": "2026-02"}
        self.assert_rejected(dict(base, bank_rows=[
            receipt("2026-02", 1, "GHOST", 10)]), "unknown sale")
        self.assert_rejected(dict(base, bank_rows=[
            receipt("2026-02", 1, "S1", 10), receipt("2026-02", 1, "S1", 5)]),
            "duplicate bank row")
        self.assert_rejected(dict(base, bank_rows=[
            receipt("2026-03", 1, "S1", 10)]), "outside batch")
        good = receipt("2026-02", 1, "S1", 10)
        bad = dict(good, note="x")
        self.assert_rejected(dict(base, bank_rows=[bad]), "invalid fields")
        self.assert_rejected(dict(base, bank_rows=[
            {"month": "2026-02", "seq": 1, "kind": "refund",
             "sale_id": "S1", "amount_cents": 10}]), "unknown bank row kind")
        for seq in (0, 1_000_001):
            self.assert_rejected(dict(base, bank_rows=[
                receipt("2026-02", seq, "S1", 10)]), "seq")
        for amount in (0, 10**12 + 1, -1, 1.5, True):
            self.assert_rejected(dict(base, bank_rows=[
                receipt("2026-02", 1, "S1", amount)]), "amount_cents")

    def test_per_month_cap_and_batch_span(self):
        rows = [receipt("2026-02", i, "S1", 1) for i in range(1, 242)]
        self.assert_rejected(
            {"lineage": self.lineage, "terms": self.terms,
             "through_month": "2026-02", "bank_rows": rows}, "per month")
        self.assert_rejected(
            {"lineage": self.lineage, "terms": self.terms,
             "through_month": "2027-02", "bank_rows": []}, "1..12")

    def test_lineage_failure_still_rejects_the_whole_input(self):
        # 出售一台不在册的设备：现有资产台账校验失败，本命令同样退出 2。
        broken = {
            "from_month": "2026-01", "as_of_month": "2026-01",
            "assets": [],
            "events": [{"id": "S1", "month": "2026-01", "seq": 1,
                        "kind": "sell", "asset_id": "GHOST",
                        "proceeds_cents": 100}]}
        self.assert_rejected(
            {"lineage": broken, "terms": self.terms,
             "through_month": "2026-02", "bank_rows": []}, "not on the books")


class CheckpointValidationTests(unittest.TestCase):
    def setUp(self):
        lineage = {
            "from_month": "2026-01", "as_of_month": "2026-01",
            "assets": [{"id": "A", "category": "equipment", "cost_cents": 100,
                        "salvage_cents": 0, "life_months": 1,
                        "in_service_month": "2026-01", "disposal": None}],
            "events": [{"id": "S1", "month": "2026-01", "seq": 1,
                        "kind": "sell", "asset_id": "A",
                        "proceeds_cents": 100}]}
        terms = [{"sale_id": "S1", "installments": [
            {"due_month": "2026-02", "amount_cents": 60},
            {"due_month": "2026-03", "amount_cents": 40}]}]
        self.checkpoint = run({"lineage": lineage, "terms": terms,
                               "through_month": "2026-03",
                               "bank_rows": [
                                   receipt("2026-02", 1, "S1", 80),
                                   chargeback("2026-03", 1, "2026-02", 1, 30)]}
                              )["checkpoint"]

    def assert_rejected(self, checkpoint, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(continuation(checkpoint, "2026-04", []))

    def test_well_formed_checkpoint_continues(self):
        report = run(continuation(self.checkpoint, "2026-04", []))
        self.assertEqual(report["checkpoint"]["format_version"], FORMAT_VERSION)
        self.assertEqual(report["through_month"], "2026-04")

    def test_contradictory_checkpoints_rejected(self):
        import copy
        cp = self.checkpoint
        bad = copy.deepcopy(cp); bad["format_version"] = 9
        self.assert_rejected(bad, "format_version")
        bad = copy.deepcopy(cp); bad["extra"] = 1
        self.assert_rejected(bad, "invalid fields")
        bad = copy.deepcopy(cp); bad["accounts"][0]["applied_cents"] += 1
        self.assert_rejected(bad, "net receipt mismatch")
        bad = copy.deepcopy(cp)
        bad["accounts"][0]["installments"][0]["open_cents"] += 1
        self.assert_rejected(bad, "applied plus outstanding")
        bad = copy.deepcopy(cp)
        bad["accounts"][0]["installments"][0]["open_cents"] = 61
        self.assert_rejected(bad, "exceeds its principal")
        bad = copy.deepcopy(cp)
        bad["accounts"][0]["installments"][0]["principal_cents"] = 61
        bad["accounts"][0]["installments"][1]["principal_cents"] = 40
        self.assert_rejected(bad, "do not sum to principal")
        bad = copy.deepcopy(cp)
        bad["accounts"][0]["gross_chargeback_cents"] = \
            bad["accounts"][0]["gross_received_cents"] + 1
        self.assert_rejected(bad, "chargeback exceeds")
        bad = copy.deepcopy(cp); bad["receipts"][0]["unapplied_cents"] += 1
        self.assert_rejected(bad, "parts do not sum")
        bad = copy.deepcopy(cp)
        bad["receipts"][0]["chargeback_cents"] = bad["receipts"][0]["amount_cents"] + 1
        self.assert_rejected(bad, "chargeback exceeds its amount")
        bad = copy.deepcopy(cp)
        bad["receipts"].append(copy.deepcopy(bad["receipts"][0]))
        bad["receipts"][1]["seq"] = 2
        self.assert_rejected(bad, "exceeds account total")
        bad = copy.deepcopy(cp); bad["receipts"][0]["month"] = "2025-12"
        self.assert_rejected(bad, "past its chargeback window")
        bad = copy.deepcopy(cp); bad["receipts"][0]["sale_id"] = "GHOST"
        self.assert_rejected(bad, "unknown sale")
        bad = copy.deepcopy(cp)
        bad["accounts"][0]["prev_overdue"] = {"2026-02": 999}
        self.assert_rejected(bad, "prev_overdue exceeds")
        bad = copy.deepcopy(cp)
        bad["accounts"][0]["mandate"] = {"installments": [
            {"due_month": "2026-02", "open_cents": 999}]}
        self.assert_rejected(bad, "mandate amount exceeds")
        bad = copy.deepcopy(cp)
        bad["accounts"][0]["mandate"] = {"installments": [
            {"due_month": "2026-09", "open_cents": 1}]}
        self.assert_rejected(bad, "unknown installment")
        bad = copy.deepcopy(cp); bad["accounts"][0]["principal_cents"] = True
        self.assert_rejected(bad, "principal_cents")

    def test_checkpoint_carries_no_lineage_or_raw_bank_rows(self):
        self.assertEqual(set(self.checkpoint),
                         {"format_version", "through_month", "accounts",
                          "receipts"})
        for account in self.checkpoint["accounts"]:
            self.assertNotIn("lineage", account)
        # 交接包里的到账只含余额所需字段，不含原始银行行的 kind/month-seq 之外
        # 的业务凭证原样归档。
        for receipt in self.checkpoint["receipts"]:
            self.assertEqual(set(receipt),
                             {"month", "seq", "sale_id", "amount_cents",
                              "chargeback_cents", "unapplied_cents",
                              "applications"})


class FixtureTests(unittest.TestCase):
    def test_fixture_matches_hand_written_expected_report(self):
        source = json.loads(
            (ROOT / "fixtures" / "sale_collections_input.json").read_text())
        expected = json.loads(
            (ROOT / "fixtures" / "sale_collections_expected.json").read_text())
        self.assertEqual(run(source), expected)

    def test_expected_report_is_internally_consistent(self):
        report = json.loads(
            (ROOT / "fixtures" / "sale_collections_expected.json").read_text())
        self.assertEqual(set(report), {"through_month", "periods", "checkpoint"})
        for period in report["periods"]:
            self.assertEqual(set(period), {"month", "accounts",
                                           "collection_orders"})
            sale_ids = [row["sale_id"] for row in period["accounts"]]
            self.assertEqual(sale_ids, sorted(sale_ids))
            for row in period["accounts"]:
                self.assertEqual(
                    row["received_net_cents"],
                    row["applied_cents"] + row["unapplied_cents"])
                self.assertEqual(
                    row["outstanding_cents"],
                    sum(i["open_cents"] for i in row["installments"]))
                due = [i["due_month"] for i in row["installments"]]
                self.assertEqual(due, sorted(due))
            order_ids = [o["sale_id"] for o in period["collection_orders"]]
            self.assertEqual(order_ids, sorted(order_ids))
        # 交接包只保留窗口内到账（4 月到账在 6 月末后不应出现）。
        for receipt in report["checkpoint"]["receipts"]:
            self.assertNotIn(receipt["month"], ("2026-04",))


class CliTests(unittest.TestCase):
    def run_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m",
                               "asset_sale_collections.cli",
                               "--source", str(source), "--output", str(output)],
                              cwd=ROOT, env=env, capture_output=True, text=True)

    def test_cli_success_atomic_output_and_existing_target_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text((ROOT / "fixtures" /
                               "sale_collections_input.json").read_text())
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(set(report),
                             {"through_month", "periods", "checkpoint"})
            stale = output.read_text()
            again = self.run_cli(source, output)
            self.assertEqual(again.returncode, 2)
            self.assertIn("asset-sale-collections", again.stderr)
            self.assertEqual(output.read_text(), stale)
            self.assertEqual(list(Path(tmp).glob(".report-*")), [])

    def test_cli_invalid_input_exit_2_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.json"
            output = Path(tmp) / "out.json"
            source.write_text(json.dumps({"through_month": "2026-02",
                                          "bank_rows": []}))
            result = self.run_cli(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_cli_rejects_oversized_nan_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.json"
            big.write_text('{"lineage":{},"terms":[],"through_month":"2026-02",'
                           '"bank_rows":' + " " * (8 * 1024 * 1024) + "[]}")
            self.assertEqual(self.run_cli(big, Path(tmp) / "o.json").returncode, 2)
            nan = Path(tmp) / "nan.json"
            nan.write_text('{"through_month":"2026-02","bank_rows":[],"checkpoint":'
                           '{"format_version":NaN,"through_month":"2026-01",'
                           '"accounts":[],"receipts":[]}}')
            self.assertEqual(self.run_cli(nan, Path(tmp) / "o2.json").returncode, 2)
            dup = Path(tmp) / "dup.json"
            dup.write_text('{"through_month":"2026-02","through_month":"2026-02",'
                           '"bank_rows":[]}')
            self.assertEqual(self.run_cli(dup, Path(tmp) / "o3.json").returncode, 2)


if __name__ == "__main__":
    unittest.main()
