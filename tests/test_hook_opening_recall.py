import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from hook_opening_recall import (  # noqa: E402
    assemble_opening_packages,
    build_hook_recall_batches,
    parse_hook_recall,
)


def _source_rows():
    return [{
        "subtitle_id": 1, "start": 0.0, "end": 2.6,
        "text": "穿上以后，正面整个人看起来特别窄。",
        "hard_safe": True, "materializable": True,
        "word_lineage": {"word_start_index": 0, "word_end_index": 5},
        "word_tokens": [
            {"offset": 0, "text": "穿上", "start": 0.0, "end": 0.3},
            {"offset": 1, "text": "以后", "start": 0.35, "end": 0.65},
            {"offset": 2, "text": "正面", "start": 0.7, "end": 1.0},
            {"offset": 3, "text": "整个人", "start": 1.05, "end": 1.35},
            {"offset": 4, "text": "看起来", "start": 1.4, "end": 1.75},
            {"offset": 5, "text": "特别窄", "start": 1.8, "end": 2.6},
        ],
    }, {
        "subtitle_id": 2, "start": 2.7, "end": 5.5,
        "text": "黑色花边线会把人的肩往里压。",
        "hard_safe": True, "materializable": True,
        "word_lineage": {"word_start_index": 6, "word_end_index": 11},
        "word_tokens": [
            {"offset": 0, "text": "黑色", "start": 2.7, "end": 3.0},
            {"offset": 1, "text": "花边线", "start": 3.05, "end": 3.45},
            {"offset": 2, "text": "会", "start": 3.5, "end": 3.65},
            {"offset": 3, "text": "把人的", "start": 3.7, "end": 4.0},
            {"offset": 4, "text": "肩", "start": 4.05, "end": 4.2},
            {"offset": 5, "text": "往里压", "start": 4.25, "end": 5.5},
        ],
    }]


class HookOpeningRecallTests(unittest.TestCase):
    def test_exact_clean_hook_survives_specialized_recall(self):
        rows = _source_rows()
        batch = build_hook_recall_batches(rows)[0]
        result = parse_hook_recall(responses=[(batch, {"hook_candidates": [{
            "hook_id": "local-1", "start_word_id": 0, "end_word_id": 5,
            "final_text": "穿上以后，正面整个人看起来特别窄。",
            "hook_type": "strong_result", "hook_evidence_function": "result",
            "core_purchase_value": "正面显窄", "stop_reason": "结果明确", "standalone_reason": "主谓完整",
            "specificity_reason": "正面变窄", "hook_strength": 5,
            "publishable_state": "publishable_clean", "visual_dependency": "none",
            "eligible_for_opening": True, "reject_reason": "",
        }], "hook_rejects": []})], source_rows=rows)
        self.assertEqual(result["status"], "hook_recall_completed")
        self.assertEqual(result["hook_candidate_count"], 1)
        hook = result["hook_candidates"][0]
        self.assertEqual(hook["hook_id"], "H001")
        self.assertTrue(hook["eligible_for_opening"])
        self.assertEqual(hook["word_lineage"][0]["word_start_index"], 0)

    def test_visual_or_sub_two_second_hook_is_not_promoted(self):
        rows = _source_rows()
        batch = build_hook_recall_batches(rows)[0]
        result = parse_hook_recall(responses=[(batch, {"hook_candidates": [{
            "hook_id": "local-1", "start_word_id": 0, "end_word_id": 1,
            "final_text": "穿上以后，", "hook_type": "strong_result", "hook_evidence_function": "result",
            "core_purchase_value": "显瘦", "stop_reason": "", "standalone_reason": "", "specificity_reason": "",
            "hook_strength": 5, "publishable_state": "publishable_clean", "visual_dependency": "required",
            "eligible_for_opening": True, "reject_reason": "",
        }], "hook_rejects": []})], source_rows=rows)
        self.assertEqual(result["status"], "hook_material_limited")
        self.assertEqual(result["hook_reject_audit_top_20"][0]["reject_reason"], "hook_duration_below_2_seconds")

    def test_opening_package_uses_frozen_actor_payoff_without_repeating_hook_words(self):
        hooks = [{
            "hook_id": "H001", "start": 0.0, "end": 2.6, "duration": 2.6,
            "text": "穿上以后，正面整个人看起来特别窄。", "start_word_id": 0, "end_word_id": 5,
            "hook_type": "strong_result", "core_purchase_value": "正面显窄", "hook_strength": 5,
        }]
        actors = [{
            "beat_id": "B010", "duration_seconds": 2.8,
            "text": "黑色花边线会把人的肩往里压。", "publishability_status": "publishable_clean",
            "visual_dependency": "none", "role_permissions": ["proof", "support"],
            "context_requirement": "standalone", "purchase_value": "肩部内收", "sub_outcome": "肩显窄",
            "evidence_function": "mechanism", "final_start_word_id": 6, "final_end_word_id": 11,
        }]
        # Patch the model call boundary by supplying its already-AI-authored
        # JSON result; parser validation remains the thing under test.
        from unittest.mock import patch
        with patch("hook_opening_recall._post_lite_request", return_value={"choices": [{"message": {"content": """{
          \"opening_packages\":[{\"opening_id\":\"O001\",\"hook_id\":\"H001\",\"payoff_beat_ids\":[\"B010\"],\"opening_promise\":\"正面显窄\",\"sequence\":[{\"beat_id\":\"H001\",\"role\":\"hook\",\"new_information\":\"先给出显窄结果\"},{\"beat_id\":\"B010\",\"role\":\"mechanism\",\"new_information\":\"解释肩线内收原因\"}],\"progression_count\":2,\"payoff_relation\":\"肩线机制兑现显窄\",\"why_viewer_keeps_watching\":\"结果立刻有原因\",\"hook_stop_power\":5,\"hook_independence\":5,\"hook_specificity\":5,\"hook_product_relevance\":5,\"payoff_strength\":5,\"payoff_immediacy\":5,\"hook_payoff_consistency\":5,\"quality\":\"medium\",\"reject_reason\":\"\"}],\"opening_rejects\":[]
        }"""}}]}):
            result = assemble_opening_packages(
                hook_recall={"hook_candidates": hooks}, frozen_actor_pool=actors,
                api_key="test", base_url="https://example.invalid", model="test", opening_promise="显瘦",
            )
        self.assertEqual(result["status"], "opening_packages_completed")
        package = result["opening_packages"][0]
        self.assertEqual(package["payoff_beat_ids"], ["B010"])
        self.assertEqual(package["total_duration"], 5.4)


if __name__ == "__main__":
    unittest.main()
