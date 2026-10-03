"""Shared, idempotent Hangbolt upload for admin requests and the local worker."""
from __future__ import annotations

import asyncio
import json
from fastapi import HTTPException, Request, UploadFile
import app as core
import voice_builder_state as state


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
        known = any(core._voice_pack_community_id(x) == meta['community_id'].casefold()
                    for x in existing + bundled if isinstance(x,dict))
        with (directory / 'pack.tar.gz').open('rb') as f:
            result = asyncio.run(core.upload_voice_pack(request=request,
                file=UploadFile(f,filename=meta['community_id']+'.tar.gz'), **fields))
        if not known:
            registry = core._uploaded_voice_pack_registry()
            for record in registry.get('packs', []):
                if record['id'] == result['pack']['id']:
                    record['store_hidden'] = True
                    result['pack']['store_hidden'] = True
            core._write_uploaded_voice_registry(registry)
        manifest['assigned_version'] = result['assigned_version']
        manifest['server_pack_id'] = result['pack']['id']
        state.atomic(directory / 'manifest.json', state.canonical(manifest).encode())
        state.update(jid,published=state.canonical(result),error='')
        state.log(jid,'Hangbolt feltöltés kész; szerver által kiosztott verzió: '+result['assigned_version'])
        return result
