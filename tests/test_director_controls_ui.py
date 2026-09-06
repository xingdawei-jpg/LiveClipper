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
    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend contract checks")
    def test_auto_gender_still_lists_concrete_clothing_types_and_preserves_sku(self):
        taxonomy = SCRIPT[SCRIPT.index("const primaryCategoryTaxonomy ="):SCRIPT.index("const autoCategoryValues =")]
        code = taxonomy + r'''
const elements = {};
const $ = id => elements[id];
const document = {createElement: () => ({})};
const isAutoCategoryValue = v => v === '自动识别';
const currentCategoryAiProfile = () => ({key:'服饰内衣'});
'''
        code += js_function("refreshCategoryLeafSuggestions", "directorDirectionOptions")
        code += r'''
const results = ['sc','mix'].map(prefix => {
  const options = [];
  elements[`${prefix}-leaf-category`] = {value:'针织衫'};
  elements[`${prefix}-leaf-category-list`] = {appendChild:o=>options.push(o.value)};
  elements[`${prefix}-secondary-category`] = {value:'自动识别'};
  elements[`${prefix}-main-product`] = {value:'白色针织上衣'};
  refreshCategoryLeafSuggestions(prefix, primaryCategoryTaxonomy['服饰内衣']);
  return {options, main:elements[`${prefix}-main-product`].value, leaf:elements[`${prefix}-leaf-category`].value};
});
console.log(JSON.stringify(results));
'''
        result = subprocess.run([shutil.which("node"), "-e", code], text=True, encoding="utf-8", capture_output=True, check=True)
        for actual in json.loads(result.stdout):
            self.assertEqual(actual["main"], "白色针织上衣")
            self.assertEqual(actual["leaf"], "针织衫")
            for value in ("上衣", "针织衫", "外套", "羽绒服", "半身裙", "裤子", "套装"):
                self.assertIn(value, actual["options"])
            self.assertNotIn("大码女装", actual["options"])

    def test_smart_and_mix_have_one_direction_and_no_quality_downgrade_control(self):
        html = (ROOT / "web_client/frontend/index.html").read_text(encoding="utf-8")
        parser = IdParser()
        parser.feed(html)
        for prefix in ("sc", "mix"):
            for suffix in ("goal", "extra-instruction", "leaf-category", "main-product", "content-price", "content-inventory", "content-size", "content-styling", "ai-preset"):
                self.assertEqual(parser.ids.count(f"{prefix}-{suffix}"), 1)
            for suffix in ("focus", "strictness", "hook-style", "ending-style", "selling-custom"):
                self.assertNotIn(f"{prefix}-{suffix}", parser.ids)
            self.assertNotIn(f'data-ai-control="{prefix}-selling"', html)
            self.assertNotIn(f'data-ai-control="{prefix}-avoid"', html)
        self.assertEqual(html.count('class="director-section director-product-section"'), 2)
        self.assertEqual(html.count('class="director-section director-content-section"'), 2)
        self.assertEqual(html.count('class="director-section director-strategy-section"'), 2)
        self.assertEqual(html.count("<strong>本次内容规则</strong>"), 2)
        self.assertEqual(html.count("跟随项目"), 2)
        self.assertNotIn("本次覆盖卖点", html)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend contract checks")
    def test_actual_collector_has_one_direction_no_stale_preferences_and_effective_policy(self):
        code = r'''
const values = {'goal':'穿着体验','main-product':'焦糖朗姆','extra-instruction':'先讲三伏天不粘身，再用面料作证明。',
 'content-price':'allow','content-inventory':'inherit','content-size':'body_only','content-styling':'block'};
const $ = id => ({value:values[id.replace(/^(sc|mix)-/, '')] || ''});
const checkedControlValues = () => { throw Error('Old checkbox preferences leaked'); };
const primaryCategoryValue = () => '服饰内衣';
const base = {price:'block',cta:'block',size_interaction:'block',inventory_pressure:'block',source_claim:'block',custom_rules:[{text:'绝对有效',action:'block'}]};
const collectContentPolicy = () => base;
const collectPreferenceWeights = () => { throw Error('Old weights leaked'); };
'''
        code += js_function("directorContentChoices", "refreshDirectorContentLabels")
        code += js_function("collectAiControls", "collectPipPayload")
        code += "console.log(JSON.stringify({runs:['sc','mix'].map(collectAiControls),base}));"
        result = subprocess.run([shutil.which("node"), "-e", code], text=True, encoding="utf-8", capture_output=True, check=True)
        output = json.loads(result.stdout)
        smart, mix = output['runs']
        self.assertEqual(smart, mix)
        self.assertEqual(smart["controls_version"], "director-controls-v2")
        self.assertEqual(smart["goal"], "穿着体验")
        self.assertEqual(smart["main_product"], "焦糖朗姆")
        self.assertIn("再用面料作证明", smart["extra_instruction"])
        for key in ('selling_points','priority_terms','hook_style','ending_style','strictness'):
            self.assertNotIn(key, smart)
        self.assertEqual(smart["preference_weights"], {})
        self.assertEqual(smart["supporting_products"], "block")
        self.assertEqual(smart["avoid"], ["无关闲聊", "无效重复", "搭配其他品"])
        self.assertEqual(smart["content_policy"]["price"], "allow")
        self.assertEqual(smart["content_policy"]["cta"], "allow")
        self.assertEqual(smart["content_policy"]["size_interaction"], "body_only")
        self.assertEqual(smart["content_policy"]["inventory_pressure"], "block")
        self.assertEqual(smart["content_policy"]["custom_rules"], output['base']['custom_rules'])
        self.assertEqual(output['base']['price'], 'block')

    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend contract checks")
    def test_old_preset_migrates_to_visible_choices_not_hidden_sell_point_lists(self):
        code = js_function("normalizeDirectorDirection", "directorContentChoices")
        code += js_function("migrateDirectorPreset", "applyCompactDirectorValues")
        code += r'''
const choices = ['自动','上身效果','穿着体验','搭配场景','面料工艺'];
const old = {goal:'场景种草', hook:'上身效果开头', ending:'信任背书',selling:['版型显瘦','质感高级'],selling_custom:['三伏天','不粘身'],avoid:['价格','尺码','库存','搭配其他品']};
const migrated = migrateDirectorPreset(old, choices);
console.log(JSON.stringify([migrated,migrateDirectorPreset(migrated,choices),migrateDirectorPreset({goal:'爆款种草'},choices)]));
'''
        result = subprocess.run([shutil.which("node"), "-e", code], text=True, encoding="utf-8", capture_output=True, check=True)
        migrated, again, automatic = json.loads(result.stdout)
        self.assertEqual(migrated, again)
        self.assertEqual(migrated['goal'], '搭配场景')
        self.assertEqual(migrated['content_choices'], dict(price='block',inventory='block',size='block',styling='block'))
        self.assertEqual(migrated['extra_instruction'], '可优先参考：三伏天、不粘身')
        self.assertIn('不再生效', migrated['migration_note'])
        self.assertEqual(automatic['goal'], '自动')

    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend contract checks")
    def test_preset_roundtrip_saves_only_visible_contract(self):
        code = r'''
const values = {'primary-category':'服饰内衣', 'goal':'搭配场景','main-product':'白衬衫', 'extra-instruction':'下班也能穿', 'content-price':'allow','content-size':'body_only','content-inventory':'block','content-styling':'allow'};
const $ = id => ({value:values[id.replace(/^(sc|mix)-/, '')] || '',textContent:''});
const primaryCategoryValue = () => '服饰内衣';
const backendCategoryForPrimary = () => '服饰内衣';
'''
        code += js_function("directorContentChoices", "directorContentPolicy")
        code += js_function("collectCurrentAiPreset", "saveCurrentAiPreset")
        code += "console.log(JSON.stringify(collectCurrentAiPreset('sc','我的预设')));"
        result = subprocess.run([shutil.which("node"), "-e", code], text=True, encoding="utf-8", capture_output=True, check=True)
        preset = json.loads(result.stdout)
        self.assertEqual(preset['content_choices']['price'], 'allow')
        self.assertEqual(preset['extra_instruction'], '下班也能穿')
        for key in ('hook','ending','selling','selling_custom','avoid'):
            self.assertNotIn(key,preset)


if __name__ == "__main__":
    unittest.main()
