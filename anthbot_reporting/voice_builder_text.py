"""Contextual translation and the desktop 7.20.11 final QA/repair pipeline.

The original English instructions are bundled verbatim. Every paid response is
saved before parsing. A QA retry reuses its response; repairs touch failed rows
only. Voice/provider changes never invalidate a verified text document.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

import voice_builder_state as state

VERIFICATION = 'grammar_semantic_character_fresh_rewrite_strength_gate_and_targeted_repair'
POLICY = 'external_rules_v1_fresh_rewrite'
TRUSTED = {'master_corrected_english', 'existing_working_reference',
           'chatgpt_contextual_translation', 'chatgpt_reviewed', 'chatgpt_styled_verified'}


def critical(name):
    definition = state.load_asset('critical-messages.json')
    return name in definition['critical_files'] or any(re.fullmatch(p, name, re.I) for p in definition['critical_patterns'])


def normalized(text):
    return ''.join(c for c in text.lower() if not c.isspace() and unicodedata.category(c)[0] not in ('P', 'S'))


def fresh_text(source, candidate):
    a, b = normalized(source), normalized(candidate)
    if not a or not b or a == b or b.startswith(a):
        return False
    words = lambda t: re.findall(r'[^\W_]+', t.lower(), re.UNICODE)
    sw, cw = words(source), words(candidate)
    n = min(4, len(sw))
    return not (n >= 2 and len(cw) >= n and sw[:n] == cw[:n])


def minimum_ratio(spec):
    return max({'funny':1., 'wild_funny':1., 'sarcastic':.58, 'flirty':.62, 'cute':.5}.get(spec.text_style, 0),
               {'natural':0, 'sensual':.62, 'emotional':.52, 'hard':.58, 'angry':.58,
                'cheerful':.52, 'whisper':.5, 'whispering':.5, 'calm':.46, 'sad':.5}.get(spec.delivery, .45))


def desktop_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def content_hash(doc):
    return desktop_hash([{'file':p['file'], 'text':p['text']} for p in doc['prompts']])


def repair_policy():
    from voice_builder_engine import rules
    policy = rules().get('repair_policy', {})
    return {'max_rounds':max(1, min(10, int(policy.get('max_rounds', 5)))),
            'draft_alternatives':max(1, min(5, int(policy.get('draft_alternatives', 3)))),
            'keep_generated_candidate':policy.get('keep_generated_candidate_if_qa_correction_breaks_freshness', True) is True}


def rule_hash(spec):
    from voice_builder_engine import rules
    r = rules()
    return desktop_hash({'schema_version':2, 'style_id':spec.text_style, 'delivery_id':spec.delivery,
                         'global_rule':r['global_rule'], 'style_rule':r['styles'].get(spec.text_style, {}).get('rule', ''),
                         'repair_policy':repair_policy()})


def target(spec):
    if spec.translation_target:
        return spec.translation_target
    locale = spec.locale.lower()
    if locale.startswith(('zh-tw', 'zh-hk')):
        return 'zh-TW'
    if locale.startswith('zh'):
        return 'zh-CN'
    return locale.split('-')[0]


def instruction(kind, spec, rows, *, round_number=0):
    from voice_builder_engine import definitions, rules
    style, delivery, _ = definitions(spec)
    template = state.load_asset('text-instructions.json')['templates'][kind]
    values = {'$([string]$style.display_name)':style['display_name'],
              '$([string]$delivery.display_name)':delivery['display_name'],
              '$([string]$style.description)':style['description'],
              '$([string]$delivery.description)':delivery['description'],
              '$([string]$delivery.text_direction)':delivery.get('text_direction', ''),
              '$([string]$style.id)':spec.text_style, '$([string]$delivery.id)':spec.delivery,
              '$globalRewriteRule':rules()['global_rule'],
              '$selectedStyleRule':rules()['styles'].get(spec.text_style, {}).get('rule', ''),
              '$draftAlternatives':str(repair_policy()['draft_alternatives']),
              '$styleName':style['display_name'], '$styleDescription':style['description'],
              '$styleStrength':style.get('rewrite_strength','medium'),
              '$deliveryName':delivery['display_name'], '$deliveryDescription':delivery['description'],
              '$deliveryDirection':delivery.get('text_direction',''),
              '$Locale':spec.locale, '$Round':str(round_number), '$TargetName':spec.language,
              '$Target':target(spec), '$sourceJson':state.canonical(rows),
              '$targetSpecific':('Use the regional standard appropriate for locale '+target(spec)+'.' if '-' in target(spec)
                                 else 'Use a neutral, broadly understood standard form; avoid region-specific slang.')}
    for token, value in sorted(values.items(), key=lambda x:-len(x[0])):
        template = template.replace(token, value)
    # Unresolved PowerShell interpolation would silently weaken the instruction.
    if re.search(r'\$[a-zA-Z(]', template):
        raise RuntimeError('Hiányos Builder ellenőrzési utasítás')
    return template


def responses_rows(rows, text, spec, keys, jid, *, qa=False, before_ai=None, purpose='text', _row_cache=True):
    from voice_builder_engine import check, paid_once, request, ProviderError
    if qa and _row_cache:
        def row_path(row):
            return state.root()/'qa-row-results'/(state.digest({'verification':VERIFICATION,'row':row,
                'locale':spec.locale,'style':spec.text_style,'delivery':spec.delivery,'rules':rule_hash(spec)})+'.json')
        cached, missing = {}, []
        for row in rows:
            path = row_path(row)
            if path.exists():
                cached[row['file']] = json.loads(path.read_text())
            else:
                missing.append(row)
        if missing:
            # An edit to one sentence does not re-bill QA of the other 200.
            request_text = text if len(missing)==len(rows) else instruction('New-StyleRepairVerifyInstruction',spec,missing)
            checked = responses_rows(missing,request_text,spec,keys,jid,qa=True,before_ai=before_ai,
                purpose=purpose+' (%s új sor)' % len(missing),_row_cache=False)
            for row, result in zip(missing,checked):
                cached[row['file']] = result
                state.atomic(row_path(row),state.canonical(result).encode())
                if all(result[k] for k in ('grammar_ok','meaning_ok','character_ok')):
                    corrected = {**row,'candidate':result['corrected_text']}
                    state.atomic(row_path(corrected),state.canonical(result).encode())
        if cached:
            check(jid)
            state.log(jid,'QA: %s mentett sor megtartva; %s új ellenőrzés.' % (len(rows)-len(missing),len(missing)))
        return [cached[row['file']] for row in rows]
    identity = {'purpose':'qa' if qa else 'text', 'model':spec.translation_model, 'instruction':text, 'api':'responses-v1'}
    key = state.digest(identity)
    response_path = state.root() / 'paid-text-results' / (key+'.json')
    if not response_path.exists():
        if not keys.get('openai_key'):
            raise RuntimeError('A fordításhoz vagy a végső szövegellenőrzéshez OpenAI API-kulcs szükséges.')
        if before_ai:
            before_ai()
    check(jid)
    state.log(jid, ('Mentett szöveg-ellenőrzési eredmény használata: ' if response_path.exists()
                    else 'ChatGPT: ') + purpose)
    for attempt in range(4):
        try:
            answer = paid_once(identity, lambda: request('https://api.openai.com/v1/responses',
                {'model':spec.translation_model, 'store':False, 'input':text,
                 'text':{'format':{'type':'json_object'}},
                 'max_output_tokens':16384 if spec.translation_model.startswith('gpt-4o') else 32768},
                {'Authorization':'Bearer '+keys['openai_key']}, json_result=True), response_path)
            break
        except ProviderError as err:
            if err.status != 429 or err.reason in ('insufficient_quota', 'credit_balance_exhausted',
                'organization_usage_limit_exceeded', 'organization_spend_limit_exceeded', 'project_spend_limit_exceeded') or attempt == 3:
                raise
            # Explicit rejections only. Uncertain requests are stopped by paid_once.
            import time
            wait = min(60, err.retry_after or (15,30,60)[attempt])
            state.log(jid, 'Átmeneti OpenAI korlátozás; várakozás: %s mp.' % wait)
            for _ in range(wait):
                check(jid)
                time.sleep(1)
    try:
        if answer.get('status') in ('incomplete', 'failed') or answer.get('error'):
            raise ValueError()
        output = answer.get('output_text') or ''.join(c.get('text', '') for o in answer.get('output', [])
            if o.get('type') == 'message' for c in o.get('content', []) if c.get('type') == 'output_text')
        data = json.loads(output)['prompts']
        if not isinstance(data, list) or [p.get('file') for p in data] != [p['file'] for p in rows]:
            raise ValueError()
        field = 'corrected_text' if qa else 'text'
        if any(not isinstance(p.get(field), str) or not 0 < len(p[field].strip()) <= 2000 for p in data):
            raise ValueError()
        if qa and any(type(p.get(k)) is not bool for p in data for k in ('grammar_ok','meaning_ok','character_ok','review_required')):
            raise ValueError()
        if any('review_required' in p and type(p['review_required']) is not bool for p in data):
            raise ValueError()
        if any(not isinstance(p.get('note',''), str) or len(p.get('note','')) > 2000 for p in data):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError('A mentett OpenAI-válasz hiányos, hibás vagy rossz fájlsorrendű. Nem küldjük újra a fizetős kérést.') from None
    return data


def base_document(spec, keys, jid, before_ai=None):
    from voice_builder_engine import validate_prompts, prompt_path
    master = state.load_asset('prompts/en-US.json')
    # Freeze the base used by an in-flight job, including before rule changes.
    jobbase = state.job_dir(jid) / 'base-prompts.json'
    if jobbase.exists():
        return validate_prompts(json.loads(jobbase.read_text()))
    bundled = state.ASSETS / 'prompts' / (spec.locale+'.json')
    legacy = prompt_path(spec)
    shared = state.root() / 'translation-cache' / (state.digest({'master':desktop_hash(master['prompts']), 'target':target(spec)})+'.json')
    migrated = state.root() / 'migrated-prompts' / (spec.locale+'_standard_natural.json')
    migrated_target = state.root() / 'migrated-prompts' / (target(spec)+'_standard_natural.json')
    if bundled.exists():
        base = validate_prompts(json.loads(bundled.read_text(encoding='utf-8-sig')))
    elif target(spec).split('-')[0].lower() == 'en':
        base = master
    elif legacy.exists():
        base = validate_prompts(json.loads(legacy.read_text()))
    elif shared.exists():
        base = validate_prompts(json.loads(shared.read_text()))
    elif migrated.exists():
        base = validate_prompts(json.loads(migrated.read_text())['doc'])
    elif migrated_target.exists():
        base = validate_prompts(json.loads(migrated_target.read_text())['doc'])
    else:
        with state.locked('translation-'+shared.stem+'.lock'):
            if shared.exists():
                base = validate_prompts(json.loads(shared.read_text()))
            else:
                rows = responses_rows(master['prompts'], instruction('New-TranslationInstruction', spec, master['prompts']),
                    spec, keys, jid, before_ai=before_ai, purpose='teljes kontextusú fordítás: '+target(spec))
                base = validate_prompts({'schema_version':3, 'language_code':target(spec), 'prompt_count':201,
                    'translation_status':'chatgpt_contextual_translation', 'master_sha256':desktop_hash(master['prompts']), 'prompts':rows})
                state.atomic(shared, state.canonical(base).encode())
    state.atomic(legacy, state.canonical(base).encode())
    # Reuse any prior regional translation for its common target, before a new
    # voice/locale can accidentally pay for the same completed translation.
    if not bundled.exists() and not shared.exists():
        state.atomic(shared, state.canonical(base).encode())
    state.atomic(jobbase, state.canonical(base).encode())
    return base


def verified(doc, base, spec):
    from voice_builder_engine import validate_prompts
    try:
        validate_prompts(doc)
        if (doc.get('translation_status') != 'chatgpt_styled_verified' or doc.get('schema_version',0) < 6 or
            doc.get('style_verification') != VERIFICATION or doc.get('style_policy_version') != POLICY or
            doc.get('base_prompt_sha256') != content_hash(base) or doc.get('style_rule_sha256') != rule_hash(spec) or
            doc.get('text_style_id') != spec.text_style or doc.get('delivery_style_id') != spec.delivery):
            return False
        return all((p['text'] == b['text'] if critical(p['file']) else fresh_text(b['text'],p['text'])) and
                   (not b.get('review_required') or p.get('review_required') is True)
                   for b,p in zip(base['prompts'],doc['prompts']))
    except (ValueError, KeyError, TypeError):
        return False


def qa_document(candidate, base, spec, keys, jid, before_ai=None):
    from voice_builder_engine import validate_prompts
    validate_prompts(candidate)
    policy = repair_policy()
    current = [dict(p) for p in candidate['prompts']]
    for b,p in zip(base['prompts'],current):
        if critical(b['file']):
            p.update(b)
        if b.get('review_required'):
            p['review_required'] = True
    failed, rounds, initial = [], 0, 0
    indices = list(range(201))
    report_path = state.job_dir(jid) / 'text-review.json'
    for round_number in range(policy['max_rounds']+1):
        if round_number:
            rows = [{'file':base['prompts'][i]['file'], 'source':base['prompts'][i]['text'], 'current':current[i]['text']}
                    for i in indices]
            repaired = responses_rows(rows, instruction('New-StyleRepairInstruction',spec,rows,round_number=round_number),
                spec,keys,jid,before_ai=before_ai,purpose='célzott javítás %s: %s sor' % (round_number,len(rows)))
            for i,p in zip(indices,repaired):
                current[i].update(p)
            rounds = round_number
        rows = [{'file':base['prompts'][i]['file'], 'source':base['prompts'][i]['text'], 'candidate':current[i]['text'],
                 'critical':critical(base['prompts'][i]['file']), 'source_review_required':bool(base['prompts'][i].get('review_required'))}
                for i in indices]
        kind = 'New-StyleVerifyInstruction' if round_number == 0 else 'New-StyleRepairVerifyInstruction'
        checks = responses_rows(rows,instruction(kind,spec,rows),spec,keys,jid,qa=True,before_ai=before_ai,
            purpose='nyelvtan / jelentés / karakter QA%s' % (' — célzott kör '+str(round_number) if round_number else ' — 201 sor'))
        failed = []
        findings = []
        for i,q in zip(indices,checks):
            b,p = base['prompts'][i],current[i]
            if critical(b['file']):
                p.update(b)
                continue
            corrected, generated = q['corrected_text'],p['text']
            keep = policy['keep_generated_candidate'] and fresh_text(b['text'],generated)
            # QA flags describe corrected_text. Do not accept an unchecked draft
            # merely because its wording is fresher than the QA correction.
            selected = corrected if fresh_text(b['text'],corrected) else generated if keep else corrected
            reasons = [k for k in ('grammar_ok','meaning_ok','character_ok') if q[k] is not True]
            if not fresh_text(b['text'],selected):
                reasons.append('freshness')
            if selected != corrected:
                reasons.append('qa_correction_breaks_freshness')
            p.update(text=selected,review_required=bool(b.get('review_required')) or q['review_required'],note=q.get('note',''))
            if reasons:
                failed.append(i)
            findings.append({'file':b['file'],'passed':not reasons,'issues':reasons,'review_required':p['review_required'],'note':p['note']})
        if round_number == 0:
            initial = len(failed)
        state.atomic(report_path,state.canonical({'round':round_number,'remaining':[base['prompts'][i]['file'] for i in failed],
            'findings':findings,'candidate':current}).encode())
        state.log(jid,'Szövegellenőrzés %s. kör: %s hibás sor; a jó sorokat megtartjuk.' % (round_number,len(failed)))
        if not failed:
            break
        indices = failed
    if failed:
        raise RuntimeError('A végső nyelvtani / jelentés / karakterellenőrzés nem sikerült: '+', '.join(base['prompts'][i]['file'] for i in failed)+'. TTS és feltöltés nem indul.')
    eligible = [i for i,b in enumerate(base['prompts']) if not critical(b['file'])]
    changed = sum(normalized(current[i]['text']) != normalized(base['prompts'][i]['text']) for i in eligible)
    ratio = changed / len(eligible)
    if ratio < minimum_ratio(spec):
        raise RuntimeError('A karaktererősség ellenőrzése nem sikerült; a csomag nem tölthető fel.')
    doc = {**candidate,'schema_version':6,'locale':spec.locale,'prompt_count':201,
           'translation_status':'chatgpt_styled_verified','base_prompt_sha256':content_hash(base),
           'master_sha256':desktop_hash(state.load_asset('prompts/en-US.json')['prompts']),
           'text_style_id':spec.text_style,'delivery_style_id':spec.delivery,'style_policy_version':POLICY,
           'style_rule_sha256':rule_hash(spec),'style_verification':VERIFICATION,'targeted_repair_initial_count':initial,
           'targeted_repair_rounds':rounds,'character_rewrite_changed_count':changed,'character_rewrite_eligible_count':len(eligible),
           'character_rewrite_ratio':ratio,'character_rewrite_minimum_ratio':minimum_ratio(spec),
           'review_required_count':sum(bool(p.get('review_required')) for p in current),'prompts':current}
    return validate_prompts(doc)


def load_prompts(spec, keys, jid, before_ai=None):
    from voice_builder_engine import definitions, validate_prompts, prompt_path
    jobpath = state.job_dir(jid) / 'prompts.json'
    previous = validate_prompts(json.loads(jobpath.read_text())) if jobpath.exists() else None
    style, delivery, _ = definitions(spec)
    rewrite = style.get('rewrite_text') or delivery.get('rewrite_text')
    prior_approval = state.job_dir(jid)/'text-approval.json'
    if previous and prior_approval.exists():
        approval = json.loads(prior_approval.read_text())
        if approval.get('verified') is True and approval.get('prompt_sha256') == state.digest(previous):
            state.log(jid,'A feladat ellenőrzött szövegkönyve megmaradt; nincs új szöveg- vagy QA-kérés.')
            return previous
    # An already completed standard translation is never translated again to
    # establish a new base. Styled legacy jobs use their original locale cache.
    if previous and not rewrite:
        bundled = state.ASSETS/'prompts'/(spec.locale+'.json')
        base = json.loads(bundled.read_text(encoding='utf-8-sig')) if bundled.exists() else previous
    else:
        base = base_document(spec, keys, jid, before_ai)
    if not rewrite:
        # Imported/started scripts stay frozen. Translation is a separate stage.
        doc = {**(previous or base),'prompts':[dict(p) for p in (previous or base)['prompts']]}
        for b,p in zip(base['prompts'],doc['prompts']):
            if critical(p['file']):
                p.update(b)
        if spec.legacy_theme == 'funny' and not previous:
            override = state.ASSETS/'theme-overrides/funny'/(spec.locale+'.json')
            if override.exists():
                mapping = json.loads(override.read_text(encoding='utf-8-sig'))['overrides']
                for p in doc['prompts']:
                    if not critical(p['file']) and p['file'] in mapping:
                        p['text'] = mapping[p['file']]
    else:
        path = prompt_path(spec,styled=True,base=base)
        legacy_path = prompt_path(spec,styled=True,base=base,legacy=True)
        migrated = state.root() / 'migrated-prompts' / (spec.locale+'_'+spec.text_style+'_'+spec.delivery+'.json')
        with state.locked('style-'+path.stem+'.lock'):
            cached = validate_prompts(json.loads(path.read_text())) if path.exists() else None
            if cached is None and legacy_path.exists():
                cached = validate_prompts(json.loads(legacy_path.read_text()))
            imported = json.loads(migrated.read_text())['doc'] if migrated.exists() else None
            doc = next((p for p in (previous,cached,imported) if p and verified(p,base,spec) and
                        (previous is None or content_hash(p) == content_hash(previous))),None)
            if doc:
                state.log(jid,'Ellenőrzött karakter-cache használata; nincs új fordítás, stílus- vagy QA-kérés.')
            else:
                candidate = previous or cached or imported
                if candidate is None:
                    rows = [{**p,'critical':critical(p['file']),'source_review_required':bool(p.get('review_required'))} for p in base['prompts']]
                    draft = responses_rows(rows,instruction('New-StyleRewriteInstruction',spec,rows),spec,keys,jid,
                        before_ai=before_ai,purpose='karakteres szöveg: '+spec.text_style+' / '+spec.delivery)
                    candidate = {**base,'prompts':draft}
                    # Retain drafts if QA fails: no second paid rewrite on resume.
                    state.atomic(path,state.canonical(candidate).encode())
                else:
                    state.log(jid,'Meglévő stílusszöveg végső ellenőrzése; nincs új fordítás vagy teljes stilizálás.')
                doc = qa_document(candidate,base,spec,keys,jid,before_ai)
                state.atomic(path,state.canonical(doc).encode())
    if previous:
        changed = [p['file'] for b,p in zip(previous['prompts'],doc['prompts']) if b['text'] != p['text']]
        if changed:
            from voice_builder_review import backup_changed_audio
            backup_changed_audio(jid, previous, changed)
    state.atomic(jobpath,state.canonical(doc).encode())
    state.atomic(state.job_dir(jid)/'text-approval.json',state.canonical({'prompt_sha256':state.digest(doc),
        'base_sha256':state.digest(base),'rewrite':bool(rewrite),'verified':not rewrite or verified(doc,base,spec),
        'review_required_count':sum(bool(p.get('review_required')) for p in doc['prompts'])}).encode())
    return doc


def require_approval(jid, doc):
    path = state.job_dir(jid) / 'text-approval.json'
    approval = json.loads(path.read_text()) if path.exists() else {}
    if approval.get('verified') is not True or approval.get('prompt_sha256') != state.digest(doc):
        raise RuntimeError('A teljes szövegellenőrzés hiányzik vagy a szövegkönyv megváltozott; feltöltés nem indul.')
    return approval
