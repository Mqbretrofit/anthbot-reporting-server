"""Admin-only web UI and API for the persistent server Voice Builder."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tarfile
import zipfile
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

import app as core
import voice_builder_state as state
import voice_builder_engine as engine


def require_builder_admin(request: Request):
    core.require_admin(request, request.headers.get('authorization'))
    if request.method not in ('GET', 'HEAD'):
        # Dashboard sessions are deliberately SameSite=None for HA embedding;
        # reject cross-origin writes and require a non-simple custom header.
        if request.headers.get('x-anthbot-builder') != '1':
            raise HTTPException(403, 'Hiányzó admin kérésazonosító')
        origin = request.headers.get('origin')
        if origin:
            parsed = urlsplit(origin)
            if parsed.netloc != request.headers.get('host') or parsed.scheme not in ('http','https'):
                raise HTTPException(403, 'Idegen eredetű admin kérés')


router = APIRouter(dependencies=[Depends(require_builder_admin)])
PREFIX = '/api/anthbot/admin/voice-builder'


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selections: list[state.Spec] = Field(min_length=1, max_length=4096)
    mode: str = Field(default='build', pattern=r'^(build|preview|import|text|generate)$')
    credit_limit: int = Field(default=10000, ge=0, le=10000000)
    skip_completed: bool = True
    continue_on_error: bool = True
    upload_enabled: bool = True


class Resume(BaseModel):
    model_config = ConfigDict(extra='forbid')
    credit_limit: int | None = Field(default=None, ge=0, le=10000000)


def job_or_404(jid):
    try:
        return state.get_job(jid)
    except (ValueError, KeyError):
        raise HTTPException(404, 'Nincs ilyen feladat') from None


@router.get('/dashboard/voice-builder', response_class=HTMLResponse)
def page():
    return HTMLResponse(Path(__file__).with_name('voice_builder.html').read_text())


@router.get(PREFIX + '/catalog')
def catalog():
    return {'builder_version':'7.20.11 / server-1.0',
            'languages':state.load_asset('languages.json')['languages'],
            'providers':state.load_asset('tts-providers.json'),
            'text_styles':state.load_asset('text-styles.json')['styles'],
            'delivery_styles':state.load_asset('delivery-styles.json')['styles'],
            'effects':state.load_asset('character-effects.json')['effects'],
            'legacy_themes':state.load_asset('themes.json')['themes']}


@router.get(PREFIX + '/settings')
def settings():
    return state.settings()


@router.put(PREFIX + '/settings')
def save_settings(payload: state.Settings):
    return state.save_settings(payload)


@router.get(PREFIX + '/voices/{provider}')
def voices(provider: str, query: str = '', licensed_only: bool = True):
    if provider not in ('elevenlabs','openai','ha_cloud','fish_audio'):
        raise HTTPException(422, 'Ismeretlen szolgáltató')
    try:
        return {'items':engine.available_voices(provider,query[:200],licensed_only)}
    except RuntimeError as err:
        raise HTTPException(502, str(err)) from None


@router.get(PREFIX + '/elevenlabs-models')
def elevenlabs_models():
    keys = state.credentials()
    if not keys.get('elevenlabs_key'):
        raise HTTPException(422, 'Előbb mentsd el az ElevenLabs API-kulcsot')
    try:
        data = engine.request('https://api.elevenlabs.io/v1/models', headers={'xi-api-key':keys['elevenlabs_key']}, json_result=True)
        return {'items':[{'id':x['model_id'],'name':x.get('name',x['model_id'])}
                          for x in data if x.get('can_do_text_to_speech')]}
    except RuntimeError as err:
        raise HTTPException(502, str(err)) from None


@router.get(PREFIX + '/jobs')
def jobs():
    from voice_builder_review import upload_recovery
    items = state.jobs()
    for job in items:
        job['upload_recovery'] = upload_recovery(job)
    if any(x['status'] in ('running','queued') for x in items):
        state.launch_worker()
    return {'items':items}


@router.post(PREFIX + '/jobs', status_code=201)
def start(payload: Start):
    specs = list({state.digest(s.model_dump()):s for s in payload.selections}.values())
    specs = [s.model_copy(update={'voice_display_name': engine.alias(s)}) if not s.voice_display_name.strip() else s for s in specs]
    skipped = []
    if payload.skip_completed and payload.mode != 'import':
        def signature(spec):
            return state.digest(spec.model_dump(exclude={'voice_display_name','provider_voice_name','language'}))
        with state.db() as c:
            previous = [state.public_job(r) for r in c.execute("SELECT * FROM jobs WHERE status='completed' AND mode=? ORDER BY created DESC",(payload.mode,))]
        complete = {}
        for old in previous:
            try:
                directory = state.job_dir(old['id'])
                doc = engine.validate_prompts(json.loads((directory/'prompts.json').read_text()))
                from voice_builder_text import require_approval
                require_approval(old['id'],doc)
                if payload.mode == 'build':
                    manifest = json.loads((directory/'manifest.json').read_text())
                    if manifest['prompt_sha256'] != state.digest(doc) or not (directory/'pack.tar.gz').exists():
                        continue
                complete.setdefault(signature(state.Spec.model_validate(old['spec'])),old['id'])
            except (OSError,ValueError,RuntimeError,KeyError):
                continue
        selected = []
        for spec in specs:
            prior = complete.get(signature(spec))
            if prior:
                skipped.append(prior)
            else:
                selected.append(spec)
        specs = selected
    ids = state.create_jobs(specs, 'build' if payload.mode == 'import' else payload.mode, payload.credit_limit,
        continue_on_error=payload.continue_on_error,upload_enabled=payload.upload_enabled) if specs else []
    if payload.mode == 'import':
        for jid in ids:
            state.set_status(jid, 'paused')
    else:
        state.launch_worker()
    return {'items':[state.get_job(jid) for jid in ids+skipped], 'skipped_completed':skipped}


@router.get(PREFIX + '/jobs/{jid}')
def detail(jid: str, after: int = 0):
    job = job_or_404(jid)
    with state.db() as c:
        logs = [dict(r) for r in c.execute('SELECT * FROM logs WHERE job_id=? AND id>? ORDER BY id LIMIT 500', (jid,after))]
        budget = c.execute('SELECT credit_limit,reserved FROM budgets WHERE id=?', (job['batch_id'],)).fetchone()
    estimate_path = state.job_dir(jid) / 'estimate.json'
    return {'job':job,'logs':logs,'budget':dict(budget),
            'estimate':json.loads(estimate_path.read_text()) if estimate_path.exists() else None}


@router.post(PREFIX + '/jobs/{jid}/pause')
def pause(jid: str):
    job_or_404(jid)
    try:
        state.set_status(jid,'paused')
    except ValueError as err:
        raise HTTPException(409,str(err)) from None
    return state.get_job(jid)


@router.post(PREFIX + '/jobs/{jid}/resume')
def resume(jid: str, payload: Resume):
    job = job_or_404(jid)
    if payload.credit_limit is not None:
        with state.db() as c:
            c.execute('UPDATE budgets SET credit_limit=? WHERE id=?', (payload.credit_limit,job['batch_id']))
    try:
        state.set_status(jid,'queued')
    except ValueError as err:
        raise HTTPException(409,str(err)) from None
    state.launch_worker()
    return state.get_job(jid)


@router.get(PREFIX + '/jobs/{jid}/files/{name}')
def download(jid: str, name: str):
    job_or_404(jid)
    if name in ('pack.tar.gz','manifest.json','catalog.json','prompts.json','estimate.json','voice_set.json',
                'text-review.json','audio-diagnostics.json'):
        path = state.job_dir(jid) / name
    elif name in ('A004.mp3','A005.mp3'):
        path = state.job_dir(jid) / 'audio' / name
    else:
        raise HTTPException(404)
    if not path.is_file():
        raise HTTPException(404, 'Ez a fájl még nem készült el')
    return FileResponse(path, media_type='audio/mpeg' if name.endswith('.mp3') else None,
                        filename=name if not name.endswith('.mp3') else None)


@router.post(PREFIX + '/jobs/{jid}/publish')
async def publish(jid: str, request: Request):
    job_or_404(jid)
    from voice_builder_publish import publish_job
    # The disk lock and existing upload work stay off the ASGI event loop.
    return await asyncio.to_thread(publish_job, jid, request)


@router.post(PREFIX + '/jobs/{jid}/import-prompts')
def import_prompts(jid: str, file: UploadFile = File(...)):
    job_or_404(jid)
    try:
        with state.locked(jid + '.lock', blocking=False):
            return _import_prompts(jid, file)
    except BlockingIOError:
        raise HTTPException(409, 'A folyamatban levő kérés még nem fejeződött be. Próbáld újra később.') from None


def _import_prompts(jid, file):
    job = job_or_404(jid)
    if job['status'] not in ('paused','failed'):
        raise HTTPException(409,'Előbb szüneteltesd a feladatot')
    if (state.job_dir(jid)/'audio').exists():
        raise HTTPException(409,'A hangkészítés után a szövegkönyv már nem cserélhető')
    try:
        raw = file.file.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise ValueError('Túl nagy szövegkönyv')
        doc = engine.validate_prompts(json.loads(raw.decode('utf-8-sig')))
        spec = state.Spec.model_validate(job['spec'])
        locale = doc.get('locale') or doc.get('language_code')
        if locale and locale != spec.locale:
            raise ValueError('A szövegkönyv nyelve nem egyezik a feladattal')
        state.atomic(state.job_dir(jid)/'prompts.json',state.canonical(doc).encode())
    except (ValueError, UnicodeError) as err:
        raise HTTPException(422,str(err)) from None
    return {'imported':201}


@router.post(PREFIX + '/jobs/{jid}/import-audio')
def import_audio(jid: str, file: UploadFile = File(...)):
    job_or_404(jid)
    try:
        with state.locked(jid + '.lock', blocking=False):
            return _import_audio(jid, file)
    except BlockingIOError:
        raise HTTPException(409, 'A folyamatban levő kérés még nem fejeződött be. Próbáld újra később.') from None


def _import_audio(jid, file):
    job = job_or_404(jid)
    if job['status'] not in ('paused','failed'):
        raise HTTPException(409,'Előbb szüneteltesd a feladatot')
    if not (state.job_dir(jid)/'prompts.json').exists():
        raise HTTPException(409,'Előbb importáld a hangokhoz tartozó 201 soros szövegkönyvet')
    raw = file.file.read(64*1024*1024+1)
    if len(raw) > 64*1024*1024:
        raise HTTPException(413,'Túl nagy archívum')
    try:
        files = read_audio_archive(raw)
        directory = state.job_dir(jid)/'audio'
        staged = state.job_dir(jid)/'import-stage'
        staged.mkdir(parents=True,exist_ok=True)
        for name,data in files.items():
            path = staged/name
            state.atomic(path,data)
            if not engine.spoken_valid(path):
                if not engine.raw_valid(path):
                    raise ValueError('Hibás MP3: '+name)
                target = staged/('normalized-'+name)
                engine.normalize(path,target,state.Spec.model_validate(job['spec']))
                target.replace(path)
        directory.mkdir(parents=True,exist_ok=True)
        for name in files:
            (staged/name).replace(directory/name)
    except (ValueError, tarfile.TarError, zipfile.BadZipFile, EOFError) as err:
        raise HTTPException(422,str(err)) from None
    finally:
        import shutil
        shutil.rmtree(state.job_dir(jid)/'import-stage',ignore_errors=True)
    state.log(jid,'Korábbi kész hangok importálva: %s. Ezekhez nem kell új TTS-kérés.' % len(files))
    return {'imported':len(files)}


def read_audio_archive(raw):
    allowed = engine.expected_names() | set(state.FIXED)
    files = {}
    total = 0
    if zipfile.is_zipfile(io.BytesIO(raw)):
        archive = zipfile.ZipFile(io.BytesIO(raw))
        members = [(m.filename,m.file_size,m.is_dir(), bool((m.external_attr >> 16) & 0o170000 == 0o120000),m)
                   for m in archive.infolist()]
        read = lambda m: archive.read(m)
    else:
        archive = tarfile.open(fileobj=io.BytesIO(raw),mode='r:*')
        members = [(m.name,m.size,m.isdir(),not m.isfile() and not m.isdir(),m) for m in archive.getmembers()]
        read = lambda m: archive.extractfile(m).read()
    try:
        if len(members)>500:
            raise ValueError('Túl sok elem az archívumban')
        for name,size,isdir,islink,member in members:
            if isdir:
                continue
            if islink or name != Path(name).name or '\\' in name or name not in allowed or name in files:
                raise ValueError('Tiltott, ismétlődő vagy nem hangfájl az archívumban')
            total += size
            if size>engine.MAX_AUDIO or total>64*1024*1024:
                raise ValueError('Túl nagy kibontott archívum')
            data=read(member)
            if name in state.FIXED:
                import hashlib
                if hashlib.sha256(data).hexdigest()!=state.FIXED[name]:
                    raise ValueError('Eltérő eredeti fix hangfájl: '+name)
            else:
                files[name]=data
        if not files:
            raise ValueError('Nincs beszélt hang az archívumban')
        return files
    finally:
        archive.close()


@router.get(PREFIX + '/style-rules')
def get_rules():
    return engine.rules()


@router.put(PREFIX + '/style-rules')
def save_rules(payload: dict):
    if (set(payload) - {'global_rule','styles','schema_version','builder_version','repair_policy'} or
        not isinstance(payload.get('global_rule'),str) or len(payload['global_rule'])>10000 or
        not isinstance(payload.get('styles'),dict)):
        raise HTTPException(422,'Érvénytelen stílusszabályok')
    allowed = {x['id'] for x in state.load_asset('text-styles.json')['styles']}
    for k,v in payload['styles'].items():
        if k not in allowed or not isinstance(v,dict) or set(v)!={'rule'} or not isinstance(v['rule'],str) or len(v['rule'])>10000:
            raise HTTPException(422,'Érvénytelen stílusszabály')
    state.atomic(state.root()/'style-rules.json',state.canonical(payload).encode())
    return payload


@router.post(PREFIX + '/style-rules/reset')
def reset_rules():
    payload = state.load_asset('style-rules.defaults.json')
    state.atomic(state.root()/'style-rules.json',state.canonical(payload).encode())
    return payload


@router.post(PREFIX + '/preset-import')
def import_preset(file: UploadFile = File(...)):
    """Restore desktop v5 selections and options, retaining server credentials."""
    try:
        raw = file.file.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise ValueError('Túl nagy beállításfájl')
        doc = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(doc,dict) or not str(doc.get('schema','')).startswith('anthbot-selection-preset-v'):
            raise ValueError('Desktop Builder selection_preset.json szükséges')
        provider = doc.get('tts_provider_id','elevenlabs')
        selections, local, voices = [], {}, {}
        defaults = {'provider':provider, 'text_style':doc.get('text_style_id','standard'),
                    'delivery':doc.get('delivery_style_id','natural'), 'character_effect':doc.get('character_effect_id','none'),
                    'hybrid':doc.get('elevenlabs_hybrid',True), 'translation_model':doc.get('openai_model') or 'gpt-4o-mini',
                    'ha_url':doc.get('home_assistant_url','')}
        if provider == 'fish_audio':
            defaults['model'] = doc.get('fish_audio_model','s2.1-pro-free')
        for entry in doc.get('catalog',[]):
            locale = entry['locale']
            metadata = {v['voice_id']:v for v in entry.get('voice_info',[])}
            local[locale] = {'voices':entry.get('voices',[]), 'target':entry.get('translation_target','')}
            for vid in entry.get('voices',[]):
                info = metadata.get(vid,{})
                spec = state.Spec(**defaults,locale=locale,language=entry.get('native_name') or entry.get('display_name') or locale,
                    voice_id=vid,provider_voice_name=info.get('name',''),
                    voice_gender=info.get('gender','unknown') if info.get('gender') in ('female','male') else 'unknown',
                    translation_target=entry.get('translation_target',''))
                selections.append(spec)
                voices[(locale,vid)] = {'id':vid,'locale':locale if provider=='ha_cloud' else '',
                    'name':info.get('name') or vid,'gender':spec.voice_gender}
        current = state.settings()
        draft = {**current.get('draft',{}),**defaults,'locales':list(local),'locale_selections':local,
                 'voice_catalog':list(voices.values()),'voices':list({s.voice_id for s in selections}),
                 'licensed_only':doc.get('fish_licensed_only',True),'follow_log':doc.get('follow_log',True),
                 'batch_scope':{'one_per_locale':'first','all_voices':'all'}.get(doc.get('batch_scope_id'),'selected'),
                 'skip_completed':doc.get('skip_completed',True),'continue_on_error':doc.get('continue_on_error',True),
                 'upload_enabled':doc.get('upload_enabled',True)}
        saved = state.save_settings(state.Settings(selections=selections,draft=draft,
            credit_limit=doc.get('elevenlabs_credit_limit',current['credit_limit'])))
    except (ValueError,KeyError,TypeError,UnicodeError):
        raise HTTPException(422,'Hibás vagy nem támogatott Builder beállításfájl') from None
    return {'imported':len(saved['selections']),'configured':saved['configured']}


@router.get(PREFIX + '/reviews')
def reviews():
    from voice_builder_review import items
    return {'items':items()}


@router.post(PREFIX + '/jobs/{jid}/review')
def review(jid: str):
    job_or_404(jid)
    from voice_builder_review import start_review
    try:
        with state.locked(jid+'.lock',blocking=False):
            return start_review(jid)
    except (ValueError, BlockingIOError) as err:
        raise HTTPException(409,str(err) or 'A feladat még fut') from None


@router.post(PREFIX + '/jobs/{jid}/validate')
def validate(jid: str):
    job_or_404(jid)
    try:
        with state.locked(jid+'.lock',blocking=False):
            return engine.validate_audio(jid)
    except BlockingIOError:
        raise HTTPException(409,'A feladat még fut') from None


@router.post(PREFIX + '/jobs/{jid}/build')
def build_existing(jid: str):
    job = job_or_404(jid)
    if job['status'] not in ('completed','failed','paused'):
        raise HTTPException(409,'A feladat még fut')
    try:
        with state.locked(jid+'.lock',blocking=False):
            doc = engine.validate_prompts(json.loads((state.job_dir(jid)/'prompts.json').read_text()))
            engine.build_pack(jid,state.Spec.model_validate(job['spec']),doc)
            state.update(jid,mode='build',status='completed',progress=100,error='')
    except (OSError,ValueError,RuntimeError,BlockingIOError) as err:
        raise HTTPException(409,str(err) if isinstance(err,(ValueError,RuntimeError)) else 'A kész hangok vagy az ellenőrzés hiányoznak') from None
    state.launch_worker()
    return state.get_job(jid)


@router.get(PREFIX + '/jobs/{jid}/audio.zip')
def export_audio(jid: str):
    job_or_404(jid)
    output = io.BytesIO()
    with state.locked(jid+'.lock'),zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as z:
        for name in sorted(engine.expected_names()):
            path = state.job_dir(jid)/'audio'/name
            if path.is_file():
                z.writestr(name,path.read_bytes())
    return Response(output.getvalue(),media_type='application/zip',
                    headers={'Content-Disposition':'attachment; filename="audio.zip"'})


@router.get(PREFIX + '/jobs/{jid}/review-backup.zip')
def export_review_backup(jid: str):
    job_or_404(jid)
    directory = state.root()/'review-backups'/jid
    if not directory.exists():
        raise HTTPException(404,'Ehhez a feladathoz nincs korábbi javítási mentés')
    output = io.BytesIO()
    with state.locked(jid+'.lock'),zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as z:
        for revision in sorted(directory.iterdir()):
            if not revision.is_dir() or len(revision.name)!=64:
                continue
            allowed = {'manifest.json','prompts.json','registry-record.json','pack.tar.gz','store-pack.tar.gz'}
            for path in sorted(revision.rglob('*')):
                relative = path.relative_to(revision)
                if path.is_file() and not path.is_symlink() and (str(relative) in allowed or
                    (len(relative.parts)==2 and relative.parts[0]=='audio' and path.name in engine.expected_names())):
                    z.writestr(revision.name+'/'+relative.as_posix(),path.read_bytes())
    return Response(output.getvalue(),media_type='application/zip',
                    headers={'Content-Disposition':'attachment; filename="review-backup.zip"'})

@router.post(PREFIX + '/cache-import')
def cache_import(file: UploadFile = File(...)):
    """Import desktop cache ZIP without executing or extracting arbitrary files."""
    raw = file.file.read(64*1024*1024+1)
    if len(raw)>64*1024*1024:
        raise HTTPException(413,'Egy cache ZIP legfeljebb 64 MB lehet')
    try:
        archive=zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(422,'ZIP archívum szükséges') from None
    imported={'audio':0,'prompts':0,'aliases':0,'skipped':0}
    total=0
    try:
        if len(archive.infolist())>10000:
            raise HTTPException(422,'Túl sok elem az archívumban')
        for m in archive.infolist():
            parts=Path(m.filename).parts
            if '..' in parts or m.filename.startswith('/') or '\\' in m.filename or ((m.external_attr>>16)&0o170000)==0o120000:
                raise HTTPException(422,'Tiltott útvonal az archívumban')
            total+=m.file_size
            if total>64*1024*1024 or m.file_size>engine.MAX_AUDIO:
                raise HTTPException(422,'Túl nagy kibontott cache')
            if m.is_dir():
                continue
            name=Path(m.filename).name
            import re
            if ('tts-cache' in parts and 'elevenlabs' in parts and re.fullmatch(r'[0-9a-f]{64}\.mp3',name)):
                p=state.root()/'tts-cache'/'elevenlabs'/name[:2]/name
                # Never overwrite a valid cache entry with an import.
                if engine.raw_valid(p):
                    imported['skipped']+=1
                    continue
                tmp=state.root()/'import-cache'/name
                state.atomic(tmp,archive.read(m))
                try:
                    if not engine.raw_valid(tmp):
                        imported['skipped']+=1
                        continue
                    state.atomic(p,tmp.read_bytes())
                    imported['audio']+=1
                finally:
                    tmp.unlink(missing_ok=True)
            elif name=='voice-aliases.json':
                doc=json.loads(archive.read(m).decode('utf-8-sig'))
                with state.locked('alias.lock'):
                    p=state.root()/'aliases.json'
                    aliases=json.loads(p.read_text()) if p.exists() else {}
                    for entry in doc.get('aliases',[]):
                        locale,voice,display=entry.get('locale',''),entry.get('voice_id',''),entry.get('display_name','')
                        if not isinstance(display,str) or not 0<len(display)<=100 or not isinstance(voice,str) or not isinstance(locale,str):
                            continue
                        key=state.digest({'locale':locale,'provider':'elevenlabs','voice_id':voice})
                        if key not in aliases:
                            aliases[key]=display
                            imported['aliases']+=1
                    state.atomic(p,state.canonical(aliases).encode())
            elif name.endswith('.json') and ('prompt-cache' in parts or ('prompts' in parts and 'chatgpt' in parts)):
                try:
                    doc=engine.validate_prompts(json.loads(archive.read(m).decode('utf-8-sig')))
                    locale=doc.get('locale') or doc.get('language_code') or doc.get('target_language_code')
                    style=doc.get('text_style_id','standard') or 'standard'
                    delivery=doc.get('delivery_style_id','natural') or 'natural'
                    # Pydantic restricts locale and style identifiers before using as path names.
                    spec=state.Spec(voice_id='cache',locale=locale,text_style=style,delivery=delivery)
                    p=state.root()/'migrated-prompts'/(spec.locale+'_'+spec.text_style+'_'+spec.delivery+'.json')
                    if not p.exists():
                        state.atomic(p,state.canonical({'doc':doc,'rules_hash':state.digest(engine.rules())}).encode())
                        imported['prompts']+=1
                    else:
                        imported['skipped']+=1
                except (ValueError,TypeError):
                    imported['skipped']+=1
            else:
                # Ignore Windows-encrypted secrets, executables and unrelated output.
                imported['skipped']+=1
    except (ValueError,UnicodeError,EOFError,zipfile.BadZipFile):
        raise HTTPException(422,'Hibás cache archívum; a korábban importált érvényes fájlok megmaradtak') from None
    finally:
        archive.close()
    return imported
