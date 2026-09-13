from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "web_client/frontend/assets/app.js").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "web_client"))
sys.path.insert(0, str(ROOT / "app"))

import server
from ai_model_config import normalize_ai_base_url, normalize_ai_model_defaults


def js_function(name: str, next_name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    return SCRIPT[start:SCRIPT.index(f"function {next_name}(", start)]


class DoubaoArkProviderUiTests(unittest.TestCase):
    def test_save_preserves_other_provider_and_migrates_legacy_key(self):
        old = server._with_ai_provider_profiles({
            "api_key": "deepseek-test", "base_url": "https://api.deepseek.com",
            "model": "deepseek-test-model",
        })
        with mock.patch.object(server, "_load_settings", return_value=old), \
             mock.patch.object(server, "_save_settings", return_value=True) as save, \
             mock.patch.object(server, "emit_log"):
            server.save_settings(server.SettingsPayload(
                api_key="ark-test", base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="doubao-test-model",
            ))
        saved = save.call_args.args[0]
        self.assertEqual(saved["api_key"], "ark-test")
        self.assertEqual(saved["ai_provider_profiles"]["deepseek"]["api_key"], "deepseek-test")
        self.assertEqual(saved["ai_provider_profiles"]["doubao-ark"]["api_key"], "ark-test")
        self.assertEqual(server._with_ai_provider_profiles(saved), saved)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_switch_roundtrip_isolates_keys_models_and_custom_drafts(self):
        configs = SCRIPT[SCRIPT.index("const aiProviderConfigs ="):SCRIPT.index("const keywordFields =")]
        code = configs + SCRIPT[SCRIPT.index("function selectedAiProvider("):SCRIPT.index("function collectSettings(")]
        code += r'''
const assert = require("assert");
const elements = {};
for (const id of ["s-ai-provider", "s-base-url", "s-model", "s-api-key",
                  "s-api-key-label", "s-model-label", "s-ai-provider-hint"])
  elements[id] = {value:"", addEventListener:(_, cb) => elements[id].change = cb};
const $ = id => elements[id];
$("s-ai-provider").value = "deepseek";
$("s-base-url").value = "https://api.deepseek.com";
$("s-api-key").value = "deepseek-test";
$("s-model").value = "deepseek-custom-model";
bindAiProviderControls();
function switchTo(value) { $("s-ai-provider").value = value; $("s-ai-provider").change(); }
switchTo("doubao-ark");
assert.equal($("s-api-key").value, "");
$("s-api-key").value = "ark-test";
$("s-model").value = "ep-test";
switchTo("deepseek");
assert.equal($("s-api-key").value, "deepseek-test");
assert.equal($("s-model").value, "deepseek-custom-model");
switchTo("doubao-ark");
assert.equal($("s-api-key").value, "ark-test");
assert.equal($("s-model").value, "ep-test");
switchTo("custom");
assert.equal($("s-api-key").value, "");
assert.equal($("s-base-url").value, "");
'''
        subprocess.run([shutil.which("node"), "-e", code], check=True, capture_output=True)

    def test_ai_model_settings_expose_doubao_ark_entrypoint(self) -> None:
        markup = (ROOT / "web_client/frontend/index.html").read_text(encoding="utf-8")
        self.assertIn('id="s-ai-provider"', markup)
        self.assertIn('value="doubao-ark"', markup)
        self.assertIn('id="s-ai-provider-hint"', markup)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend contract checks")
    def test_doubao_provider_applies_ark_url_and_accepts_model_or_endpoint_id(self) -> None:
        configs = SCRIPT[SCRIPT.index("const aiProviderConfigs ="):SCRIPT.index("const keywordFields =")]
        code = configs + js_function("selectedAiProvider", "syncAiProviderUi") + js_function("syncAiProviderUi", "bindAiProviderControls")
        code += r'''
const elements = {
  "s-ai-provider": {value:"doubao-ark"},
  "s-base-url": {value:"https://api.deepseek.com"},
  "s-model": {value:"deepseek-v4-flash", placeholder:""},
  "s-api-key-label": {textContent:""},
  "s-model-label": {textContent:""},
  "s-ai-provider-hint": {textContent:""},
};
const $ = id => elements[id];
syncAiProviderUi({applyProviderDefaults:true});
console.log(JSON.stringify(elements));
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", code],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        elements = json.loads(result.stdout)
        self.assertEqual(elements["s-base-url"]["value"], "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(elements["s-model"]["value"], "")
        self.assertIn("doubao-seed-2-1-pro-260628", elements["s-model"]["placeholder"])
        self.assertEqual(elements["s-model-label"]["textContent"], "模型或接入点")
        self.assertIn("方舟 API Key", elements["s-ai-provider-hint"]["textContent"])

    def test_missing_ark_endpoint_is_rejected_before_connection_test(self) -> None:
        settings = normalize_ai_model_defaults({
            "api_key": "ark-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "model": "",
        })
        self.assertEqual(settings["model"], "")
        warning = server._ai_provider_warning(
            settings["base_url"],
            settings["model"],
        )
        self.assertIn("模型 ID", warning)

    def test_ark_responses_url_normalizes_to_chat_compatible_base(self) -> None:
        self.assertEqual(
            normalize_ai_base_url("https://ark.cn-beijing.volces.com/api/v3/responses"),
            "https://ark.cn-beijing.volces.com/api/v3",
        )
