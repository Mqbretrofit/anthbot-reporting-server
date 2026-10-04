"""Shared, idempotent Hangbolt upload for admin requests and the local worker."""
from __future__ import annotations

import asyncio
import json
from fastapi import HTTPException, Request, UploadFile
import app as core
import voice_builder_state as state


class ReviewRequired(HTTPException):
    """A controlled local proof failure, safe to show without provider details."""
    def __init__(self, detail):
        super().__init__(409, detail)


def approved_pack_text(jid, manifest):
    from voice_builder_text import require_approval
    from voice_builder_engine import validate_prompts
    doc = validate_prompts(json.loads((state.job_dir(jid)/'prompts.json').read_text()))
    try:
        approval = require_approval(jid, doc)
    except RuntimeError as err:
        raise ReviewRequired(str(err)) from None
    if manifest.get('prompt_sha256') != state.digest(doc) or manifest.get('text_verification') != approval:
        raise ReviewRequired('A csomag nem az ellenőrzött szövegkönyv alapján készült; újraépítés szükséges')
    return approval


def publish_job(jid, request=None):
    # The HTTP route authorizes callers. The worker publishes only validated
    # jobs already created by that admin route, without any network loopback.
    if request is None:
        request = Request({'type':'http','method':'POST','scheme':'http',
                           'server':('localhost',8080),'path':'/','root_path':'',
                           'query_string':b'','headers':[]})
    job = state.get_job(jid)
    if job['status'] != 'completed' or job['mode'] != 'build':
        raise HTTPException(409,'Csak kész, ellenőrzött teljes csomag tölthető fel a Hangboltba')
    if job['published']:
        return job['published']
    directory = state.job_dir(jid)
    manifest = json.loads((directory / 'manifest.json').read_text())
    from voice_builder_engine import voice_set_template
    approval = approved_pack_text(jid, manifest)
    # File mutation/publishing uses the existing upload path and server version allocator.
    import hashlib
    if hashlib.sha256((directory / 'pack.tar.gz').read_bytes()).hexdigest() != manifest['sha256']:
        raise HTTPException(409,'A csomag ellenőrzőösszege megváltozott')
    meta = manifest['public_store']
    fields = {k:meta[k] for k in ('language','language_code','community_id','variant_id','variant_name',
                                 'voice_gender','locale','voice_display_name','style','style_name',
                                 'delivery','delivery_name','character_effect','character_effect_name','tier','license_required')}
    fields.update(version='',preview_files=json.dumps(meta['preview_files']),technical_slot=core.COMMUNITY_TECHNICAL_SLOT,
                  english_name=core.COMMUNITY_TECHNICAL_LANGUAGE,sex=core.COMMUNITY_TECHNICAL_SEX,
                  music_package=core.COMMUNITY_MUSIC_PACKAGE,models=json.dumps(meta['compatible_models']))
    # Serialize worker and admin uploads across processes.
    with state.locked('publish.lock'):
        previous = state.get_job(jid)['published']
        if previous:
            return previous
        existing = core._uploaded_voice_pack_registry().get('packs', [])
        bundled = core._bundled_voice_pack_registry().get('packs', [])
        intent_path = directory/'review-intent.json'
        if intent_path.exists():
            intent = json.loads(intent_path.read_text())
            current = next((x for x in existing if core._voice_pack_community_id(x) == meta['community_id'].casefold()),None)
            if intent.get('expected_md5') and current and current.get('music_md5') not in (intent['expected_md5'],manifest['music_md5']):
                raise HTTPException(409,'Időközben másik csomag került fel ehhez a hanghoz. A korábbi javítás nem írja felül az új feltöltést.')
        known = any(core._voice_pack_community_id(x) == meta['community_id'].casefold()
                    for x in existing + bundled if isinstance(x,dict))
        # Core upload replaces the registry revision and removes its old file.
        # Retain the previous paid package and metadata before that replacement.
        for old in existing:
            if core._voice_pack_community_id(old) == meta['community_id'].casefold():
                backup = state.root()/'store-revisions'/state.digest({'id':old['id'],'md5':old.get('music_md5','')})
                state.atomic(backup/'registry-record.json',state.canonical(old).encode())
                filename = old.get('filename','')
                from pathlib import Path
                if filename and Path(filename).name == filename and (core._voice_pack_dir()/filename).exists():
                    state.atomic(backup/'pack.tar.gz',(core._voice_pack_dir()/filename).read_bytes())
        with (directory / 'pack.tar.gz').open('rb') as f:
            result = asyncio.run(core.upload_voice_pack(request=request,
                file=UploadFile(f,filename=meta['community_id']+'.tar.gz'), **fields))
        if not known or approval.get('review_required_count',0):
            registry = core._uploaded_voice_pack_registry()
            for record in registry.get('packs', []):
                if record['id'] == result['pack']['id']:
                    record['store_hidden'] = True
                    result['pack']['store_hidden'] = True
            core._write_uploaded_voice_registry(registry)
        manifest['assigned_version'] = result['assigned_version']
        manifest['server_pack_id'] = result['pack']['id']
        manifest['anthbot_voice_set_template'] = voice_set_template(state.Spec.model_validate(job['spec']),
            version=result['assigned_version'],url=result['pack'].get('music_url',''),md5=manifest['music_md5'])
        state.atomic(directory / 'manifest.json', state.canonical(manifest).encode())
        state.atomic(directory / 'voice_set.json',state.canonical(manifest['anthbot_voice_set_template']).encode())
        state.update(jid,published=state.canonical(result),error='')
        from voice_builder_review import replacement_published
        replacement_published(jid,result)
        state.log(jid,'Hangbolt feltöltés kész; szerver által kiosztott verzió: '+result['assigned_version'])
        return result
