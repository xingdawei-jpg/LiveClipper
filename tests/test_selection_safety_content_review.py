from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


content_review = importlib.import_module("content_review")


class ContentReviewSafetyGateTests(unittest.TestCase):
    def test_personal_size_reply_cannot_enter_review_cards_or_final_review(self) -> None:
        text = "[V1] 158的女孩子是S。但是我跟你说，我1.7米。"

        self.assertFalse(content_review._reviewable_candidate_text(text))
        issues = content_review._final_objective_issues([
            {"clip_type": "hook", "duration_sec": 4.0, "text": text},
        ])
        self.assertTrue(any("个人尺码答复" in issue for issue in issues))

    def test_size_statement_is_not_a_review_hook(self) -> None:
        text = "衣长158厘米，S码肩宽合适。"

        self.assertTrue(content_review._reviewable_candidate_text(text))
        self.assertFalse(content_review._reviewable_hook_text(text))
        issues = content_review._final_objective_issues([
            {"clip_type": "hook", "duration_sec": 3.0, "text": text},
        ])
        self.assertTrue(any("尺码信息不可作Hook" in issue for issue in issues))

    def test_live_inventory_question_cannot_enter_content_review(self) -> None:
        self.assertFalse(
            content_review._reviewable_candidate_text(
                "粉色粉色粉色什么粉色姐妹，我们有粉色的衣服吗？今天没有吧。"
            )
        )

    def test_context_dependent_or_generic_intensity_cannot_enter_hook_pairs(self) -> None:
        self.assertFalse(
            content_review._reviewable_hook_text(
                "\u5450\uff0c\u548c\u7eb8\u7eb1\u3002\u7136\u540e\u8fd9\u79cd\u548c\u7eb8\u7eb1\uff0c\u5b83\u7684\u6574\u4e2a\u624b\u611f\u5f88\u597d\u3002"
            )
        )
        self.assertFalse(
            content_review._reviewable_hook_text(
                "\u975e\u5e38\u975e\u5e38\u72e0\uff0c\u800c\u4e14\u9762\u6599\u4e5f\u4e0d\u5bb9\u6613\u52fe\u4e1d\u3002"
            )
        )


if __name__ == "__main__":
    unittest.main()
