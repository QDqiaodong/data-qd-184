import json
import unittest
from pathlib import Path
from asset_schedule.service import run


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class AssetTests(unittest.TestCase):
    def test_expense_disposal_and_future_are_consistent(self):
        result = run(fixture())
        self.assertEqual(result["summary"], {"assets": 3, "disposed": 1,
                         "closing_book_cents": 4000, "cumulative_depreciation_cents": 9000,
                         "disposal_gain_loss_cents": 500})
        camera, future, printer = result["assets"]
        self.assertEqual(camera["monthly"][-1]["closing_book_cents"], 4000)
        self.assertEqual(future["status"], "not_in_service")
        self.assertEqual(future["monthly"], [])
        self.assertEqual(printer["disposal_result"]["carrying_cents"], 2000)
        self.assertEqual([r["month"] for r in printer["monthly"]], ["2026-02", "2026-03"])

    def test_fractional_cents_conserve_exact_full_life(self):
        data = fixture()
        data["assets"] = [dict(data["assets"][0], cost_cents=100, salvage_cents=0, life_months=3)]
        rows = run(data)["assets"][0]["monthly"]
        self.assertEqual([row["depreciation_cents"] for row in rows], [33, 33, 34])
        self.assertEqual(sum(row["depreciation_cents"] for row in rows), 100)

    def test_disposal_in_service_month_has_no_depreciation(self):
        data = fixture()
        data["assets"] = [dict(data["assets"][0], disposal={"month": "2026-01", "proceeds_cents": 8000})]
        result = run(data)["assets"][0]
        self.assertEqual(result["monthly"], [])
        self.assertEqual(result["disposal_result"]["gain_loss_cents"], -2000)
        self.assertEqual(result["closing_book_cents"], 0)

    def test_future_disposal_does_not_clear_current_asset(self):
        data = fixture()
        data["assets"] = [dict(data["assets"][0], disposal={"month": "2026-08", "proceeds_cents": 1000})]
        result = run(data)["assets"][0]
        self.assertEqual(result["status"], "in_service")
        self.assertIsNone(result["disposal_result"])
        self.assertEqual(result["closing_book_cents"], 4000)

    def test_residual_is_retained_after_life(self):
        data = fixture()
        data["as_of_month"] = "2027-01"
        data["assets"] = [data["assets"][0]]
        asset = run(data)["assets"][0]
        self.assertEqual(asset["status"], "fully_depreciated")
        self.assertEqual(asset["closing_book_cents"], 1000)
        self.assertEqual(len(asset["monthly"]), 6)

    def test_bad_residual_and_early_disposal_reject(self):
        data = fixture()
        data["assets"][0]["salvage_cents"] = 11000
        with self.assertRaisesRegex(ValueError, "salvage"):
            run(data)
        data = fixture()
        data["assets"][1]["disposal"]["month"] = "2026-01"
        with self.assertRaisesRegex(ValueError, "precedes"):
            run(data)
