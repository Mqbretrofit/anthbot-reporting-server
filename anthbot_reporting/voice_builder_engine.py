"""Linux Voice Builder engine, ported from desktop alpha.7.20.11.

No network work runs on the reporting server's event loop. Provider request
signatures, localized text and finished audio are durable and reusable.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

import voice_builder_state as state

MAX_AUDIO = 8 * 1024 * 1024


class Paused(Exception):
    pass


class ProviderError(RuntimeError):
    def __init__(self, status=0, *, reason='', operation=''):
        self.status = status
        messages = {
            'invalid_api_key': 'Érvénytelen vagy visszavont API-kulcs. Mentsd el a teljes, érvényes kulcsot.',
            'missing_api_key': 'A szolgáltató nem kapott API-kulcsot.',
            'missing_permissions': 'Az API-kulcs jogosultsága hiányzik.',
            'insufficient_permissions': 'Az API-kulcs jogosultsága hiányzik.',
            'quota_exceeded': 'A szolgáltatói kreditkeret elfogyott, vagy az API-kulcs saját kerete nem elegendő.',
            'voice_not_found': 'A kiválasztott hang nem található vagy nem hozzáférhető.',
            'subscription_required': 'A szolgáltató ehhez előfizetést kér.',
            'unusual_activity': 'Az ElevenLabs szokatlan aktivitás miatt korlátozta a hozzáférést.',
        }
        self.reason = reason if reason in messages else ''
        message = ('A szolgáltató elutasította a kérést (HTTP %s).' % status if status else
                   'A szolgáltató nem válaszolt. A kész fájlok megmaradtak; a feladat folytatható.')
        if operation:
            message = operation + ': ' + message
        if self.reason:
            message += ' ' + messages[self.reason]
            if self.reason in ('missing_permissions','insufficient_permissions'):
                permission = {'ElevenLabs kreditkeret lekérdezése':'User → Read',
                              'ElevenLabs modelllista':'Models → Read',
                              'ElevenLabs hanglista':'Voices → Read',
                              'ElevenLabs hanggenerálás':'Text to Speech'}.get(operation)
                if permission:
                    message += ' ElevenLabs → Developers → API Keys → Edit: ' + permission + '.'
        elif status in (401,403):
            message += ' Ellenőrizd a kulcs érvényességét és a művelethez tartozó jogosultságot.'
        super().__init__(message)


def provider_operation(url):
    parts = urlsplit(url)
    if parts.hostname == 'api.elevenlabs.io':
        path = parts.path
        if path == '/v1/user/subscription':
            return 'ElevenLabs kreditkeret lekérdezése'
        if path == '/v1/models':
            return 'ElevenLabs modelllista'
        if path in ('/v1/voices','/v2/voices'):
            return 'ElevenLabs hanglista'
        if path.startswith('/v1/text-to-speech/'):
            return 'ElevenLabs hanggenerálás'
    return {'api.openai.com':'OpenAI szöveg- vagy hangfeldolgozás',
            'api.fish.audio':'Fish Audio hanggenerálás'}.get(parts.hostname,'')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward API credentials to a provider-controlled redirect.
        return None


def request(url, payload=None, headers=None, *, json_result=False):
    data = state.canonical(payload).encode() if payload is not None else None
    req = Request(url, data=data, headers={'User-Agent': 'ANTHBOT-Server-Voice-Builder/1.0',
                  **({'Content-Type': 'application/json'} if data is not None else {}), **(headers or {})})
    try:
        with build_opener(NoRedirect).open(req, timeout=240) as response:
            result = response.read(MAX_AUDIO + 1)
            if len(result) > MAX_AUDIO:
                raise RuntimeError('Túl nagy szolgáltatói válasz')
    except HTTPError as e:
        # Parse only a bounded, allowlisted reason code. Never log provider text,
        # headers, credentials, voice IDs, URLs or user-supplied query strings.
        reason = ''
        try:
            body = json.loads(e.read(8192))
            detail = body.get('detail',{}) if isinstance(body,dict) else {}
            if isinstance(detail,dict) and isinstance(detail.get('status'),str):
                reason = detail['status']
        except (ValueError, UnicodeError, OSError, TypeError):
            pass
        finally:
            e.close()
        raise ProviderError(e.code, reason=reason, operation=provider_operation(url)) from None
    except (URLError, TimeoutError, OSError):
        raise ProviderError() from None
    if json_result:
        try:
            return json.loads(result)
        except (ValueError, UnicodeError):
            raise RuntimeError('Érvénytelen szolgáltatói JSON-válasz') from None
    return result


def probe(path):
    try:
        if not path.exists() or path.stat().st_size < 500:
            return None
        p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                            'stream=codec_name,sample_rate,channels,bit_rate', '-of', 'json', str(path)],
                           capture_output=True, timeout=30, check=True)
        return json.loads(p.stdout)['streams'][0]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        return None


def spoken_valid(path):
    p = probe(path)
    return bool(p and p.get('codec_name') == 'mp3' and int(p.get('sample_rate', 0)) == 16000 and
                int(p.get('channels', 0)) == 1 and int(p.get('bit_rate', 0)) == 32000)


def raw_valid(path):
    p = probe(path)
    return bool(p and p.get('codec_name') == 'mp3')


def definitions(spec):
    style = next(x for x in state.load_asset('text-styles.json')['styles'] if x['id'] == spec.text_style)
    delivery = next(x for x in state.load_asset('delivery-styles.json')['styles'] if x['id'] == spec.delivery)
    effect = next(x for x in state.load_asset('character-effects.json')['effects'] if x['id'] == spec.character_effect)
    return style, delivery, effect


def normalize(source, dest, spec, *, native_delivery=True):
    _, delivery, effect = definitions(spec)
    filters = [effect.get('ffmpeg_filter', '')]
    if not native_delivery:
        filters.insert(0, delivery.get('fallback_ffmpeg_filter', ''))
    filters = ','.join(f for f in filters if f)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.part.mp3')
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-threads', '1', '-filter_threads', '1', '-i', str(source), '-map_metadata', '-1']
    if filters:
        args += ['-af', filters]
    args += ['-ac', '1', '-ar', '16000', '-codec:a', 'libmp3lame', '-b:a', '32k', '-write_xing', '0',
             '-id3v2_version', '0', '-write_id3v1', '0', str(tmp)]
    try:
        p = subprocess.run(args, capture_output=True, timeout=120)
        if p.returncode or not spoken_valid(tmp):
            raise RuntimeError('Az MP3 átalakítása vagy formátumellenőrzése sikertelen: ' + dest.name)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def expected_names():
    return {p['file'] for p in state.load_asset('prompts/en-US.json')['prompts']}


def validate_prompts(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get('prompts'), list):
        raise ValueError('Hiányzó szövegkönyv')
    rows = doc['prompts']
    if len(rows) != 201 or {p.get('file') for p in rows if isinstance(p, dict)} != expected_names():
        raise ValueError('A szövegkönyvnek pontosan a 201 eredeti fájlnevet kell tartalmaznia')
    if any(not isinstance(p.get('text'), str) or not 0 < len(p['text'].strip()) <= 2000 for p in rows):
        raise ValueError('Üres vagy túl hosszú hangszöveg')
    return doc


def check(jid):
    if state.get_job(jid)['status'] != 'running':
        raise Paused()


def rules():
    p = state.root() / 'style-rules.json'
    return json.loads(p.read_text()) if p.exists() else state.load_asset('style-rules.json')


def prompt_key(spec, *, styled=False, base=None):
    sig = {'schema': 1, 'master': state.digest(state.load_asset('prompts/en-US.json')),
           'locale': spec.locale, 'model': spec.translation_model}
    if styled:
        sig.update(base=state.digest(base), style=spec.text_style, delivery=spec.delivery,
                   rules=rules(), definitions=definitions(spec)[:2])
    return state.digest(sig)


def prompt_path(spec, *, styled=False, base=None):
    return state.root() / 'prompt-cache' / (prompt_key(spec, styled=styled, base=base) + '.json')


def chat_rows(rows, instruction, spec, keys, jid):
    if not keys.get('openai_key'):
        raise RuntimeError('Ehhez a fordításhoz vagy szövegstílushoz OpenAI API-kulcs szükséges.')
    result = []
    for offset in range(0, len(rows), 20):
        check(jid)
        group = rows[offset:offset + 20]
        state.log(jid, 'Szövegfeldolgozás: %s–%s / %s' % (offset + 1, min(offset + 20, len(rows)), len(rows)))
        chunk_path = state.root() / 'text-chunks' / (state.digest({'rows':group,'instruction':instruction,'model':spec.translation_model}) + '.json')
        if chunk_path.exists():
            result.extend(json.loads(chunk_path.read_text()))
            continue
        answer = request('https://api.openai.com/v1/chat/completions',
                         {'model': spec.translation_model, 'response_format': {'type': 'json_object'},
                          'messages': [{'role': 'system', 'content': instruction +
                            '\nReturn only JSON {"prompts":[{"file":"original filename","text":"result"}]}. '
                            'Keep filenames, exact row count and all numbers, units, button names and required actions.'},
                            {'role': 'user', 'content': state.canonical({'prompts': group})}]},
                         {'Authorization': 'Bearer ' + keys['openai_key']}, json_result=True)
        try:
            data = json.loads(answer['choices'][0]['message']['content'])['prompts']
        except (KeyError, IndexError, ValueError, TypeError):
            raise RuntimeError('A szövegfeldolgozó nem adott érvényes szövegkönyvet') from None
        if (not isinstance(data, list) or len(data) != len(group) or
            {p.get('file') for p in data if isinstance(p, dict)} != {p['file'] for p in group} or
            any(not isinstance(p.get('text'), str) or not 0 < len(p['text'].strip()) <= 2000 for p in data)):
            raise RuntimeError('A szövegfeldolgozó hiányos vagy hibás sorokat adott vissza')
        state.atomic(chunk_path, state.canonical(data).encode())
        result.extend(data)
    return result


def load_prompts(spec, keys, jid, *, before_ai=None):
    jobpath = state.job_dir(jid) / 'prompts.json'
    if jobpath.exists():
        return validate_prompts(json.loads(jobpath.read_text()))
    migrated = state.root() / 'migrated-prompts' / (spec.locale+'_'+spec.text_style+'_'+spec.delivery+'.json')
    if migrated.exists():
        data = json.loads(migrated.read_text())
        if data.get('rules_hash') == state.digest(rules()):
            doc = validate_prompts(data['doc'])
            state.atomic(jobpath, state.canonical(doc).encode())
            state.log(jid, 'Korábbi Builder szöveg-cache átvéve; nincs új fordítás vagy stilizálás.')
            return doc
    basepath = prompt_path(spec)
    bundled = state.ASSETS / 'prompts' / (spec.locale + '.json')
    if basepath.exists():
        base = validate_prompts(json.loads(basepath.read_text()))
        state.log(jid, 'Meglévő fordítás használata; nincs új fordítási kérés.')
    elif bundled.exists():
        base = validate_prompts(json.loads(bundled.read_text(encoding='utf-8-sig')))
        state.atomic(basepath, state.canonical(base).encode())
    else:
        if before_ai:
            before_ai()
        master = state.load_asset('prompts/en-US.json')
        translated = chat_rows(master['prompts'],
            'Translate the ANTHBOT mower messages naturally and accurately into %s (%s). '
            'Preserve the exact mower event, safety meaning and necessary actions.' % (spec.language, spec.locale),
            spec, keys, jid)
        base = validate_prompts({'locale': spec.locale, 'prompts': translated, 'prompt_count': 201})
        state.atomic(basepath, state.canonical(base).encode())
    style, delivery, _ = definitions(spec)
    rewrite = style.get('rewrite_text') or delivery.get('rewrite_text')
    if not rewrite:
        doc = base
    else:
        path = prompt_path(spec, styled=True, base=base)
        if path.exists():
            doc = validate_prompts(json.loads(path.read_text()))
            state.log(jid, 'Meglévő stílusszöveg használata; nincs új szövegkérés.')
        else:
            if before_ai:
                before_ai()
            eligible = [p for p in base['prompts'] if not p['file'].startswith('E')]
            policy = rules()
            instruction = ('Rewrite every line in %s (%s). %s\nStyle: %s\n%s\nDelivery: %s\n%s' %
                           (spec.language, spec.locale, policy['global_rule'], style['description'],
                            policy['styles'].get(spec.text_style, {}).get('rule', ''),
                            delivery['description'], delivery.get('text_direction', '')))
            rewritten = chat_rows(eligible, instruction, spec, keys, jid)
            mapping = {p['file']: p['text'] for p in rewritten}
            # Reject unchanged/source+append variants; repair only failed rows.
            for attempt in range(5):
                bad = [p for p in eligible if not fresh_text(p['text'], mapping[p['file']])]
                if not bad:
                    break
                mapping.update({p['file']: p['text'] for p in chat_rows(bad,
                    instruction + '\nPrevious output retained too much source wording. Use fresh sentence structure, '
                    'not the source followed by a joke. Every non-error line must be rewritten.', spec, keys, jid)})
            if any(not fresh_text(p['text'], mapping[p['file']]) for p in eligible):
                raise RuntimeError('A stílusellenőrzés változatlan mondatokat talált. A csomag nem lett publikálva.')
            doc = validate_prompts({**base, 'prompts': [
                {'file': p['file'], 'text': mapping.get(p['file'], p['text'])} for p in base['prompts']]})
            # Critical E rows always remain exact, irrespective of AI output.
            state.atomic(path, state.canonical(doc).encode())
    state.atomic(jobpath, state.canonical(doc).encode())
    return doc


def fresh_text(source, candidate):
    def norm(t):
        return re.sub(r'\W+', ' ', t.casefold()).strip()
    a, b = norm(source), norm(candidate)
    return a != b and not b.startswith(a + ' ') and SequenceMatcher(None, a, b).ratio() < .88


def tts_signature(spec, text, filename):
    style, delivery, _ = definitions(spec)
    error = filename.startswith('E')
    tagkey = 'elevenlabs_audio_tag' if spec.provider == 'elevenlabs' else 'fish_audio_tag'
    tags = [delivery.get(tagkey, ''), style.get(tagkey, '')] if not error else []
    tagged = ' '.join([t for t in tags if t] + [text])
    model = spec.model or {'elevenlabs': 'eleven_v4', 'openai': 'gpt-4o-mini-tts',
                           'fish_audio': 's2.1-pro-free', 'ha_cloud': 'ha_cloud'}[spec.provider]
    if spec.provider == 'elevenlabs' and spec.hybrid and (error or (spec.text_style == 'standard' and spec.delivery == 'natural')):
        model = spec.error_model
    if spec.provider == 'elevenlabs':
        settings = {'stability': .62 if error else delivery.get('elevenlabs_stability', .55),
                    'similarity_boost': .97 if error else max(.94, delivery.get('elevenlabs_similarity', .97))}
        # Same property order/compact UTF-8 as desktop PowerShell cache schema 2.
        return {'schema': 2, 'provider': 'elevenlabs', 'endpoint': 'v1/text-to-speech',
                'output_format': 'mp3_22050_32', 'voice_id': spec.voice_id, 'model_id': model,
                'language_code': spec.locale.split('-')[0] if len(spec.locale.split('-')[0]) == 2 else None,
                'text': tagged, 'voice_settings': settings}
    if spec.provider == 'openai':
        instruction = (state.load_asset('delivery-styles.json')['styles'][0]['openai_tts_instructions']
                       if error else delivery['openai_tts_instructions'])
        if not error:
            hints = {'funny':' Use comic timing and a playful personality.', 'wild_funny':' Use bold absurd comic timing.',
                     'sarcastic':' Use dry sarcastic timing.', 'flirty':' Use playful teasing warmth.',
                     'cute':' Sound charming and playful.'}
            instruction += hints.get(spec.text_style,'')
        return {'provider': spec.provider, 'model': model, 'voice': spec.voice_id, 'input': text,
                'instructions': instruction, 'response_format': 'mp3'}
    if spec.provider == 'fish_audio':
        temp, top, speed = {'emotional': (.78,.75,1), 'sensual': (.76,.72,.94), 'hard': (.62,.65,1.02),
                           'cheerful': (.82,.78,1.04), 'angry': (.78,.72,1.03), 'whispering': (.65,.68,.92),
                           'calm': (.55,.62,.93), 'sad': (.62,.65,.92)}.get(spec.delivery if not error else 'natural', (.7,.7,1))
        if not error and spec.text_style == 'wild_funny':
            temp, top = max(temp,.85), max(top,.8)
        elif not error and spec.text_style == 'funny':
            temp = max(temp,.8)
        return {'provider': spec.provider, 'model': model, 'text': tagged, 'reference_id': spec.voice_id,
                'format': 'mp3', 'sample_rate': 44100, 'mp3_bitrate': 128, 'latency': 'normal',
                'normalize': True, 'temperature': temp, 'top_p': top,
                'prosody': {'speed': speed, 'volume': 0, 'normalize_loudness': True}}
    profile = next((p for p in state.load_asset('profiles.json')['tts_profiles']
                    if p['voice'] == spec.voice_id and p['language_code'] == spec.locale), {})
    variant = next((v for v in delivery.get('provider_style_preference', []) if v in profile.get('variants', [])), '') if not error else ''
    return {'provider': spec.provider, 'ha_url': spec.ha_url, 'engine_id': spec.ha_engine,
            'message': text, 'language': spec.locale, 'cache': True,
            'options': {'voice': spec.voice_id + ('||' + variant if variant else ''), 'preferred_format': 'mp3',
                        'preferred_sample_rate': 16000, 'preferred_sample_channels': 1}}


def raw_cache_path(sig):
    # Desktop ElevenLabs uses insertion order, all other signatures canonical order.
    content = json.dumps(sig, ensure_ascii=False, separators=(',', ':')) if sig['provider'] == 'elevenlabs' else state.canonical(sig)
    h = hashlib.sha256(content.encode()).hexdigest()
    return state.root() / 'tts-cache' / sig['provider'] / h[:2] / (h + '.mp3')


def audio_request(sig, keys):
    provider = sig['provider']
    if provider == 'elevenlabs':
        if not keys.get('elevenlabs_key'):
            raise RuntimeError('Hiányzik az ElevenLabs API-kulcs')
        return request('https://api.elevenlabs.io/v1/text-to-speech/' + quote(sig['voice_id'], safe='') +
                       '?output_format=mp3_22050_32',
                       {k: sig[k] for k in ('text','model_id','language_code','voice_settings')},
                       {'xi-api-key': keys['elevenlabs_key'], 'Accept': 'audio/mpeg'}), True
    if provider == 'openai':
        if not keys.get('openai_key'):
            raise RuntimeError('Hiányzik az OpenAI API-kulcs')
        return request('https://api.openai.com/v1/audio/speech', {k:v for k,v in sig.items() if k != 'provider'},
                       {'Authorization': 'Bearer ' + keys['openai_key']}), True
    if provider == 'fish_audio':
        if not keys.get('fish_audio_key'):
            raise RuntimeError('Hiányzik a Fish Audio API-kulcs')
        return request('https://api.fish.audio/v1/tts', {k:v for k,v in sig.items() if k not in ('provider','model')},
                       {'Authorization': 'Bearer ' + keys['fish_audio_key'], 'model': sig['model']}), True
    if not keys.get('ha_token') or not sig['ha_url']:
        raise RuntimeError('Home Assistant URL és token szükséges')
    payload = {k:v for k,v in sig.items() if k not in ('provider','ha_url')}
    native = '||' in payload['options']['voice']
    try:
        answer = request(sig['ha_url'] + '/api/tts_get_url', payload,
                         {'Authorization': 'Bearer ' + keys['ha_token']}, json_result=True)
    except ProviderError:
        if not native:
            raise
        payload['options'] = {**payload['options'], 'voice': payload['options']['voice'].split('||')[0]}
        answer = request(sig['ha_url'] + '/api/tts_get_url', payload,
                         {'Authorization': 'Bearer ' + keys['ha_token']}, json_result=True)
        native = False
    target = urljoin(sig['ha_url'] + '/', answer.get('path') or answer.get('url') or '')
    if not (answer.get('path') or answer.get('url')) or urlsplit(target).netloc != urlsplit(sig['ha_url']).netloc or urlsplit(target).scheme != urlsplit(sig['ha_url']).scheme:
        raise RuntimeError('A Home Assistant érvénytelen hangcímet adott')
    return request(target, headers={'Authorization': 'Bearer ' + keys['ha_token']}), native


def estimate(job, spec, doc, rates=None):
    credit = 0
    new = 0
    directory = state.job_dir(job['id'])
    for p in doc['prompts']:
        if job['mode'] == 'preview' and p['file'] not in ('A004.mp3','A005.mp3'):
            continue
        sig = tts_signature(spec, p['text'], p['file'])
        if spoken_valid(directory / 'audio' / p['file']) or raw_valid(raw_cache_path(sig)):
            continue
        new += 1
        if spec.provider == 'elevenlabs':
            credit += len(sig['text']) * (rates or {}).get(sig['model_id'], 1)
    return {'estimated_credits': int(credit + .999), 'new_requests': new}


def preflight(job, spec, doc, keys, *, subscription=None):
    rates = {}
    if spec.provider == 'elevenlabs':
        pending = any(not spoken_valid(state.job_dir(job['id'])/'audio'/p['file']) and
                      not raw_valid(raw_cache_path(tts_signature(spec,p['text'],p['file'])))
                      for p in doc['prompts'] if job['mode'] != 'preview' or p['file'] in ('A004.mp3','A005.mp3'))
        if pending:
            if not keys.get('elevenlabs_key'):
                raise RuntimeError('Hiányzik az ElevenLabs API-kulcs')
            models = request('https://api.elevenlabs.io/v1/models',
                             headers={'xi-api-key':keys['elevenlabs_key']},json_result=True)
            ids = {m['model_id'] for m in models if m.get('can_do_text_to_speech',True)}
            for m in models:
                rate = m.get('model_rates',{}).get('character_cost_multiplier') or m.get('token_cost_factor') or 1
                if isinstance(rate,(int,float)) and 0 < rate <= 100:
                    rates[m['model_id']] = rate
            requested = {tts_signature(spec,p['text'],p['file'])['model_id'] for p in doc['prompts']
                         if job['mode'] != 'preview' or p['file'] in ('A004.mp3','A005.mp3')}
            if not requested <= ids:
                raise RuntimeError('Az ElevenLabs nem kínálja a kiválasztott modellt. Válassz az elérhető modellek listájából; a TTS nem indult el.')
    estimate_value = estimate(job, spec, doc, rates)
    state.atomic(state.job_dir(job['id']) / 'rates.json', state.canonical(rates).encode())
    state.log(job['id'], 'Új TTS-kérések: %s; becsült ElevenLabs kredit: %s' %
              (estimate_value['new_requests'], estimate_value['estimated_credits']))
    state.atomic(state.job_dir(job['id']) / 'estimate.json', state.canonical(estimate_value).encode())
    if spec.provider != 'elevenlabs' or not estimate_value['new_requests']:
        return
    if not keys.get('elevenlabs_key'):
        raise RuntimeError('Hiányzik az ElevenLabs API-kulcs')
    with state.db() as c:
        budget = c.execute('SELECT * FROM budgets WHERE id=?', (job['batch_id'],)).fetchone()
    if budget['credit_limit'] and budget['reserved'] + estimate_value['estimated_credits'] > budget['credit_limit']:
        raise RuntimeError('A becsült fogyasztás meghaladja a köteg megmaradt kreditkeretét. A TTS nem indult el.')
    if subscription is None:
        subscription = request('https://api.elevenlabs.io/v1/user/subscription', headers={'xi-api-key': keys['elevenlabs_key']}, json_result=True)
    remaining = subscription.get('character_limit', 0) - subscription.get('character_count', 0)
    if remaining < estimate_value['estimated_credits']:
        raise RuntimeError('Az ElevenLabs fiók becsült maradék kerete nem elég ehhez a feladathoz. A TTS nem indult el.')


def slug(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub('[^a-z0-9]+', '_', value).strip('_')[:40] or 'hang'


def alias(spec):
    if spec.voice_display_name.strip():
        return spec.voice_display_name.strip()
    key = state.digest({'locale': spec.locale, 'provider': spec.provider, 'voice_id': spec.voice_id})
    local_names = state.load_asset('local-alias-names.json')
    with state.locked('alias.lock'):
        path = state.root() / 'aliases.json'
        names = json.loads(path.read_text()) if path.exists() else {}
        if key not in names:
            language = spec.locale.split('-')[0]
            pools = local_names.get(language, local_names['default'])
            pool = pools.get(spec.voice_gender, pools['female'] + pools['male'])
            reserved = state.load_asset('voice-alias-reserved-names.json')['languages'].get(language, [])
            used = {slug(v) for v in names.values()} | set(reserved)
            start = int(key[:8],16) % len(pool)
            candidates = pool[start:] + pool[:start]
            base = next((v for v in candidates if slug(v) not in used), candidates[0])
            value, n = base, 2
            while slug(value) in used:
                value, n = base + ' ' + str(n), n + 1
            names[key] = value
            state.atomic(path, state.canonical(names).encode())
        return names[key]


def build_pack(jid, spec, doc):
    directory = state.job_dir(jid)
    for p in doc['prompts']:
        if not spoken_valid(directory / 'audio' / p['file']):
            raise RuntimeError('Hiányzó vagy hibás hang: ' + p['file'])
    for name, sha in state.FIXED.items():
        p = state.ASSETS / 'base-assets/genie_slot3_community' / name
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha or not raw_valid(p):
            raise RuntimeError('Az eredeti fix hangfájl ellenőrzése sikertelen: ' + name)
    archive = directory / 'pack.tar.gz'
    tmp = archive.with_suffix('.part')
    # Stable gzip/tar metadata makes retries byte-identical and server version allocation idempotent.
    with tmp.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as gz, tarfile.open(fileobj=gz, mode='w') as tar:
        for name in sorted(expected_names() | set(state.FIXED)):
            path = (state.ASSETS / 'base-assets/genie_slot3_community' / name) if name in state.FIXED else directory / 'audio' / name
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    with tarfile.open(tmp, 'r:gz') as tar:
        if set(tar.getnames()) != expected_names() | set(state.FIXED) or len(tar.getmembers()) != 204:
            raise RuntimeError('A csomag nem a szükséges 204 MP3 fájlt tartalmazza')
    tmp.replace(archive)
    public = metadata(spec)
    data = archive.read_bytes()
    manifest = {'schema_version': 1, 'builder_version': 'server-1.0 / desktop-7.20.11', 'public_store': public,
                'technical': spec.model_dump(exclude={'voice_display_name','ha_url'}),
                'music_md5': hashlib.md5(data).hexdigest(), 'sha256': hashlib.sha256(data).hexdigest(),
                'size': len(data), 'spoken_count': 201, 'total_mp3': 204,
                'fixed_sha256': state.FIXED, 'prompt_sha256': state.digest(doc)}
    state.atomic(directory / 'manifest.json', state.canonical(manifest).encode())
    state.atomic(directory / 'catalog.json', state.canonical(public).encode())
    state.log(jid, 'Ellenőrzött csomag kész: 201 beszélt + 3 eredeti hang, A004/A005 próbahang.')


def metadata(spec):
    style, delivery, effect = definitions(spec)
    display = alias(spec)
    # Display-name edits must never invalidate a previously sold entitlement.
    variant = 'voice_' + state.digest({'provider':spec.provider,'voice':spec.voice_id,'locale':spec.locale,
                                       'style':spec.text_style,'delivery':spec.delivery,'effect':spec.character_effect})[:32]
    language_code = spec.locale.lower() if spec.locale.lower().startswith(('zh-cn','zh-tw','zh-hk')) else spec.locale.split('-')[0].lower()
    return {'community_id': language_code + '_' + variant, 'variant_id': variant,
            'variant_name': display + ' · ' + style['short_name'] + ' · ' + delivery['short_name'],
            'locale': spec.locale, 'language': spec.language, 'language_code': language_code,
            'voice_display_name': display, 'voice_gender': spec.voice_gender,
            'style': spec.text_style, 'style_name': style['display_name'],
            'delivery': spec.delivery, 'delivery_name': delivery['display_name'],
            'character_effect': spec.character_effect, 'character_effect_name': effect['display_name'],
            'tier': 'standard' if (spec.text_style,spec.delivery,spec.character_effect) == ('standard','natural','none') else 'premium',
            'license_required': True, 'preview_files': ['A004.mp3','A005.mp3'],
            'compatible_models': ['Anthbot Genie 1000','Anthbot Genie 2000','Anthbot Genie 3000']}


def run_job(job):
    jid = job['id']
    directory = state.job_dir(jid)
    directory.mkdir(parents=True, exist_ok=True)
    spec = state.Spec.model_validate(job['spec'])
    keys = state.credentials()
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('FFmpeg/FFprobe nem elérhető; frissítsd a report server telepítését.')
    state.log(jid, 'Generálás indul; a kész fájlok és cache újrahasználhatók.')
    subscription = None
    def before_ai():
        nonlocal subscription
        if spec.provider == 'elevenlabs' and subscription is None:
            if not keys.get('elevenlabs_key'):
                raise RuntimeError('Hiányzik az ElevenLabs API-kulcs')
            state.log(jid, 'ElevenLabs hozzáférés ellenőrzése az új szövegfeldolgozás előtt.')
            subscription = request('https://api.elevenlabs.io/v1/user/subscription',
                                   headers={'xi-api-key':keys['elevenlabs_key']},json_result=True)
    doc = load_prompts(spec, keys, jid, before_ai=before_ai)
    preflight(job, spec, doc, keys, subscription=subscription)
    selected = [p for p in doc['prompts'] if job['mode'] != 'preview' or p['file'] in ('A004.mp3','A005.mp3')]
    for index, p in enumerate(selected):
        check(jid)
        name, text = p['file'], p['text']
        dest = directory / 'audio' / name
        state.update(jid, current_file=name, progress=int(index * 100 / len(selected)))
        if spoken_valid(dest):
            state.log(jid, name + ': kész hang megtartva.')
            continue
        sig = tts_signature(spec, text, name)
        raw = raw_cache_path(sig)
        native_path = raw.with_suffix('.json')
        if raw_valid(raw):
            native = json.loads(native_path.read_text()).get('native', False) if native_path.exists() else spec.provider != 'ha_cloud'
            state.log(jid, name + ': TTS-cache használata.')
        else:
            for attempt in range(3):
                check(jid)
                if spec.provider == 'elevenlabs':
                    rates = json.loads((directory/'rates.json').read_text())
                    cost = len(sig['text']) * rates.get(sig['model_id'], 1)
                    state.reserve(job['batch_id'], cost)
                try:
                    data, native = audio_request(sig, keys)
                    break
                except ProviderError as e:
                    # Only explicit non-success HTTP responses are retryable;
                    # timeout after an accepted request could otherwise bill twice.
                    if e.status not in (429,500,502,503,504) or attempt == 2:
                        raise
                    state.log(jid, name + ': átmeneti szolgáltatói hiba, újrapróbálás.')
                    time.sleep(attempt + 1)
            state.atomic(raw, data)
            if not raw_valid(raw):
                raw.unlink(missing_ok=True)
                raise RuntimeError('A szolgáltató nem adott érvényes MP3-at: ' + name)
            state.atomic(native_path, state.canonical({'native': native}).encode())
            state.log(jid, name + ': új TTS elkészült.')
        check(jid)
        normalize(raw, dest, spec, native_delivery=native)
    check(jid)
    if job['mode'] != 'preview':
        build_pack(jid, spec, doc)
    state.update(jid, status='completed', progress=100, current_file='', error='')


def available_voices(provider):
    keys = state.credentials()
    if provider == 'elevenlabs':
        if not keys.get('elevenlabs_key'):
            raise RuntimeError('Előbb mentsd el az ElevenLabs API-kulcsot')
        data = request('https://api.elevenlabs.io/v1/voices', headers={'xi-api-key': keys['elevenlabs_key']}, json_result=True)
        return [{'id':x['voice_id'], 'name':x.get('name', x['voice_id']), 'gender': x.get('labels',{}).get('gender','unknown')}
                for x in data.get('voices', [])]
    if provider == 'openai':
        return [{'id':v,'name':v,'gender':'unknown'} for v in state.load_asset('tts-providers.json')['openai_voices']]
    if provider == 'ha_cloud':
        return [{'id':x['voice'],'name':x['display_name'],'gender':x['gender'],'locale':x['language_code'],
                 'language':x['language']} for x in state.load_asset('profiles.json')['tts_profiles'] if x.get('enabled')]
    return []  # Fish reference IDs are entered explicitly, as in the desktop builder.
