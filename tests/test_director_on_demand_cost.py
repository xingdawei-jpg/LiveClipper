import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'web_client'), str(ROOT / 'app')]
import server

SCRIPT = (ROOT / 'web_client/frontend/assets/app.js').read_text(encoding='utf-8')

def section(start, end):
    a = SCRIPT.index(start)
    return SCRIPT[a:SCRIPT.index(end, a)]


@unittest.skipUnless(shutil.which('node'), 'Node required')
class BrowserBehaviorTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, encoding='utf-8', check=True)
        return json.loads(result.stdout)

    def test_prices_weekends_boundaries_and_each_request_not_report_time(self):
        code = section('const deepSeekPreviewRatesCnyPerMillion =', 'function versionCostNoteText(')
        code += section('function beijingPeakWindowFromReport(', 'function renderCommerceDirectorCostStrip(')
        code += '''
const times = ['2026-09-07T08:59:59+08:00','2026-09-07T09:00:00+08:00',
'2026-09-07T12:00:00+08:00','2026-09-07T14:00:00+08:00','2026-09-07T18:00:00+08:00',
'2026-09-06T10:00:00+08:00','2026-09-05T15:00:00+08:00'];
const row = time => ({model:'deepseek-v4-flash',request_started_at:time,
input_tokens:1000000,cached_input_tokens:200000,output_tokens:1000000});
const report = {generated_at:'2026-09-07T14:00:00+08:00',tokens:{known_total_tokens:4000000},
by_model:[{model:'deepseek-v4-flash'}],output_diagnostics:[row(times[0]),row(times[1])]};
const summary = commerceDirectorPreviewCostSummary({cost_report:report});
report.output_diagnostics[1].cached_input_tokens=null;
const missing = commerceDirectorPreviewCostSummary({cost_report:report});
console.log(JSON.stringify({windows:times.map(t=>beijingPeakWindowFromReport({request_started_at:t}).peak),
amount:summary.cost,label:summary.priceWindowLabel,basis:summary.details[0].basis,missing:missing.cost}));
'''
        result = self.run_js(code)
        self.assertEqual(result['windows'], [False, True, False, True, False, False, False])
        self.assertAlmostEqual(result['amount'], 17.13)
        self.assertEqual(result['label'], '跨峰谷')
        self.assertEqual(result['basis'], '请求开始时间')
        self.assertIsNone(result['missing'])

    def test_cost_strip_shows_only_the_peak_or_offpeak_estimate(self):
        strip = section('function renderCommerceDirectorCostStrip(', 'function collectFeaturePayload(')
        self.assertIn('${escapeHtml(summary.priceWindowLabel)}约 ¥${summary.cost.toFixed(3)}', strip)
        self.assertNotIn('<details>', strip)
        self.assertNotIn('峰谷费用明细', strip)

    def test_unbuilt_titles_remain_visible_after_one_alternative_is_built(self):
        code = section('function commerceDirectorProposalId(', 'function commerceDirectorRenderSelectionKey(')
        code += '''
const proposals = [1,2,3].map(i=>({director_strategy_id:'D'+i,primary_story_id:'S'+i,available:true,requires_additional_ai_call:i>1}));
const p={id:'root',director_review:{active_director_strategy_id:'S1',director_strategy_library:{proposals},
director_variants:[{strategy_id:'S1',preview_id:'root',available:true},{strategy_id:'S2',preview_id:'child',available:true}]}};
console.log(JSON.stringify(commerceDirectorPlanRows(p)));
'''
        rows = self.run_js(code)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r['preview_id'] for r in rows], ['root', 'child', ''])
        self.assertTrue(rows[2]['can_generate'])

    def test_cancel_does_not_request_and_existing_variant_switches_without_confirm(self):
        # Extract the async function up to the next top-level declaration.
        start = SCRIPT.index('async function selectCommerceDirectorStrategy(')
        end = SCRIPT.index('\n}', start) + 2
        code = SCRIPT[start:end]
        code = '''const state={smartPreview:{id:'root'}};let confirms=0,requests=0,switches=[];
const window={confirm:()=>{confirms++;return false;}};
const api=()=>{requests++;throw Error('must not call');};const toast=()=>{};
let rows=[];const commerceDirectorPlanRows=()=>rows;
const switchPreviewDirectorVariant=async id=>switches.push(id);
''' + code + '''
(async()=>{await selectCommerceDirectorStrategy('D2',true);
rows=[{director_strategy_id:'D2',preview_id:'child'}];await selectCommerceDirectorStrategy('D2',true);
console.log(JSON.stringify({confirms,requests,switches}));})();'''
        result = self.run_js(code)
        self.assertEqual(result, {'confirms': 1, 'requests': 0, 'switches': ['child']})

    def test_workbench_renders_paid_button_for_unbuilt_card(self):
        code = section('function commerceDirectorProposalId(', 'function commerceDirectorRenderSelectionKey(')
        code += section('function renderCommerceDirectorRecommendationCard(', 'function togglePreviewOverviewDetails(')
        code += '''
const escapeHtml=s=>String(s||''); const document={querySelector:()=>null};
const previewDirectorOutline=()=>[],previewWorkbenchSelectedClips=()=>[],previewDirectorActiveChapterId=()=>'';
const previewDirectorCurrentStatus=()=>({status:'ok'}),previewDirectorHasManualEdits=()=>false;
const previewWorkbenchCandidateFilterStats=()=>({duration:0}),commerceDirectorSelectedRenderIds=()=>new Set(['root']);
const renderCommerceDirectorCostStrip=()=>'';
const preview={id:'root',commercial_director_experiment:true,director_review:{active_director_strategy_id:'S1',
director_strategy_library:{proposals:[{director_strategy_id:'D1',primary_story_id:'S1',name:'主方案',available:true},
{director_strategy_id:'D2',primary_story_id:'S2',name:'备选题目',available:true,requires_additional_ai_call:true}]}}};
console.log(JSON.stringify(renderCommerceDirectorRecommendationCard(preview)));
'''
        html = self.run_js(code)
        self.assertIn('data-action="select-commerce-director-strategy"', html)
        self.assertIn('data-director-strategy-id="D2"', html)
        self.assertIn('data-additional-ai-call="true"', html)
        self.assertIn('生成并查看 · 额外消耗 AI', html)


