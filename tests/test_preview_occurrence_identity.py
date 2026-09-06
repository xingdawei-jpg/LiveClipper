import copy
import json
import shutil
import subprocess
import unittest
from tests.test_preview_word_editing import ROOT, server


class PreviewOccurrenceIdentityTests(unittest.TestCase):
    def preview(self, ids=(1, 2, 1)):
        raw = [("product", f"原话{i}", float(i), float(i+1), 0, 1.0, "", "C:/same.mp4") for i in ids]
        public = server._preview_public_clips(raw)
        for clip in public:
            clip["segments"] = [{"index": 0, "start": clip["start"], "end": clip["end"], "text": clip["text"],
                                 "words": [{"index": i, "text": text, "start": clip["start"]+i*.25, "end": clip["start"]+(i+1)*.25} for i, text in enumerate("真实原话")]}]
        return {"id": "occurrence", "status": "ready", "candidate_raw_clips": raw,
                "candidate_clips": public, "clips": copy.deepcopy(public)}

    def draft(self, p, indices, **kwargs):
        return server._normalize_preview_selection_draft(p, "smart", indices, {}, order=indices, **kwargs)

    def test_two_aele_sequences_keep_every_occurrence_and_ai_order(self):
        for ids in [
            [1,21,15,31,79,82,67,75,82,83,85,87,31,9,118,31,37,34,67,31,11,75,113,117,112,114],
            [1,15,21,15,21,141,31,9,36,29,139,37,139,132,67,79,70,75,114,118],
        ]:
            p = self.preview(ids)
            original_keys = [c["candidate_key"] for c in p["clips"]]
            indices = list(range(len(ids)))
            migrated = self.draft(p, indices, selected_keys=original_keys, order_keys=original_keys)
            self.assertEqual(migrated["selected_indices"], indices)
            self.assertEqual(migrated["order"], indices)
            self.assertEqual(len(set(migrated["selected_keys"])), len(ids))
            reloaded = self.draft(p, indices, selected_keys=migrated["selected_keys"], order_keys=migrated["order_keys"])
            self.assertEqual(reloaded["selected_indices"], indices)
            self.assertEqual([c["candidate_key"] for c in p["clips"]], original_keys)

    def test_removed_occurrence_stays_removed_in_legacy_draft(self):
        p = self.preview()
        source_key = p["clips"][0]["candidate_key"]
        self.assertEqual(self.draft(p, [2], selected_keys=[source_key])["selected_indices"], [2])

    def test_occurrence_words_and_reordering_round_trip_independently(self):
        p = self.preview()
        keys = [c["selection_key"] for c in server._preview_selection_public_clips(p)]
        draft = self.draft(p, [2, 0], selected_keys=[keys[2], keys[0]], order_keys=[keys[2], keys[0], keys[1]],
                           selected_words_by_key={keys[0]: {"0": [0, 1]}, keys[2]: {"0": [2, 3]}})
        self.assertEqual(draft["selected_indices"], [2, 0])
        self.assertEqual(draft["selected_words"], {"0": {"0": [0, 1]}, "2": {"0": [2, 3]}})
        public = server._preview_public(p)
        self.assertEqual([c["selection_key"] for c in public["clips"]], keys)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_frontend_restores_legacy_and_new_keys_without_last_occurrence_wins(self):
        script = (ROOT / "web_client/frontend/assets/app.js").read_text(encoding="utf-8")
        start = script.rfind("function applyPreviewDraftToState(")
        apply = script[start:script.index("function resetPreviewSegmentWords(", start)]
        start = script.index("function previewCandidateKey(")
        key_func = script[start:script.index("function buildPreviewDraftFromState(", start)]
        p = server._preview_public(self.preview())
        # Frontend uses the candidate pool as preview.clips.
        code = "let preview = " + json.dumps({"id": p["id"], "clips": p["candidate_clips"]}, ensure_ascii=False) + ";\n"
        code += r'''
const state = {previewAssemblyOrders:{}};
const getPreviewState = () => preview;
const previewAssemblyOrderKey = () => 'test';
const normalizedIntegerList = xs => [...new Set((xs||[]).map(Number).filter(Number.isInteger))];
const previewSegments = c => c.segments || [];
const previewSegmentWords = s => s.words || [];
const isPreviewWordLocked = w => w.selection_locked === true;
const selectedPreviewWords = s => s.words.filter(w=>w.selected!==false);
const isPreviewSegmentSelected = s => s.selected !== false;
const isPreviewWorkbenchSelected = c => c.selected !== false;
'''
        code += key_func + apply
        code += r'''
const clips = preview.clips;
const oldKeys = clips.map(c=>c.candidate_key);
applyPreviewDraftToState('smart', {selected_indices:[0,1,2], selected_keys:oldKeys, order:[0,1,2], order_keys:oldKeys,
 selected_words:{'0':{'0':[0,1]},'2':{'0':[2,3]}}});
const legacy = {order:state.previewAssemblyOrders.test, words:clips.map(c=>selectedPreviewWords(c.segments[0]).map(w=>w.index))};
applyPreviewDraftToState('smart', {selected_indices:[2,0], selected_keys:[clips[2].selection_key,clips[0].selection_key],
 selected_words_by_key:{[clips[0].selection_key]:{'0':[1]},[clips[2].selection_key]:{'0':[3]}}});
console.log(JSON.stringify({legacy, order:state.previewAssemblyOrders.test, selected:clips.map(c=>c.selected),
 words:[0,2].map(i=>selectedPreviewWords(clips[i].segments[0]).map(w=>w.index))}));
'''
        result = json.loads(subprocess.run([shutil.which("node"), "-e", code], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
        self.assertEqual(result["legacy"]["order"], [0, 1, 2])
        self.assertEqual(result["legacy"]["words"][0], [0, 1])
        self.assertEqual(result["legacy"]["words"][2], [2, 3])
        self.assertEqual(result["order"], [2, 0])
        self.assertEqual(result["selected"], [True, False, True])
        self.assertEqual(result["words"], [[1], [3]])


if __name__ == "__main__":
    unittest.main()
