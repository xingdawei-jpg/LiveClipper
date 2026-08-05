from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import smart_crop
import cutter_logic


class SmartCropBottomPreservationTests(unittest.TestCase):
    @staticmethod
    def _person(head_top: float) -> dict[str, float]:
        return {
            "person_cx_ratio": 0.5,
            "person_cy_ratio": 0.5,
            "person_size_ratio": 0.6,
            "person_h_ratio": 0.8,
            "person_bottom_ratio": 0.95,
            "head_top_ratio": head_top,
        }

    def test_person_crop_keeps_bottom_for_every_strength(self) -> None:
        for level in ("light", "medium", "heavy"):
            with self.subTest(level=level), mock.patch.object(
                smart_crop.random, "uniform", side_effect=lambda _low, high: high
            ):
                crop = smart_crop.compute_smart_crop(self._person(0.2), 1080, 1920, crop_level=level)

            self.assertAlmostEqual(crop["crop_y"] + crop["crop_h"], 1.0, places=9)

    def test_head_near_top_reduces_zoom_instead_of_cropping_bottom(self) -> None:
        with mock.patch.object(smart_crop.random, "uniform", side_effect=lambda _low, high: high):
            crop = smart_crop.compute_smart_crop(self._person(0.03), 1080, 1920, crop_level="heavy")

        self.assertAlmostEqual(crop["crop_w"], 1.0, places=9)
        self.assertAlmostEqual(crop["crop_h"], 1.0, places=9)
        self.assertAlmostEqual(crop["crop_y"], 0.0, places=9)

    def test_head_at_exact_top_is_not_treated_as_missing_detection(self) -> None:
        with mock.patch.object(smart_crop.random, "uniform", side_effect=lambda _low, high: high):
            crop = smart_crop.compute_smart_crop(self._person(0.0), 1080, 1920, crop_level="heavy")

        self.assertAlmostEqual(crop["crop_w"], 1.0, places=9)
        self.assertAlmostEqual(crop["crop_h"], 1.0, places=9)
        self.assertAlmostEqual(crop["crop_y"], 0.0, places=9)
        self.assertEqual(cutter_logic._kb_quality_cap_for_zoom(1.0, crop), 0.0)

    def test_detected_person_keeps_head_margin_and_logs_bottom_lock(self) -> None:
        messages: list[str] = []
        with mock.patch.object(smart_crop.random, "uniform", side_effect=lambda _low, high: high):
            crop = smart_crop.compute_smart_crop(
                self._person(0.1),
                1080,
                1920,
                crop_level="medium",
                log_fn=messages.append,
            )

        visible_headroom = (0.1 - crop["crop_y"]) / crop["crop_h"]
        self.assertGreaterEqual(visible_headroom, smart_crop.HEADROOM_RATIO - 1e-9)
        self.assertAlmostEqual(crop["crop_y"] + crop["crop_h"], 1.0, places=9)
        self.assertTrue(any("bottom-locked" in message for message in messages))

    def test_fallback_crop_also_keeps_bottom(self) -> None:
        with mock.patch.object(smart_crop.random, "uniform", side_effect=lambda _low, high: high):
            crop = smart_crop.compute_smart_crop(None, 1080, 1920, crop_level="heavy")

        # 无人检测时使用极轻缩放（1.02-1.05x）而非完全静止
        self.assertAlmostEqual(crop["zoom"], 1.05, places=3)
        self.assertAlmostEqual(crop["crop_y"] + crop["crop_h"], 1.0, places=9)

    def test_detector_box_top_is_expanded_above_the_hairline(self) -> None:
        detection = (100, 192, 400, 960, 0.9, "body")

        head_top = smart_crop._estimated_head_top_ratio(detection, 1920)

        self.assertAlmostEqual(head_top, 0.05, places=6)

    def test_hog_scalar_weight_is_supported(self) -> None:
        frame = smart_crop.np.zeros((200, 100, 3), dtype=smart_crop.np.uint8)
        hog = mock.Mock()
        hog.detectMultiScale.return_value = ([(10, 20, 30, 80)], smart_crop.np.array([0.9]))

        with mock.patch.object(smart_crop, "_get_hog", return_value=hog):
            detections = smart_crop._detect_persons(frame)

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0][-1], "body")
        self.assertGreater(detections[0][4], 0.3)

    def test_batch_detection_uses_five_samples_and_supported_subject_envelope(self) -> None:
        detections = [
            [(100, 300, 400, 1300, 0.9, "body")],
            [(100, 250, 400, 1400, 0.9, "body")],
            [(100, 200, 400, 1200, 0.9, "body")],
            [(100, 220, 400, 1650, 0.9, "body")],
            [(100, 260, 400, 1400, 0.9, "body")],
        ]
        extracted_frame = mock.Mock()
        extracted_frame.shape = (1920, 1080, 3)
        with mock.patch.object(smart_crop, "_CV2_AVAILABLE", True), mock.patch.object(
            smart_crop, "prepare_face_detector"
        ), mock.patch.object(
            smart_crop, "_extract_frame_ffmpeg", return_value=extracted_frame
        ) as extract_frame, mock.patch.object(
            smart_crop, "_detect_persons", side_effect=detections
        ):
            result = smart_crop.batch_detect_clips(
                "source.mp4",
                [("Product", "text", 0.0, 10.0)],
                ffmpeg_cmd="ffmpeg",
                frame_w=1080,
                frame_h=1920,
            )[0]

        self.assertEqual(extract_frame.call_count, 5)
        # One isolated edge box is ignored; two observations are still required
        # before the crop or secondary zoom is constrained.
        self.assertAlmostEqual(result["head_top_ratio"], (200 / 1920) - ((1200 / 1920) * 0.10), places=6)
        self.assertAlmostEqual(result["person_bottom_ratio"], (260 + 1400) / 1920, places=6)

    def test_batch_detection_ignores_one_false_top_edge_box(self) -> None:
        detections = [
            [(100, 10, 400, 1200, 0.9, "body")],
            [(100, 400, 400, 1200, 0.9, "body")],
            [(100, 420, 400, 1200, 0.9, "body")],
            [(100, 410, 400, 1200, 0.9, "body")],
            [(100, 430, 400, 1200, 0.9, "body")],
        ]
        extracted_frame = mock.Mock()
        extracted_frame.shape = (1920, 1080, 3)
        with mock.patch.object(smart_crop, "_CV2_AVAILABLE", True), mock.patch.object(
            smart_crop, "prepare_face_detector"
        ), mock.patch.object(
            smart_crop, "_extract_frame_ffmpeg", return_value=extracted_frame
        ), mock.patch.object(
            smart_crop, "_detect_persons", side_effect=detections
        ):
            result = smart_crop.batch_detect_clips(
                "source.mp4",
                [("Product", "text", 0.0, 10.0)],
                ffmpeg_cmd="ffmpeg",
                frame_w=1080,
                frame_h=1920,
            )[0]

        self.assertGreater(result["head_top_ratio"], 0.14)

    def test_ken_burns_is_disabled_when_subject_touches_an_edge(self) -> None:
        crop = {
            "method": "smart",
            "crop_h": 0.95,
            "crop_y": 0.05,
            "subject_head_top_ratio": 0.07,
            "subject_bottom_ratio": 0.98,
        }

        cap = cutter_logic._kb_quality_cap_for_zoom(1 / 0.95, crop)

        self.assertEqual(cap, 0.0)

    def test_ken_burns_cap_uses_remaining_subject_clearance(self) -> None:
        crop = {
            "method": "smart",
            "crop_h": 0.95,
            "crop_y": 0.05,
            "subject_head_top_ratio": 0.15,
            "subject_bottom_ratio": 0.85,
        }

        cap = cutter_logic._kb_quality_cap_for_zoom(1 / 0.95, crop)

        self.assertGreater(cap, 0.0)
        self.assertLessEqual(cap, 0.10)

    def test_ken_burns_does_not_consume_reserved_hair_headroom(self) -> None:
        crop = {
            "method": "smart",
            "crop_h": 0.94,
            "crop_y": 0.06,
            "subject_head_top_ratio": 0.12,
            "subject_bottom_ratio": 0.90,
        }

        cap = cutter_logic._kb_quality_cap_for_zoom(1 / 0.94, crop)

        self.assertEqual(cap, 0.0)

    def test_zero_ken_burns_cap_is_not_raised_to_two_percent(self) -> None:
        with mock.patch.object(smart_crop.random, "choice", return_value="in"), mock.patch.object(
            smart_crop.random, "uniform", return_value=0.25
        ):
            _direction, delta = smart_crop._ken_burns_motion("heavy", max_zoom_delta=0.0)

        self.assertEqual(delta, 0.0)

    def test_ffmpeg_even_pixel_rounding_keeps_the_last_source_row(self) -> None:
        crop = {
            "method": "smart",
            "crop_w": 1715 / 1920,
            "crop_h": 1715 / 1920,
            "crop_x": 0.0,
            "crop_y": 205 / 1920,
        }

        video_filter = cutter_logic._smart_crop_vf(crop, 1080, 1920, 1080, 1920, smart_crop._even)
        crop_filter = video_filter.split(",", 1)[0]
        _name, width, height, x_pos, y_pos = crop_filter.replace("=", ":").split(":")

        self.assertEqual(int(y_pos) + int(height), 1920)


if __name__ == "__main__":
    unittest.main()