class DirectionFamilyTests(unittest.TestCase):
    def test_mix_three_versions_use_the_same_two_pass_multi_plan_packet(self):
        payload = server.MixPayload(video_paths=['C:/one.mp4', 'C:/two.mp4'], duration=60, versions=3)
        source_bundle = {'sources': ['one', 'two']}
        with (
            mock.patch.object(server, '_ensure_feature_access'),
            mock.patch.object(server, '_existing_paths', return_value=[Path('C:/one.mp4'), Path('C:/two.mp4')]),
            mock.patch.object(server, '_set_task'),
            mock.patch.object(server, '_store_preview'),
            mock.patch.object(server, '_prepare_mix_director_source', return_value=source_bundle),
            mock.patch.object(server, '_run_commerce_director_preview') as run_director,
        ):
            server._run_commerce_director_mix_preview('task', 'preview', payload)

        self.assertEqual(run_director.call_args.args[:3], ('task', 'preview', mock.ANY))
        forwarded = run_director.call_args.args[2]
        self.assertEqual(forwarded.versions, 3)
        self.assertEqual(run_director.call_args.kwargs['preview_scope'], 'mix')
        self.assertIs(run_director.call_args.kwargs['source_bundle'], source_bundle)
        contract = run_director.call_args.kwargs['director_strategy_contract']
        self.assertEqual(contract['semantic_call_count'], 2)
        self.assertEqual(contract['requested_director_plan_count'], 3)
        self.assertEqual(contract['packet_mode'], 'multi_plan')

    def test_link_keeps_parent_edits_costs_and_unbuilt_direction_and_reuses_on_both_routes(self):
        for scope, route in [('smart-cut', server.select_commerce_director_strategy), ('mix', server.select_mix_commerce_director_strategy)]:
            with self.subTest(scope=scope), mock.patch.dict(server._CLIP_PREVIEWS, {}, clear=True):
                proposals = [{'director_strategy_id': f'D{i}', 'primary_story_id': f'S{i}', 'name': f'方向{i}',
                              'available': True, 'requires_additional_ai_call': i > 1} for i in (1,2,3)]
                server._store_preview('root', status='ready', scope=scope, commercial_director_experiment=True,
                    raw_clips=['original'], clips=[], selection_draft={'order':[3,1]},
                    dedup_summary={'cost_report':{'tokens':10}},
                    director_review={'active_director_strategy_id':'S1', 'director_strategy_library':{'proposals':proposals}})
                server._store_preview('child', director_family_root='root', director_requested_proposal='D2')
                server._store_preview('child', status='ready', scope=scope, commercial_director_experiment=True,
                    clips=[{'duration':3}], dedup_summary={'cost_report':{'tokens':20}}, director_review={})
                root, child = server._get_preview('root'), server._get_preview('child')
                self.assertEqual(root['selection_draft'], {'order':[3,1]})
                self.assertEqual(root['raw_clips'], ['original'])
                self.assertEqual(child['parent_preview_id'], 'root')
                self.assertEqual(child['director_review']['active_director_strategy_id'], 'S2')
                self.assertEqual(root['dedup_summary']['cost_report']['tokens'],10)
                self.assertEqual(child['dedup_summary']['cost_report']['tokens'],20)
                self.assertEqual(len(child['director_review']['director_strategy_library']['proposals']),3)
                with mock.patch.object(server, '_ensure_scope_idle'), mock.patch.object(server.threading, 'Thread') as thread:
                    result = route(server.CommerceDirectorStrategySelectionPayload(
                        preview_id='child', director_strategy_id='D2', confirm_additional_ai_call=False))
                    self.assertTrue(result['reused'])
                    self.assertEqual(result['additional_ai_calls'],0)
                    thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
