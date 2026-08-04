from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web_client"))

import server


class OutputNamingTests(unittest.TestCase):
    def test_payloads_default_to_timestamp_and_accept_source_names(self) -> None:
        self.assertEqual(server.SmartCutPayload().output_naming_mode, "source_timestamp")
        self.assertEqual(server.MixPayload().output_naming_mode, "source_timestamp")
        self.assertEqual(server.SmartCutPayload(output_naming_mode="source").output_naming_mode, "source")
        self.assertEqual(server.MixPayload(output_naming_mode="source").output_naming_mode, "source")

    def test_smart_output_path_supports_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            server, "_stamp_name", return_value="20260730_153000"
        ):
            out_dir = Path(tmp)
            video = Path("D:/素材/夏季衬衫.mp4")

            source_name = server._smart_output_path(out_dir, video, "source")
            timestamp_name = server._smart_output_path(out_dir, video, "source_timestamp")

        self.assertEqual(source_name.name, "夏季衬衫.mp4")
        self.assertEqual(timestamp_name.name, "夏季衬衫_20260730_153000.mp4")

    def test_mix_output_path_keeps_mix_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            server, "_stamp_name", return_value="20260730_153000"
        ):
            out_dir = Path(tmp)
            video = Path("D:/素材/夏季衬衫.mp4")

            source_name = server._mix_output_path(out_dir, video, "source")
            timestamp_name = server._mix_output_path(out_dir, video, "source_timestamp")

        self.assertEqual(source_name.name, "夏季衬衫_混剪.mp4")
        self.assertEqual(timestamp_name.name, "夏季衬衫_混剪_20260730_153000.mp4")

    def test_existing_single_and_multi_version_outputs_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "夏季衬衫.mp4").touch()
            self.assertEqual(
                server._smart_output_path(out_dir, Path("D:/夏季衬衫.mp4"), "source").name,
                "夏季衬衫_2.mp4",
            )

            (out_dir / "夏季套装_v1.mp4").touch()
            self.assertEqual(
                server._smart_output_path(
                    out_dir,
                    Path("D:/夏季套装.mp4"),
                    "source",
                    versions=3,
                ).name,
                "夏季套装_2.mp4",
            )

    def test_multi_version_worker_uses_the_requested_output_stem(self) -> None:
        source = (ROOT / "app" / "cutter_logic.py").read_text(encoding="utf-8")
        self.assertIn('f"{output_stem}_v{vi+1}.mp4"', source)
        self.assertIn('"outputs": generated_outputs', source)

    def test_multi_version_result_paths_are_collected_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            first = out_dir / "夏季衬衫_v1.mp4"
            second = out_dir / "夏季衬衫_v2.mp4"
            first.touch()
            second.touch()

            outputs = server._collect_smart_cut_outputs(
                out_dir,
                Path("D:/夏季衬衫.mp4"),
                0.0,
                out_dir / "夏季衬衫.mp4",
                {"版本数": 2, "outputs": [str(first), str(second)]},
            )

        self.assertEqual(set(outputs), {str(first), str(second)})

    def test_parameter_cards_follow_the_requested_order(self) -> None:
        markup = (ROOT / "web_client" / "frontend" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")

        for prefix in ("sc", "mix"):
            start = markup.index(f'data-collapsible-panel="{prefix}-params"')
            end = markup.index("</section>", start)
            panel = markup[start:end]
            ordered_ids = [
                f'{prefix}-output-naming',
                f'{prefix}-versions',
                f'{prefix}-duration',
                f'{prefix}-duration-tolerance',
                f'{prefix}-dedup',
            ]
            positions = [panel.index(f'id="{field_id}"') for field_id in ordered_ids]
            self.assertEqual(positions, sorted(positions))
            self.assertIn(f'output_naming_mode: $("{prefix}-output-naming")?.value', script)


if __name__ == "__main__":
    unittest.main()
