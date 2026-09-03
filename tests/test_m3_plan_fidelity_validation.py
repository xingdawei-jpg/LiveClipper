import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from run_m3_plan_fidelity_validation import _load_sidecar  # noqa: E402


class M3PlanFidelityValidationTests(unittest.TestCase):
    def test_missing_sidecar_is_a_word_lineage_blocker(self) -> None:
        segments, status = _load_sidecar(ROOT / "workspace" / "not-a-source.words.json")

        self.assertEqual(segments, [])
        self.assertEqual(status, "missing_exact_source_sidecar")


if __name__ == "__main__":
    unittest.main()
