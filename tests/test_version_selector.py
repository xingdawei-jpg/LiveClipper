import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commercial_analyzer import Strategy, EvidenceItem
from version_selector import compute_overlap_breakdown, compute_pairwise_overlap, select_versions


def _strategy(sid, family, angle, evidence_ids, story=0.8, material=0.9, contract=1.0):
    evidence = tuple(EvidenceItem("proof", f"证据{sid}", (i,)) for i in evidence_ids)
    return Strategy(
        strategy_id=sid,
        type="problem_solver",
        strategy_family=family,
        sub_angle=angle,
        thesis=f"thesis-{sid}",
        target_user="",
        evidence=evidence,
        missing_roles=(),
        blocked_evidence_types=(),
        contract_audit_hits=(),
        coherence_reason="",
        distinctiveness="high",
        story_strength=story,
        material_sufficiency=material,
        contract_compatibility=contract,
        strategy_viability="recommended",
    )


class OverlapTests(unittest.TestCase):
    def test_same_family_same_angle_is_high_overlap(self) -> None:
        a = _strategy("S1", "body_shaping", "waist_hip", [1, 2, 3])
        b = _strategy("S2", "body_shaping", "waist_hip", [1, 2, 4])
        self.assertGreaterEqual(compute_pairwise_overlap(a, b), 0.6)

    def test_breakdown_exposes_components(self) -> None:
        a = _strategy("S1", "body_shaping", "waist_hip", [1, 2, 3])
        b = _strategy("S2", "body_shaping", "upper_body", [10, 11, 12])
        bd = compute_overlap_breakdown(a, b)
        self.assertEqual(bd.family_overlap, 0.4)
        self.assertEqual(bd.angle_overlap, 0.0)  # 不同 angle
        self.assertEqual(bd.evidence_overlap, 0.0)  # 无共享证据
        self.assertEqual(bd.total, 0.4)


class SelectVersionsTests(unittest.TestCase):
    def test_same_family_dedup_keeps_only_one_version(self) -> None:
        # 简致衬衫场景：S1/S2 同属 body_shaping，S2 应被跳过，S3/S4 保留
        s1 = _strategy("S1", "body_shaping", "waist_hip", [1, 2, 3], story=0.83)
        s2 = _strategy("S2", "body_shaping", "upper_body", [10, 11, 12], story=0.79)
        s3 = _strategy("S3", "versatility", "early_autumn", [20, 21, 22], story=0.74)
        s4 = _strategy("S4", "quality", "fabric", [30, 31, 32], story=0.81)
        sel = select_versions([s1, s2, s3, s4], n=3)
        self.assertEqual([v.strategy_id for v in sel.selected], ["S1", "S4", "S3"])
        self.assertEqual(len(sel.skipped), 1)
        self.assertEqual(sel.skipped[0].strategy_id, "S2")
        self.assertEqual(sel.skipped[0].skip_reason, "same_family")
        self.assertEqual(sel.skipped[0].overlap_with, "S1")

    def test_three_distinct_families_kept(self) -> None:
        s1 = _strategy("S1", "body_shaping", "waist_hip", [1, 2, 3], story=0.88)
        s2 = _strategy("S2", "lifestyle", "early_autumn", [5, 6, 7], story=0.80)
        s3 = _strategy("S3", "quality_trust", "fabric", [9, 10, 11], story=0.75)
        sel = select_versions([s1, s2, s3], n=3)
        self.assertEqual([v.version_type for v in sel.selected], ["strategy", "strategy", "strategy"])
        self.assertEqual({v.strategy_id for v in sel.selected}, {"S1", "S2", "S3"})
        self.assertEqual(len(sel.skipped), 0)

    def test_variant_fill_when_short(self) -> None:
        s1 = _strategy("S1", "body_shaping", "waist_hip", [1, 2, 3], story=0.88)
        s2 = _strategy("S2", "lifestyle", "early_autumn", [5, 6, 7], story=0.80)
        sel = select_versions([s1, s2], n=4)
        types = [v.version_type for v in sel.selected]
        self.assertEqual(types, ["strategy", "strategy", "variant", "variant"])

    def test_ranked_by_base_score(self) -> None:
        weak = _strategy("S_weak", "body_shaping", "waist_hip", [1], story=0.3, material=0.5, contract=1.0)
        strong = _strategy("S_strong", "lifestyle", "early_autumn", [5], story=0.9, material=0.9, contract=1.0)
        sel = select_versions([weak, strong], n=2)
        self.assertEqual(sel.selected[0].strategy_id, "S_strong")


if __name__ == "__main__":
    unittest.main()
