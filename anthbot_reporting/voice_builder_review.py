"""Review only provable server-built revisions; keep sales and old files intact."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import voice_builder_state as state


def backup_changed_audio(jid, old_doc, names):
    """Move only changed sentences aside, before replacing their text document."""
    directory = state.job_dir(jid)
    backup = state.root() / 'review-backups' / jid / state.digest(old_doc)
    state.atomic(backup / 'prompts.json', state.canonical(old_doc).encode())
    for name in names:
        if name not in {p['file'] for p in old_doc['prompts']}:
            raise ValueError('Ismeretlen javítandó hangfájl')
        path = directory / 'audio' / name
        if path.exists():
            state.atomic(backup / 'audio' / name,path.read_bytes())
            path.unlink()
    state.log(jid,'Csak a megváltozott mondatok hangjai cserélődnek: '+', '.join(names))


def review_state(jid):
    path = state.job_dir(jid) / 'review-state.json'
    return json.loads(path.read_text()) if path.exists() else None


def upload_recovery(job):
    """Read-only readiness check, including completed but never uploaded jobs.

    Do not spend money, queue generation, or mutate Store records while listing.
    Use exactly the publisher's proof gate instead of trusting a stale error.
    """
    if job['mode'] != 'build' or job['status'] != 'completed' or job['published']:
        return None
    from voice_builder_publish import approved_pack_text, ReviewRequired
    try:
        manifest = json.loads((state.job_dir(job['id'])/'manifest.json').read_text())
        approved_pack_text(job['id'], manifest)
    except ReviewRequired as err:
        return {'action':'review','reason':err.detail,
                'note':'A mentett szöveg ellenőrzése után csak a javított mondatok hangja készül újra. Az új QA és javítás szolgáltatói költséggel járhat.'}
    except (OSError, ValueError, KeyError, TypeError):
        # An unreadable artifact is not evidence that only QA is missing.
        return None
    return None


def backup_job(jid):
    directory = state.job_dir(jid)
    manifest = json.loads((directory/'manifest.json').read_text()) if (directory/'manifest.json').exists() else {}
    backup = state.root()/'review-backups'/jid/state.digest(manifest)
    for name in ('manifest.json','prompts.json','pack.tar.gz','text-approval.json'):
        path = directory/name
        if path.exists() and not (backup/name).exists():
            state.atomic(backup/name, path.read_bytes())
    return manifest


def scan_and_quarantine():
    """Local migration: no provider calls, no paid work, no registry deletion.

    An older job may refer to a superseded pack. Identity AND payload must match
    the current registry revision before hiding anything. Never touch desktop
    uploads, bundled packs, sales records, price or access fields.
    """
    import app as core
    from voice_builder_engine import definitions
    with state.db() as c:
        jobs = [state.public_job(r) for r in c.execute("SELECT * FROM jobs WHERE mode='build' AND published!='' ORDER BY created DESC")]
    if not jobs:
        return []
    affected = []
    with state.locked('publish.lock'):
        registry = core._uploaded_voice_pack_registry()
        dirty = False
        for job in jobs:
            jid = job['id']
            directory = state.job_dir(jid)
            try:
                manifest = json.loads((directory/'manifest.json').read_text())
                doc = json.loads((directory/'prompts.json').read_text())
                spec = state.Spec.model_validate(job['spec'])
                style,delivery,_ = definitions(spec)
                if not (style.get('rewrite_text') or delivery.get('rewrite_text')):
                    continue
                approval_path = directory/'text-approval.json'
                approval = json.loads(approval_path.read_text()) if approval_path.exists() else {}
                if approval.get('verified') is True and approval.get('prompt_sha256') == state.digest(doc) and manifest.get('prompt_sha256') == state.digest(doc):
                    continue
                record = next((r for r in registry.get('packs',[]) if isinstance(r,dict) and
                    core._voice_pack_community_id(r) == manifest['public_store']['community_id'].casefold() and
                    r.get('id') == manifest.get('server_pack_id',job['published'].get('pack',{}).get('id')) and
                    r.get('music_md5') == manifest.get('music_md5')),None)
                prior = review_state(jid)
                if not record:
                    if prior:
                        if prior.get('status') != 'repaired':
                            prior = {**prior,'status':'superseded','reason':'Időközben másik csomag került fel; a korábbi verzió javítása nem írhatja felül.'}
                            state.atomic(directory/'review-state.json',state.canonical(prior).encode())
                        affected.append({'job_id':jid,**prior})
                    continue
                backup = state.root()/'review-backups'/jid/state.digest(manifest)
                if not (backup/'registry-record.json').exists():
                    state.atomic(backup/'registry-record.json',state.canonical(record).encode())
                    for name in ('manifest.json','prompts.json','pack.tar.gz'):
                        path = directory/name
                        if path.exists():
                            state.atomic(backup/name,path.read_bytes())
                    filename = record.get('filename','')
                    if filename and Path(filename).name == filename and (core._voice_pack_dir()/filename).is_file():
                        state.atomic(backup/'store-pack.tar.gz',(core._voice_pack_dir()/filename).read_bytes())
                review = prior or {'status':'required','pack_id':record['id'],'community_id':manifest['public_store']['community_id'],
                                  'reason':'A korábbi szerveres szövegből hiányzott a végső nyelvtani / jelentés / karakter QA.',
                                  'was_hidden':bool(record.get('store_hidden')),'backup_available':True}
                if record.get('builder_review_required') is not True or record.get('store_hidden') is not True:
                    record['builder_review_required'] = True
                    record['store_hidden'] = True
                    dirty = True
                state.atomic(directory/'review-state.json',state.canonical(review).encode())
                affected.append({'job_id':jid,**review})
            except (OSError, ValueError, KeyError, TypeError):
                # Incomplete evidence cannot authorize changing a store record.
                continue
        if dirty:
            core._write_uploaded_voice_registry(registry)
    return affected


def items():
    scan_and_quarantine()
    with state.db() as c:
        jobs = [state.public_job(r) for r in c.execute("SELECT * FROM jobs WHERE mode='build' ORDER BY created DESC")]
    result = []
    for job in jobs:
        review = review_state(job['id'])
        recovery = upload_recovery(job)
        if recovery:
            review = {**(review or {}),'status':'required','reason':recovery['reason'],'note':recovery['note']}
        if review:
            result.append({'job_id':job['id'],'spec':job['spec'],**review,'job_status':job['status']})
    return result


def start_review(jid):
    job = state.get_job(jid)
    if job['mode'] != 'build' or job['status'] not in ('completed','failed','paused'):
        raise ValueError('Csak befejezett vagy megszakadt teljes csomag vizsgálható felül')
    if not (state.job_dir(jid)/'prompts.json').exists():
        raise ValueError('Hiányzik a csomaghoz tartozó mentett szövegkönyv')
    review = review_state(jid) or {'status':'required','reason':'Kézzel kért felülvizsgálat','backup_available':True}
    if review.get('status') == 'superseded':
        raise ValueError('Ezt a verziót már újabb feltöltés váltotta fel; a korábbi feladat nem írhatja felül')
    # Keep the old complete artifact before any QA correction or rebuild,
    # including legacy jobs that have never reached the Store.
    manifest = backup_job(jid)
    state.atomic(state.job_dir(jid)/'review-state.json',state.canonical({**review,'status':'queued','backup_available':True}).encode())
    state.atomic(state.job_dir(jid)/'review-intent.json',state.canonical({'repair':True,'expected_md5':manifest.get('music_md5','')}).encode())
    # Published result remains attached until a complete replacement exists.
    state.set_status(jid,'queued')
    state.launch_worker()
    return state.get_job(jid)


def replacement_ready(jid):
    directory = state.job_dir(jid)
    if not (directory/'review-intent.json').exists():
        return
    state.update(jid,published='')
    prior = review_state(jid) or {}
    state.atomic(directory/'review-state.json',state.canonical({**prior,'status':'verified_pending_upload'}).encode())


def replacement_published(jid, result):
    directory = state.job_dir(jid)
    if not (directory/'review-intent.json').exists():
        return
    review = review_state(jid) or {}
    state.atomic(directory/'review-state.json',state.canonical({**review,'status':'repaired','pack_id':result['pack']['id'],
        'assigned_version':result['assigned_version'],'reason':'Ellenőrzött javítás feltöltve; közzététel előtt meghallgatható.'}).encode())
    (directory/'review-intent.json').unlink(missing_ok=True)
