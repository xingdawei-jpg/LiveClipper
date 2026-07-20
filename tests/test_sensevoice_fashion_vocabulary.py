from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr_quality = importlib.import_module("local_asr_quality")
volcengine_asr = importlib.import_module("volcengine_asr")


def _timed_characters(text: str) -> list[dict[str, float | str]]:
    return [
        {"text": char, "start": index * 0.1, "end": (index + 1) * 0.1}
        for index, char in enumerate(text)
    ]


class SenseVoiceFashionVocabularyTests(unittest.TestCase):
    def test_observed_fashion_terms_are_corrected_without_changing_time_range(self) -> None:
        text = (
            "色质它是纱线，色织芝麻和常规染色麻。枝数高，枝树啊，高枝啊，"
            "高颗重的亚麻，下意失踪的穿法。采雷率低，采住率会低。"
            "只要你够兴我在我这买。一00纯亚麻，8谷折，13V尼羊绒，我们色置的面料。"
        )
        spoken = "".join(char for char in text if char not in "，。")
        segment = {
            "text": text,
            "start": 0.0,
            "end": len(spoken) * 0.1,
            "words": _timed_characters(spoken),
        }

        corrected, count = local_asr_quality.apply_domain_corrections([segment])

        self.assertEqual(count, 14)
        expected = (
            "色织它是纱线，色织色织麻和常规染色麻。支数高，支数啊，高支啊，"
            "高克重的亚麻，下衣失踪的穿法。踩雷率低，踩雷率会低。"
            "只要你够信我在我这买。100纯亚麻，85折，13微米羊绒，我们色织的面料。"
        )
        self.assertEqual(corrected[0]["text"], expected)
        self.assertEqual(
            "".join(word["text"] for word in corrected[0]["words"]),
            expected.replace("，", "").replace("。", ""),
        )
        self.assertEqual(corrected[0]["words"][0]["start"], segment["words"][0]["start"])
        self.assertEqual(corrected[0]["words"][-1]["end"], segment["words"][-1]["end"])

    def test_existing_sensevoice_sidecar_is_upgraded_when_loaded(self) -> None:
        source = "色质它是纱线枝数高"
        segment = {
            "text": source,
            "start": 0.0,
            "end": len(source) * 0.1,
            "words": _timed_characters(source),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = Path(temp_dir) / "existing.srt"
            volcengine_asr.write_word_timing_sidecar(
                str(srt_path),
                [segment],
                provider="sensevoice",
            )

            loaded = volcengine_asr.load_word_timing_sidecar(str(srt_path))

        self.assertEqual(loaded[0]["text"], "色织它是纱线支数高")
        self.assertEqual(
            "".join(word["text"] for word in loaded[0]["words"]),
            "色织它是纱线支数高",
        )

    def test_latest_preview_material_and_sizing_errors_are_corrected(self) -> None:
        text = (
            "面料每一米度70多，克重亚麻克重越高，亚麻的支数枝数越高，你知数高就不一样。"
            "穿戴上没有任何扎腹感，它的腹感还是很舒服。"
            "这个吃量如果非得按体重算就是180，手工拉毛被全部纯手工，全部是定制色之再先染纱。"
            "东西完全跟常规养麻不一样，克重比一般养苗要重，贵和便宜悬乎啊？它这就是有差别。"
            "哦，流量汗删。不要冲小费，不要成动消费；它不会被重蛀，就麻着料子。"
            "我们家没有紧上的衣服，它还能一多穿。僧侣，而且有点偏岔气的一个款。100串元吗？"
            "风衣薏米面料能做到50。"
        )
        punctuation = "，。；！？?"
        spoken = "".join(char for char in text if char not in punctuation)
        segment = {
            "text": text,
            "start": 0.0,
            "end": len(spoken) * 0.1,
            "words": _timed_characters(spoken),
        }

        corrected, count = local_asr_quality.apply_domain_corrections([segment])

        expected = (
            "面料每一米都70多，亚麻克重越高，亚麻的支数越高，你支数高就不一样。"
            "穿戴上没有任何扎肤感，它的肤感还是很舒服。"
            "这个尺码如果非得按体重算就是180，手工拉毛边全部纯手工，全部是定制色织再先染纱。"
            "东西完全跟常规亚麻不一样，克重比一般亚麻要重，贵和便宜悬殊啊？它这就是有差别。"
            "哦，流浪汉衫。不要冲动消费，不要冲动消费；它不会被虫蛀，就麻质料子。"
            "我们家没有紧身的衣服，它还能一衣多穿。僧侣，而且有点偏禅系的一个款。1000元吗？"
            "风衣一米面料能做到50。"
        )
        self.assertEqual(count, 22)
        self.assertEqual(corrected[0]["text"], expected)
        self.assertEqual(
            "".join(word["text"] for word in corrected[0]["words"]),
            "".join(char for char in expected if char not in punctuation),
        )
        self.assertEqual(corrected[0]["words"][0]["start"], segment["words"][0]["start"])
        self.assertEqual(corrected[0]["words"][-1]["end"], segment["words"][-1]["end"])

    def test_phase_two_rules_do_not_replace_ambiguous_words_without_context(self) -> None:
        text = "每一米度量不同，腹感不适，这个吃量很大，拉毛被套，色之后再看，儿童养苗基地。"
        spoken = text.replace("，", "").replace("。", "")
        segment = {
            "text": text,
            "start": 0.0,
            "end": len(spoken) * 0.1,
            "words": _timed_characters(spoken),
        }

        corrected, count = local_asr_quality.apply_domain_corrections([segment])

        self.assertEqual(count, 0)
        self.assertEqual(corrected[0]["text"], text)
        self.assertEqual(
            "".join(word["text"] for word in corrected[0]["words"]),
            spoken,
        )


if __name__ == "__main__":
    unittest.main()
