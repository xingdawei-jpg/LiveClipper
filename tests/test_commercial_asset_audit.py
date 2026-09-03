from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from candidate_ledger import CandidateLedger  # noqa: E402
from commercial_asset_audit import classify_commercial_asset  # noqa: E402


def _candidate(candidate_id: int, text: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "source": "V1",
        "start": float(candidate_id),
        "end": float(candidate_id) + 2.0,
        "text": text,
    }


class CommercialAssetAuditTests(unittest.TestCase):
    def test_styling_scene_is_kept_as_supporting_not_filtered(self) -> None:
        asset = classify_commercial_asset(_candidate(1, "这件衬衫搭牛仔裤特别好看。"))

        self.assertEqual(asset.subject_context.product_focus, "related_product")
        self.assertEqual(asset.asset_role, "styling_scene")
        self.assertEqual(asset.story_permission, "supporting_story")

    def test_lifestyle_and_wearing_effect_remain_story_assets(self) -> None:
        lifestyle = classify_commercial_asset(_candidate(1, "早秋出去玩穿着也很舒服。"))
        effect = classify_commercial_asset(_candidate(2, "微胖女生上身很显瘦。"))

        self.assertEqual(lifestyle.asset_role, "lifestyle_scene")
        self.assertEqual(lifestyle.story_permission, "supporting_story")
        self.assertEqual(effect.asset_role, "wearing_effect")
        self.assertEqual(effect.story_permission, "main_story")

    def test_fit_safety_and_academy_scene_are_not_misclassified_as_other_product(self) -> None:
        fit = classify_commercial_asset(_candidate(1, "大货的裙长比我身上这个长两公分。"))
        academy = classify_commercial_asset(_candidate(2, "学院风开学穿出去很合适。"))

        self.assertEqual(fit.asset_role, "product_proof")
        self.assertEqual(fit.story_permission, "main_story")
        self.assertEqual(academy.asset_role, "lifestyle_scene")
        self.assertEqual(academy.story_permission, "supporting_story")

    def test_explicit_other_product_conversion_is_unavailable(self) -> None:
        asset = classify_commercial_asset(_candidate(1, "这条裤子今天卖爆了，喜欢裤子的拍链接。"))

        self.assertEqual(asset.story_permission, "unavailable")
        self.assertEqual(asset.subject_context.product_focus, "related_product")

    def test_ledger_annotation_never_removes_or_creates_a_candidate(self) -> None:
        ledger = CandidateLedger()
        ledger.seed("semantic", [_candidate(1, "这件衬衫搭牛仔裤特别好看。")])
        ledger.transition("hard_safe", [_candidate(1, "这件衬衫搭牛仔裤特别好看。")], reason_code="test")
        asset = classify_commercial_asset(_candidate(1, "这件衬衫搭牛仔裤特别好看。"))

        ledger.annotate_commercial_assets("asset_annotation", [asset])

        self.assertEqual(ledger.to_dict()["stages"][-1]["active_count"], 1)
        self.assertEqual(ledger.commercial_assets()[1].story_permission, "supporting_story")
        with self.assertRaisesRegex(ValueError, "absent from the active ledger"):
            ledger.annotate_commercial_assets("asset_annotation", [
                classify_commercial_asset(_candidate(2, "早秋通勤也能穿。")),
            ])


if __name__ == "__main__":
    unittest.main()
