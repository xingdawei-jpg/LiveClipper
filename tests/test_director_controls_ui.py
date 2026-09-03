from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "web_client/frontend/assets/app.js").read_text(encoding="utf-8")


def js_function(name: str, next_name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    return SCRIPT[start:SCRIPT.index(f"function {next_name}(", start)]


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        identifier = dict(attrs).get("id")
        if identifier:
            self.ids.append(identifier)


class DirectorControlsUiTests(unittest.TestCase):
    def test_smart_and_mix_have_one_direction_and_no_quality_downgrade_control(self):
        html = (ROOT / "web_client/frontend/index.html").read_text(encoding="utf-8")
        parser = IdParser()
        parser.feed(html)
        for prefix in ("sc", "mix"):
            for suffix in ("goal", "hook-style", "ending-style", "selling-custom"):
                self.assertEqual(parser.ids.count(f"{prefix}-{suffix}"), 1)
            self.assertNotIn(f"{prefix}-focus", parser.ids)
            self.assertNotIn(f"{prefix}-strictness", parser.ids)
        self.assertEqual(html.count("本次优先讲"), 2)
        self.assertNotIn("本次覆盖卖点", html)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend contract checks")
    def test_actual_collector_sends_topics_terms_and_long_term_preferences(self):
        code = r'''
const $ = id => ({value: id.endsWith('goal') ? '场景种草' : id.endsWith('main-product') ? '焦糖朗姆' : '自动'});
const checkedControlValues = key => key.endsWith('selling') ? ['穿着体验'] : ['价格','闲聊'];
const customSellingValues = () => ['三伏天','不粘身'];
const primaryCategoryValue = () => '服饰内衣';
const collectContentPolicy = () => ({price:'block',size_interaction:'body_only'});
const collectPreferenceWeights = () => ({'场景搭配':3});
'''
        code += js_function("collectAiControls", "collectPipPayload")
        code += "console.log(JSON.stringify(['sc','mix'].map(collectAiControls)));"
        result = subprocess.run([shutil.which("node"), "-e", code], text=True, encoding="utf-8", capture_output=True, check=True)
        smart, mix = json.loads(result.stdout)
        self.assertEqual(smart, mix)
        self.assertEqual(smart["goal"], "场景种草")
        self.assertEqual(smart["main_product"], "焦糖朗姆")
        self.assertEqual(smart["selling_points"], ["穿着体验"])
        self.assertEqual(smart["priority_terms"], ["三伏天", "不粘身"])
        self.assertEqual(smart["preference_weights"], {"场景搭配": 3})
        self.assertEqual(smart["content_policy"]["size_interaction"], "body_only")
        self.assertNotIn("strictness", smart)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend contract checks")
    def test_old_focus_is_migrated_to_preferred_topics_without_duplicates(self):
        code = js_function("sellingWithLegacyFocus", "collectCurrentAiPreset")
        code += "console.log(JSON.stringify([sellingWithLegacyFocus([], '场景搭配'), sellingWithLegacyFocus(['场景搭配'], '场景搭配'), sellingWithLegacyFocus([], '自动')]));"
        result = subprocess.run([shutil.which("node"), "-e", code], text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [["场景搭配"], ["场景搭配"], []])


if __name__ == "__main__":
    unittest.main()
