"""Regression checks for the omitted desktop QA and existing-store repair path."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import test_voice_builder as fixtures
import app as core
import voice_builder_state as state
import voice_builder_engine as engine
import voice_builder_text as text
import voice_builder_review as review
import voice_builder_worker as worker


class VoiceParityTests(unittest.TestCase):
    setUpClass = fixtures.VoiceBuilderTests.__dict__['setUpClass']
    tearDownClass = fixtures.VoiceBuilderTests.__dict__['tearDownClass']
    setUp = fixtures.VoiceBuilderTests.setUp
    tearDown = fixtures.VoiceBuilderTests.tearDown
    job = fixtures.VoiceBuilderTests.job
    api = fixtures.VoiceBuilderTests.api
    probe = fixtures.VoiceBuilderTests.probe
    normalize = fixtures.VoiceBuilderTests.normalize
    run_fast = fixtures.VoiceBuilderTests.run_fast

    def answer(self, url, payload=None, headers=None, **kwargs):
        self.assertEqual(url,'https://api.openai.com/v1/responses')
        rows = json.loads(payload['input'].splitlines()[-1])
        if 'QA editor' in payload['input']:
            output = [{'file':p['file'],'corrected_text':p['candidate'],'grammar_ok':True,
                       'meaning_ok':True,'character_ok':True,'review_required':False,'note':''} for p in rows]
        else:
            output = [{'file':p['file'],'text':('Javított kertkaland ' if 'TARGETED repair' in payload['input'] else 'Új kertkaland ' if 'copywriter' in payload['input'] else 'Traducere ')+p['file'],
                       'review_required':False,'note':''} for p in rows]
        return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'prompts':output})}]}]}

    def test_full_context_translation_shared_by_target_and_english_has_no_request(self):
        ro=state.Spec(provider='openai',voice_id='coral',locale='ro-RO',language='Română')
        md=ro.model_copy(update={'locale':'ro-MD','language':'Română Moldova','voice_id':'onyx'})
        with patch.object(engine,'request',side_effect=self.answer) as request:
            a=engine.load_prompts(ro,{'openai_key':'fake'},self.job(ro)['id'])
            b=engine.load_prompts(md,{'openai_key':'fake'},self.job(md)['id'])
            self.assertEqual(request.call_count,1)
            self.assertEqual(a['prompts'],b['prompts'])
            self.assertEqual(len(json.loads(request.call_args.args[1]['input'].splitlines()[-1])),201)
        en=ro.model_copy(update={'locale':'en-GB'})
        with patch.object(engine,'request',side_effect=AssertionError('English charged')):
            c=engine.load_prompts(en,{},self.job(en)['id'])
        self.assertEqual(c['prompts'],state.load_asset('prompts/en-US.json')['prompts'])
        self.assertEqual(text.target(ro.model_copy(update={'locale':'zh-HK'})),'zh-TW')
        self.assertEqual(text.target(ro.model_copy(update={'locale':'es-MX','translation_target':'es-MX'})),'es-MX')

    def test_qa_repairs_only_failed_line_then_rechecks_only_that_line_and_reuses_all_results(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        job=self.job(spec)
        qa_sizes=[]; repair_sizes=[]
        def answer(url,payload=None,headers=None,**kw):
            result=self.answer(url,payload,**kw)
            instruction=payload['input']; rows=json.loads(instruction.splitlines()[-1])
            if 'QA editor' in instruction:
                qa_sizes.append(len(rows))
                if len(rows)==201:
                    parsed=json.loads(result['output'][0]['content'][0]['text'])
                    next(p for p in parsed['prompts'] if p['file']=='A004.mp3')['meaning_ok']=False
                    result['output'][0]['content'][0]['text']=json.dumps(parsed)
            if 'TARGETED repair' in instruction:
                repair_sizes.append(len(rows))
            return result
        with patch.object(engine,'request',side_effect=answer) as requests:
            doc=engine.load_prompts(spec,{'openai_key':'fake'},job['id'])
            self.assertEqual(requests.call_count,4)
        self.assertEqual(qa_sizes,[201,1]); self.assertEqual(repair_sizes,[1])
        self.assertEqual(doc['targeted_repair_initial_count'],1)
        self.assertEqual(doc['targeted_repair_rounds'],1)
        self.assertEqual(doc['character_rewrite_ratio'],1)
        second=spec.model_copy(update={'voice_id':'onyx','character_effect':'robot'})
        with patch.object(engine,'request',side_effect=AssertionError('Duplicate text billing')):
            self.assertEqual(engine.load_prompts(second,{},self.job(second)['id']),doc)

    def test_failed_qa_stops_before_tts_and_does_not_reissue_any_request_on_resume(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        job=self.job(spec)
        def answer(url,payload=None,headers=None,**kw):
            result=self.answer(url,payload,**kw)
            if 'QA editor' in payload['input']:
                parsed=json.loads(result['output'][0]['content'][0]['text'])
                for p in parsed['prompts']:
                    if p['file']=='A004.mp3':
                        p['grammar_ok']=False
                result['output'][0]['content'][0]['text']=json.dumps(parsed)
            return result
        with patch.object(engine,'request',side_effect=answer) as request,\
             patch.object(state,'credentials',return_value={'openai_key':'fake'}),\
             patch.object(engine,'audio_request',side_effect=AssertionError('TTS before QA')):
            with self.assertRaisesRegex(RuntimeError,'végső'):
                engine.run_job(job)
            count=request.call_count
            with self.assertRaisesRegex(RuntimeError,'végső'):
                engine.run_job(job)
            self.assertEqual(request.call_count,count)
        self.assertFalse((state.job_dir(job['id'])/'pack.tar.gz').exists())
        self.assertEqual(self.api('/jobs/'+job['id']+'/publish','POST').status_code,409)

    def test_missing_or_stale_approval_cannot_publish_a_completed_archive(self):
        job=self.job(); self.run_fast(job)
        (state.job_dir(job['id'])/'text-approval.json').unlink()
        r=self.api('/jobs/'+job['id']+'/publish','POST')
        self.assertEqual(r.status_code,409)
        self.assertEqual(core._uploaded_voice_pack_registry()['packs'],[])

    def test_quarantine_requires_exact_current_revision_backs_up_before_hiding_and_keeps_price(self):
        job=self.job(); self.run_fast(job)
        uploaded=self.api('/jobs/'+job['id']+'/publish','POST').json()
        registry=core._uploaded_voice_pack_registry()
        record=registry['packs'][0]
        record.update(access='paid',price_amount=899,store_hidden=False)
        # Simulate a 1.0.48/49/50 styled job, with its existing archive unchanged.
        spec=self.spec.model_copy(update={'text_style':'funny'})
        with state.db() as c:
            c.execute('UPDATE jobs SET spec=? WHERE id=?',(state.canonical(spec.model_dump()),job['id']))
        (state.job_dir(job['id'])/'text-approval.json').unlink()
        unrelated={**record,'id':'unrelated','community_id':'desktop_unrelated','music_md5':'other','store_hidden':False}
        registry['packs'].append(unrelated);core._write_uploaded_voice_registry(registry)
        with patch.object(engine,'request',side_effect=AssertionError('Quarantine charged')):
            items=review.scan_and_quarantine()
            self.assertEqual(len(items),1)
            review.scan_and_quarantine()
        actual=core._uploaded_voice_pack_registry()['packs']
        self.assertTrue(actual[0]['store_hidden']);self.assertEqual(actual[0]['price_amount'],899)
        self.assertEqual(actual[0]['access'],'paid');self.assertFalse(actual[1]['store_hidden'])
        backup=list((state.root()/'review-backups'/job['id']).glob('*/registry-record.json'))
        self.assertEqual(len(backup),1)
        self.assertFalse(json.loads(backup[0].read_text())['store_hidden'])
        self.assertEqual((backup[0].parent/'pack.tar.gz').read_bytes(),(state.job_dir(job['id'])/'pack.tar.gz').read_bytes())
        # A later independent upload must not be quarantined based on an old job.
        actual[0].update(music_md5='new-payload',store_hidden=False,builder_review_required=False)
        core._write_uploaded_voice_registry({'schema':core.VOICE_PACKS_SCHEMA,'packs':actual})
        review.scan_and_quarantine()
        self.assertFalse(core._uploaded_voice_pack_registry()['packs'][0]['store_hidden'])

    def test_existing_unverified_text_is_qa_only_and_only_changed_audio_is_regenerated(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        job=self.job(spec)
        base=state.load_asset('prompts/hu-HU.json')
        old={**base,'prompts':[{**p,'text':p['text'] if text.critical(p['file']) else 'Korábbi kerti móka '+p['file']} for p in base['prompts']]}
        directory=state.job_dir(job['id']);state.atomic(directory/'prompts.json',state.canonical(old).encode())
        (directory/'audio').mkdir()
        for p in old['prompts']:
            (directory/'audio'/p['file']).write_bytes(self.audio)
        counts={'qa':0,'draft':0,'repair':0}
        def answer(url,payload=None,headers=None,**kw):
            result=self.answer(url,payload,**kw)
            instruction=payload['input']
            counts['qa' if 'QA editor' in instruction else 'repair' if 'TARGETED repair' in instruction else 'draft']+=1
            if 'QA editor' in instruction:
                parsed=json.loads(result['output'][0]['content'][0]['text'])
                for p in parsed['prompts']:
                    if p['file']=='A004.mp3':
                        p['corrected_text']='Kijavított kerti móka A004'
                result['output'][0]['content'][0]['text']=json.dumps(parsed)
            return result
        with patch.object(engine,'request',side_effect=answer),patch.object(engine,'probe',side_effect=self.probe),\
             patch.object(engine,'normalize',side_effect=self.normalize),\
             patch.object(state,'credentials',return_value={'openai_key':'fake'}),\
             patch.object(engine,'audio_request',return_value=(self.audio,True)) as audio:
            engine.run_job(job)
            self.assertEqual(audio.call_count,1)
        self.assertEqual(counts,{'qa':1,'draft':0,'repair':0})
        kept=json.loads((directory/'prompts.json').read_text())
        self.assertEqual(sum(b['text']!=p['text'] for b,p in zip(old['prompts'],kept['prompts'])),1)
        self.assertTrue(list((state.root()/'review-backups'/job['id']).glob('*/audio/A004.mp3')))

    def test_text_only_and_generate_build_validate_export_have_no_repeat_paid_calls(self):
        job=self.job(mode='text')
        with patch.object(engine,'request',side_effect=AssertionError('Bundled text charged')),\
             patch.object(engine,'audio_request',side_effect=AssertionError('Text mode ran TTS')):
            engine.run_job(job)
        self.assertFalse((state.job_dir(job['id'])/'pack.tar.gz').exists())
        second=self.job(mode='generate');count=self.run_fast(second);self.assertGreater(count,0);self.assertLessEqual(count,201)
        self.assertFalse((state.job_dir(second['id'])/'pack.tar.gz').exists())
        with patch.object(engine,'probe',side_effect=self.probe),\
             patch.object(engine,'audio_request',side_effect=AssertionError('Build charged')):
            self.assertTrue(self.api('/jobs/'+second['id']+'/validate','POST').json()['ok'])
            self.assertEqual(self.api('/jobs/'+second['id']+'/build','POST').status_code,200)
        archive=self.api('/jobs/'+second['id']+'/audio.zip').content
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            self.assertEqual(set(z.namelist()),engine.expected_names())
        body={'selections':[self.spec.model_dump()],'mode':'build'}
        r=self.api('/jobs','POST',json=body).json()
        self.assertEqual(r['skipped_completed'],[second['id']])

    def test_continue_on_error_false_pauses_remaining_jobs_and_upload_disabled_does_not_loop(self):
        ids=state.create_jobs([self.spec,self.spec],'build',10000,continue_on_error=False)
        with patch.object(worker,'run_job',side_effect=RuntimeError('test failure')),patch.object(worker.time,'sleep'):
            worker.main()
        self.assertEqual([state.get_job(x)['status'] for x in ids],['failed','paused'])
        jid=state.create_jobs([self.spec],'build',10000,upload_enabled=False)[0]
        state.update(jid,status='running');self.run_fast(state.get_job(jid))
        with patch.object(worker,'run_job',side_effect=AssertionError('Repeat')),patch.object(worker.time,'sleep'):
            worker.main()
        self.assertIsNone(state.get_job(jid)['published']);self.assertFalse(state.pending_work())

    def test_provider_lists_paginate_dedupe_and_filter_fish_models(self):
        with patch.object(state,'credentials',return_value={'elevenlabs_key':'fake','fish_audio_key':'fake'}):
            with patch.object(engine,'request',side_effect=[{'voices':[{'voice_id':'a','name':'A','labels':{'gender':'female'}}],
                    'has_more':True,'next_page_token':'next'}, {'voices':[{'voice_id':'b','name':'B'}],'has_more':False}]) as req:
                self.assertEqual([x['id'] for x in engine.available_voices('elevenlabs')],['a','b'])
                self.assertIn('next_page_token=next',req.call_args.args[0])
            models=[{'_id':'a','type':'tts','state':'trained','title':'A','tags':['female']},
                    {'_id':'bad','type':'asr','state':'trained'}, {'_id':'pending','type':'tts','state':'training'}]
            with patch.object(engine,'request',return_value={'items':models}) as req:
                self.assertEqual([x['id'] for x in engine.available_voices('fish_audio','query',True)],['a'])
                self.assertIn('licensed=true',req.call_args.args[0]);self.assertEqual(req.call_count,2)

    def test_desktop_verified_style_import_and_flags_are_preserved_without_new_qa(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        with patch.object(engine,'request',side_effect=self.answer):
            doc=engine.load_prompts(spec,{'openai_key':'fake'},self.job(spec)['id'])
        state.atomic(state.root()/'migrated-prompts'/'hu-HU_funny_natural.json',state.canonical({'doc':doc}).encode())
        # Remove server style cache, leaving only desktop-compatible proof fields.
        import shutil
        shutil.rmtree(state.root()/'prompt-cache')
        with patch.object(engine,'request',side_effect=AssertionError('Verified desktop QA charged')):
            restored=engine.load_prompts(spec,{},self.job(spec)['id'])
        self.assertEqual(restored,doc)

    def test_raw_import_normalizes_format_without_tts(self):
        job=self.job();state.update(job['id'],status='paused')
        state.atomic(state.job_dir(job['id'])/'prompts.json',state.canonical(state.load_asset('prompts/hu-HU.json')).encode())
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('A004.mp3',self.audio)
        with patch.object(engine,'spoken_valid',return_value=False),patch.object(engine,'raw_valid',return_value=True),\
             patch.object(engine,'normalize',side_effect=self.normalize) as normalize,\
             patch.object(engine,'audio_request',side_effect=AssertionError('Import charged')):
            r=self.api('/jobs/'+job['id']+'/import-audio','POST',files={'file':('raw.zip',archive.getvalue())})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(normalize.call_count,1)

    def test_reordered_prompts_and_nonretryable_quota_stop_without_second_paid_call(self):
        doc=state.load_asset('prompts/hu-HU.json');doc['prompts'].reverse()
        with self.assertRaisesRegex(ValueError,'sorrend'):
            engine.validate_prompts(doc)
        spec=self.spec.model_copy(update={'text_style':'funny'});job=self.job(spec)
        with patch.object(engine,'request',side_effect=engine.ProviderError(429,reason='insufficient_quota')) as req:
            with self.assertRaisesRegex(engine.ProviderError,'kerete'):
                engine.load_prompts(spec,{'openai_key':'fake'},job['id'])
        self.assertEqual(req.call_count,1)

    def test_ai_public_alias_generated_once_and_desktop_preset_import_keeps_keys(self):
        spec=state.Spec(provider='elevenlabs',voice_id='new-voice',provider_voice_name='Provider Name',voice_gender='female')
        state.save_settings(state.Settings(secret_updates={'openai_key':'fake','elevenlabs_key':'fake'}))
        with patch.object(engine,'request',side_effect=[{'character_limit':10000}, {'output_text':'{"name":"Boróka"}'}]) as req:
            self.assertEqual(engine.alias(spec),'Boróka')
            self.assertEqual(engine.alias(spec),'Boróka')
        self.assertEqual(req.call_count,2)
        preset={'schema':'anthbot-selection-preset-v5','tts_provider_id':'elevenlabs','openai_model':'gpt-4o-mini',
                'text_style_id':'funny','elevenlabs_credit_limit':4321,'catalog':[{'locale':'hu-HU','native_name':'Magyar',
                    'translation_target':'hu','voices':['new-voice'],'voice_info':[{'voice_id':'new-voice','name':'Provider Name','gender':'female'}]}]}
        r=self.api('/preset-import','POST',files={'file':('selection_preset.json',json.dumps(preset))})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(state.credentials(),{'openai_key':'fake','elevenlabs_key':'fake'})
        saved=state.settings();self.assertEqual(saved['credit_limit'],4321)
        self.assertEqual(saved['selections'][0]['text_style'],'funny')
        with patch.object(engine,'request',side_effect=AssertionError('Prepared Hungarian translated again')):
            base=text.base_document(state.Spec.model_validate(saved['selections'][0]),{},self.job()['id'])
        self.assertEqual(base['prompts'],state.load_asset('prompts/hu-HU.json')['prompts'])

    def test_real_review_republishes_same_identity_keeps_price_and_entitlement_and_one_audio_change(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        job=self.job(spec)
        with patch.object(engine,'request',side_effect=self.answer):
            self.run_fast(job)
        result=self.api('/jobs/'+job['id']+'/publish','POST').json()
        directory=state.job_dir(job['id'])
        base=state.load_asset('prompts/hu-HU.json')
        old={**base,'prompts':[{**p,'text':p['text'] if text.critical(p['file']) else 'Korábbi kerti móka '+p['file']} for p in base['prompts']]}
        state.atomic(directory/'prompts.json',state.canonical(old).encode())
        (directory/'text-approval.json').unlink()
        manifest=json.loads((directory/'manifest.json').read_text())
        manifest.update(schema_version=1,prompt_sha256=state.digest(old));manifest.pop('text_verification',None)
        state.atomic(directory/'manifest.json',state.canonical(manifest).encode())
        registry=core._uploaded_voice_pack_registry();record=registry['packs'][0]
        record.update(access='paid',price_amount=799,store_hidden=False)
        core._write_uploaded_voice_registry(registry)
        stable=record['community_id']
        review.scan_and_quarantine();review.start_review(job['id'])
        def qa(url,payload=None,headers=None,**kw):
            self.assertIn('QA editor',payload['input'])
            result=self.answer(url,payload,headers,**kw)
            parsed=json.loads(result['output'][0]['content'][0]['text'])
            for p in parsed['prompts']:
                if p['file']=='A004.mp3':
                    p['corrected_text']='Kijavított kerti móka A004'
            result['output'][0]['content'][0]['text']=json.dumps(parsed)
            return result
        with patch.object(engine,'request',side_effect=qa),patch.object(engine,'probe',side_effect=self.probe),\
             patch.object(engine,'normalize',side_effect=self.normalize),\
             patch.object(state,'credentials',return_value={'openai_key':'fake'}),\
             patch.object(engine,'audio_request',return_value=(self.audio+b'\0'*100,True)) as audio,\
             patch.object(worker.time,'sleep'):
            worker.main()
            self.assertEqual(audio.call_count,1)
        repaired=core._uploaded_voice_pack_registry()['packs'][0]
        self.assertEqual(repaired['community_id'],stable)
        self.assertEqual(repaired['access'],'paid');self.assertEqual(repaired['price_amount'],799)
        self.assertTrue(repaired['store_hidden']);self.assertNotEqual(repaired['id'],record['id'])
        self.assertEqual(review.review_state(job['id'])['status'],'repaired')
        backup=self.api('/jobs/'+job['id']+'/review-backup.zip')
        self.assertEqual(backup.status_code,200)
        with zipfile.ZipFile(io.BytesIO(backup.content)) as z:
            self.assertTrue(any(n.endswith('/store-pack.tar.gz') for n in z.namelist()))
        import store_api
        resolved=store_api._resolve_order_pack({'pack_id':record['id'],'community_id':stable})
        self.assertEqual(resolved['id'],repaired['id'])

    def test_qa_of_one_edited_line_reuses_other_200_row_results(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        job=self.job(spec);base=state.load_asset('prompts/hu-HU.json')
        rows=[{'file':p['file'],'source':p['text'],'candidate':p['text'] if text.critical(p['file']) else 'Kerti móka '+p['file'],
               'critical':text.critical(p['file']),'source_review_required':False} for p in base['prompts']]
        with patch.object(engine,'request',side_effect=self.answer) as req:
            text.responses_rows(rows,text.instruction('New-StyleVerifyInstruction',spec,rows),spec,{'openai_key':'fake'},job['id'],qa=True)
            changed=[{**p,'candidate':'Javított kerti móka'} if p['file']=='A004.mp3' else p for p in rows]
            result=text.responses_rows(changed,text.instruction('New-StyleVerifyInstruction',spec,changed),spec,{'openai_key':'fake'},job['id'],qa=True)
            self.assertEqual(req.call_count,2)
            second=json.loads(req.call_args.args[1]['input'].splitlines()[-1])
            self.assertEqual([p['file'] for p in second],['A004.mp3'])
            self.assertEqual(len(result),201)

    def test_legacy_style_cache_gets_qa_only_and_new_text_model_reuses_verified_script(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        base=state.load_asset('prompts/hu-HU.json')
        old={**base,'prompts':[{**p,'text':p['text'] if text.critical(p['file']) else 'Korábbi kerti móka '+p['file']} for p in base['prompts']]}
        state.atomic(engine.prompt_path(spec,styled=True,base=base,legacy=True),state.canonical(old).encode())
        with patch.object(engine,'request',side_effect=self.answer) as req:
            doc=engine.load_prompts(spec,{'openai_key':'fake'},self.job(spec)['id'])
            self.assertEqual(req.call_count,1)
            self.assertIn('QA editor',req.call_args.args[1]['input'])
        changed=spec.model_copy(update={'translation_model':'another-text-model','voice_id':'onyx'})
        with patch.object(engine,'request',side_effect=AssertionError('Text model change rebilled verified script')):
            reused=engine.load_prompts(changed,{},self.job(changed)['id'])
        self.assertEqual(reused,doc)
