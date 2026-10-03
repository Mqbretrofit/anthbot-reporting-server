from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import tarfile
import zipfile
from fastapi.testclient import TestClient
import asgi
import app as core
import voice_builder_state as state
import voice_builder_engine as engine
import voice_builder_api as api_module
import voice_builder_worker as worker


class VoiceBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir=tempfile.TemporaryDirectory()
        cls.fixture=Path(cls.fixture_dir.name)/'tone.mp3'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=1',
            '-ac','1','-ar','16000','-codec:a','libmp3lame','-b:a','32k','-write_xing','0',
            '-id3v2_version','0','-write_id3v1','0',str(cls.fixture)],check=True)
        cls.audio=cls.fixture.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'ANTHBOT_DB_PATH':str(Path(self.temp.name)/'report.sqlite3'),
            'ANTHBOT_VOICE_BUILDER_DIR':str(Path(self.temp.name)/'builder'),
            'ANTHBOT_VOICE_PACK_DIR':str(Path(self.temp.name)/'voice_packs'),
            'ANTHBOT_ADMIN_TOKEN':'test-admin','ANTHBOT_VOICE_BUILDER_NO_WORKER':'1'})
        self.env.start()
        self.ctx=TestClient(asgi.app,base_url='https://testserver')
        self.client=self.ctx.__enter__()
        self.headers={'Authorization':'Bearer test-admin','X-ANTHBOT-Builder':'1'}
        self.prefix='/api/anthbot/admin/voice-builder'
        self.spec=state.Spec(provider='openai',voice_id='coral',voice_gender='female')

    def tearDown(self):
        self.ctx.__exit__(None,None,None)
        self.env.stop()
        self.temp.cleanup()

    def api(self,path,method='GET',**kwargs):
        return self.client.request(method,self.prefix+path,headers=self.headers,**kwargs)

    def job(self,spec=None,mode='build',limit=10000):
        jid=state.create_jobs([spec or self.spec],mode,limit)[0]
        state.update(jid,status='running')
        state.job_dir(jid).mkdir(parents=True,exist_ok=True)
        return state.get_job(jid)

    def probe(self,path):
        if path.exists() and path.stat().st_size>=500:
            return {'codec_name':'mp3','sample_rate':'16000','channels':1,'bit_rate':'32000'}
        return None

    def normalize(self,source,dest,*a,**kw):
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,dest)

    def run_fast(self,job):
        with patch.object(engine,'probe',side_effect=self.probe),patch.object(engine,'normalize',side_effect=self.normalize),\
             patch.object(engine,'audio_request',return_value=(self.audio,True)) as audio:
            engine.run_job(job)
            return audio.call_count

    def test_auth_same_origin_and_custom_write_header(self):
        for path in ['/catalog','/settings','/jobs','/style-rules','/voices/openai']:
            self.assertEqual(self.client.get(self.prefix+path).status_code,401)
        self.assertEqual(self.client.get('/dashboard/voice-builder').status_code,401)
        body={'selections':[self.spec.model_dump()]}
        self.assertEqual(self.client.post(self.prefix+'/jobs',headers={'Authorization':'Bearer test-admin'},json=body).status_code,403)
        self.assertEqual(self.client.post(self.prefix+'/jobs',headers={**self.headers,'Origin':'https://evil.test'},json=body).status_code,403)
        self.assertEqual(self.api('/jobs','POST',json=body).status_code,201)
        self.assertIn('no-store',self.api('/settings').headers['cache-control'])

    def test_catalog_and_dashboard_preserve_desktop_choices(self):
        r=self.client.get('/dashboard/voice-builder',headers=self.headers)
        self.assertEqual(r.status_code,200)
        self.assertIn('Korábbi Builder cache',r.text)
        cat=self.api('/catalog').json()
        self.assertEqual(len(cat['languages']),9)
        self.assertEqual(len(cat['effects']),9)
        self.assertEqual(len(self.api('/voices/ha_cloud').json()['items']),18)
        self.client.post('/dashboard/login',data={'token':'test-admin'})
        self.assertIn('Voice Builder',self.client.get('/dashboard').text)

    def test_secrets_are_encrypted_not_returned_and_survive_settings_changes(self):
        secret='sk-test-private-value'
        r=self.api('/settings','PUT',json={'selections':[self.spec.model_dump()],
            'credit_limit':1234,'secret_updates':{'openai_key':secret},'draft':{'locales':['hu-HU']}})
        self.assertEqual(r.status_code,200,r.text)
        self.assertNotIn(secret,r.text)
        self.assertNotIn(secret,(state.root()/'secrets.enc').read_text())
        self.assertNotIn(secret,(state.root()/'settings.json').read_text())
        self.assertEqual(state.credentials()['openai_key'],secret)
        self.assertEqual((state.root()/'encryption.key').stat().st_mode&0o777,0o600)
        self.api('/settings','PUT',json={'selections':[self.spec.model_dump()]})
        self.assertEqual(state.credentials()['openai_key'],secret)
        self.api('/settings','PUT',json={'secret_updates':{'openai_key':''}})
        self.assertFalse(self.api('/settings').json()['configured']['openai_key'])

    def test_invalid_specs_and_secret_drafts_rejected(self):
        for change in [{'locale':'../../x'},{'text_style':'bad'},{'voice_id':'x/../y'},{'ha_url':'https://user:secret@ha.test'}]:
            self.assertEqual(self.api('/jobs','POST',json={'selections':[{**self.spec.model_dump(),**change}]}).status_code,422)
        self.assertEqual(self.api('/settings','PUT',json={'draft':{'custom_voices':[{'api_key':'secret'}]}}).status_code,422)

    def test_real_ffmpeg_format_and_fixed_original_hashes(self):
        self.assertTrue(engine.spoken_valid(self.fixture))
        dest=Path(self.temp.name)/'converted.mp3'
        engine.normalize(self.fixture,dest,self.spec)
        self.assertTrue(engine.spoken_valid(dest))
        for name,sha in state.FIXED.items():
            p=state.ASSETS/'base-assets/genie_slot3_community'/name
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),sha)
            self.assertTrue(engine.raw_valid(p))

    def test_preview_to_full_reuses_tts_and_builds_204_deterministic_archive(self):
        self.assertEqual(self.run_fast(self.job(mode='preview')),2)
        job=self.job()
        unique=len({p['text'] for p in state.load_asset('prompts/hu-HU.json')['prompts']})
        self.assertEqual(self.run_fast(job),unique-2)
        d=state.job_dir(job['id'])
        first=(d/'pack.tar.gz').read_bytes()
        state.update(job['id'],status='running')
        self.assertEqual(self.run_fast(state.get_job(job['id'])),0)
        self.assertEqual((d/'pack.tar.gz').read_bytes(),first)
        with tarfile.open(d/'pack.tar.gz') as tar:
            self.assertEqual(len(tar.getmembers()),204)
            self.assertEqual(set(tar.getnames()),engine.expected_names()|set(state.FIXED))
            for name,sha in state.FIXED.items():
                self.assertEqual(hashlib.sha256(tar.extractfile(name).read()).hexdigest(),sha)
        manifest=json.loads((d/'manifest.json').read_text())
        self.assertEqual(manifest['music_md5'],hashlib.md5(first).hexdigest())
        self.assertEqual(manifest['public_store']['preview_files'],['A004.mp3','A005.mp3'])
        self.assertNotIn('coral',(d/'catalog.json').read_text())
        self.assertNotIn('voice_id',(d/'catalog.json').read_text())

    def test_pause_resume_reuses_request_completed_before_pause(self):
        job=self.job(mode='preview')
        def pause(sig,keys):
            state.update(job['id'],status='paused')
            return self.audio,True
        with patch.object(engine,'probe',side_effect=self.probe),patch.object(engine,'audio_request',side_effect=pause):
            with self.assertRaises(engine.Paused):
                engine.run_job(job)
        self.assertEqual(self.api('/jobs/'+job['id']+'/resume','POST',json={}).status_code,200)
        state.update(job['id'],status='running')
        self.assertEqual(self.run_fast(state.get_job(job['id'])),1)

    def test_error_signatures_and_entitlement_identity_remain_stable(self):
        a=state.Spec(provider='elevenlabs',voice_id='abc')
        b=a.model_copy(update={'text_style':'wild_funny','delivery':'angry'})
        self.assertEqual(engine.tts_signature(a,'hiba','E001.mp3'),engine.tts_signature(b,'hiba','E001.mp3'))
        self.assertNotEqual(engine.tts_signature(a,'hello','A004.mp3'),engine.tts_signature(b,'hello','A004.mp3'))
        self.assertEqual(engine.tts_signature(a,'x','A004.mp3')['model_id'],'eleven_flash_v2_5')
        self.assertEqual(engine.tts_signature(b,'x','A004.mp3')['model_id'],'eleven_v4')
        self.assertEqual(engine.metadata(a)['community_id'],engine.metadata(a.model_copy(update={'voice_display_name':'Új név'}))['community_id'])

    def test_batch_budget_and_actual_model_rates_prevent_tts_start(self):
        ids=state.create_jobs([self.spec,self.spec],'build',10)
        batch=state.get_job(ids[0])['batch_id']
        state.reserve(batch,6)
        with self.assertRaises(RuntimeError):
            state.reserve(batch,5)
        with state.db() as c:
            self.assertEqual(c.execute('SELECT reserved FROM budgets WHERE id=?',(batch,)).fetchone()[0],6)
        spec=state.Spec(provider='elevenlabs',voice_id='abc')
        job=self.job(spec,mode='preview',limit=1)
        with patch.object(engine,'request',return_value=[{'model_id':'eleven_flash_v2_5','model_rates':{'character_cost_multiplier':2}}]) as req:
            with self.assertRaisesRegex(RuntimeError,'meghaladja'):
                engine.preflight(job,spec,state.load_asset('prompts/hu-HU.json'),{'elevenlabs_key':'test'})
            self.assertEqual(req.call_count,1)
        estimate=json.loads((state.job_dir(job['id'])/'estimate.json').read_text())
        texts=[p['text'] for p in state.load_asset('prompts/hu-HU.json')['prompts'] if p['file'] in ('A004.mp3','A005.mp3')]
        self.assertEqual(estimate['estimated_credits'],sum(len(t) for t in texts)*2)

    def test_unknown_models_stop_before_billing(self):
        spec=state.Spec(provider='elevenlabs',voice_id='abc',hybrid=False,model='missing')
        job=self.job(spec,mode='preview')
        with patch.object(engine,'request',return_value=[{'model_id':'eleven_v3'}]):
            with self.assertRaisesRegex(RuntimeError,'nem kínálja'):
                engine.preflight(job,spec,state.load_asset('prompts/hu-HU.json'),{'elevenlabs_key':'x'})

    def test_styles_rewrite_104_rows_keep_97_errors_exact_and_reuse_text_cache(self):
        spec=self.spec.model_copy(update={'text_style':'funny'})
        job=self.job(spec)
        def rewrite(rows,*a):
            return [{'file':p['file'],'text':'Kertkaland! '+p['file']} for p in rows]
        with patch.object(engine,'chat_rows',side_effect=rewrite) as chat:
            doc=engine.load_prompts(spec,{},job['id'])
        source={p['file']:p['text'] for p in state.load_asset('prompts/hu-HU.json')['prompts']}
        self.assertEqual(sum(p['file'].startswith('E') for p in doc['prompts']),97)
        self.assertEqual(len(chat.call_args[0][0]),104)
        for p in doc['prompts']:
            if p['file'].startswith('E'):
                self.assertEqual(p['text'],source[p['file']])
            else:
                self.assertNotEqual(p['text'],source[p['file']])
        with patch.object(engine,'chat_rows',side_effect=AssertionError('Repeat translation')):
            engine.load_prompts(spec,{},self.job(spec)['id'])
        self.assertFalse(engine.fresh_text('Hello world.','Hello world. Ha ha!'))

    def test_text_chunks_survive_partial_translation_failure(self):
        job=self.job()
        rows=state.load_asset('prompts/en-US.json')['prompts'][:21]
        count=0
        def chat(url,body,*a,**kw):
            nonlocal count
            count+=1
            if count==2:
                raise engine.ProviderError(503)
            return {'choices':[{'message':{'content':body['messages'][1]['content']}}]}
        with patch.object(engine,'request',side_effect=chat):
            with self.assertRaises(engine.ProviderError):
                engine.chat_rows(rows,'Translate',self.spec,{'openai_key':'x'},job['id'])
        with patch.object(engine,'request',return_value={'choices':[{'message':{'content':json.dumps({'prompts':rows[20:]})}}]}) as req:
            result=engine.chat_rows(rows,'Translate',self.spec,{'openai_key':'x'},job['id'])
            self.assertEqual(req.call_count,1)
            self.assertEqual(len(result),21)

    def test_worker_recovers_running_jobs_and_has_single_queue_owner(self):
        job=self.job()
        with patch.object(worker,'run_job',side_effect=lambda j:state.update(j['id'],status='completed')),patch.object(worker.time,'sleep'):
            worker.main()
        self.assertEqual(state.get_job(job['id'])['status'],'completed')
        job=self.job()
        state.update(job['id'],status='queued')
        with state.locked('worker.lock'),patch.object(worker,'run_job') as run:
            worker.main()
            run.assert_not_called()
        self.assertEqual(state.get_job(job['id'])['status'],'queued')

    def test_foreign_ha_audio_url_cannot_receive_token(self):
        sig=engine.tts_signature(state.Spec(provider='ha_cloud',voice_id='NoemiNeural',ha_url='https://ha.test'),'hi','A004.mp3')
        with patch.object(engine,'request',return_value={'url':'https://evil.test/audio.mp3'}) as req:
            with self.assertRaisesRegex(RuntimeError,'érvénytelen'):
                engine.audio_request(sig,{'ha_token':'secret'})
            self.assertEqual(req.call_count,1)
        self.assertIsNone(engine.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test'))

    def test_publish_uses_existing_version_allocator_hidden_draft_and_is_idempotent(self):
        job=self.job()
        self.run_fast(job)
        r=self.api('/jobs/'+job['id']+'/publish','POST')
        self.assertEqual(r.status_code,200,r.text)
        result=r.json()
        self.assertTrue(result['pack']['store_hidden'])
        self.assertRegex(result['assigned_version'],r'^1\.2\.\d+$')
        self.assertEqual(self.api('/jobs/'+job['id']+'/publish','POST').json(),result)
        self.assertEqual(len(core._uploaded_voice_pack_registry()['packs']),1)
        manifest=json.loads((state.job_dir(job['id'])/'manifest.json').read_text())
        self.assertEqual(manifest['assigned_version'],result['assigned_version'])

    def test_existing_store_price_visibility_and_version_survive_builder_republish(self):
        first=self.job()
        self.run_fast(first)
        result=self.api('/jobs/'+first['id']+'/publish','POST').json()
        registry=core._uploaded_voice_pack_registry()
        registry['packs'][0].update(access='paid',price_amount=799,store_hidden=False)
        core._write_uploaded_voice_registry(registry)
        second=self.job()
        self.run_fast(second)
        r=self.api('/jobs/'+second['id']+'/publish','POST')
        self.assertEqual(r.status_code,200,r.text)
        record=core._uploaded_voice_pack_registry()['packs'][0]
        self.assertEqual(record['access'],'paid')
        self.assertEqual(record['price_amount'],799)
        self.assertFalse(record['store_hidden'])
        self.assertEqual(r.json()['assigned_version'],result['assigned_version'])
        self.assertEqual(len(core._uploaded_voice_pack_registry()['packs']),1)

    def test_audio_import_rejects_traversal_duplicate_unknown_and_wrong_fixed_sound(self):
        for names in [['../A004.mp3'],['A004.mp3','A004.mp3'],['evil.sh'],['A001.mp3']]:
            raw=io.BytesIO()
            with zipfile.ZipFile(raw,'w') as z:
                for name in names:
                    z.writestr(name,self.audio)
            with self.assertRaises(ValueError):
                api_module.read_audio_archive(raw.getvalue())

    def test_full_audio_import_needs_no_paid_calls(self):
        job=self.job()
        state.update(job['id'],status='paused')
        doc=state.load_asset('prompts/hu-HU.json')
        self.assertEqual(self.api('/jobs/'+job['id']+'/import-prompts','POST',files={'file':('p.json',json.dumps(doc))}).status_code,200)
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            for name in engine.expected_names():
                z.writestr(name,self.audio)
        with patch.object(engine,'probe',side_effect=self.probe):
            r=self.api('/jobs/'+job['id']+'/import-audio','POST',files={'file':('audio.zip',raw.getvalue())})
            self.assertEqual(r.status_code,200,r.text)
            state.update(job['id'],status='running')
            with patch.object(engine,'audio_request',side_effect=AssertionError('Must reuse')):
                engine.run_job(state.get_job(job['id']))
        self.assertEqual(state.get_job(job['id'])['status'],'completed')
        self.assertEqual(self.api('/jobs/'+job['id']+'/files/secret.enc').status_code,404)
        self.assertEqual(self.api('/jobs/not-an-id').status_code,404)

    def test_desktop_cache_import_preserves_audio_prompts_aliases_ignores_secrets(self):
        spec=state.Spec(provider='elevenlabs',voice_id='abc')
        name=engine.raw_cache_path(engine.tts_signature(spec,'hello','A004.mp3')).name
        raw=io.BytesIO()
        doc=state.load_asset('prompts/hu-HU.json')
        with zipfile.ZipFile(raw,'w') as z:
            z.writestr('ANTHBOT/tts-cache/elevenlabs/'+name[:2]+'/'+name,self.audio)
            z.writestr('ANTHBOT/prompt-cache/by-target/hu-HU.json',json.dumps(doc))
            z.writestr('ANTHBOT/voice-aliases.json',json.dumps({'aliases':[{'locale':'hu-HU','voice_id':'abc','display_name':'Réka'}]}))
            z.writestr('ANTHBOT/secrets.json','WINDOWS_SECRET')
        r=self.api('/cache-import','POST',files={'file':('cache.zip',raw.getvalue())})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json(),{'audio':1,'prompts':1,'aliases':1,'skipped':1})
        self.assertEqual(engine.alias(spec),'Réka')
        self.assertTrue(engine.raw_valid(engine.raw_cache_path(engine.tts_signature(spec,'hello','A004.mp3'))))
        with patch.object(engine,'chat_rows',side_effect=AssertionError('Cache must suffice')):
            loaded=engine.load_prompts(spec,{},self.job(spec)['id'])
        self.assertEqual(loaded['prompts'],doc['prompts'])
        self.assertFalse((state.root()/'secrets.json').exists())

    def test_import_waits_for_in_flight_worker_request_to_finish(self):
        job=self.job()
        state.update(job['id'],status='paused')
        with state.locked(job['id']+'.lock'):
            r=self.api('/jobs/'+job['id']+'/import-prompts','POST',files={'file':('p.json','{}')})
        self.assertEqual(r.status_code,409)

if __name__=='__main__':
    unittest.main()
