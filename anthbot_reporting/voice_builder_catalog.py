"""Paginated provider voice discovery; no paid generation requests."""
from urllib.parse import urlencode
import voice_builder_state as state


def available_voices(provider, query='', licensed_only=True):
    from voice_builder_engine import request
    keys = state.credentials()
    if provider == 'elevenlabs':
        if not keys.get('elevenlabs_key'):
            raise RuntimeError('Előbb mentsd el az ElevenLabs API-kulcsot')
        rows, token = [], ''
        for _ in range(10):
            args = {'page_size':100,'sort':'name','sort_direction':'asc','include_total_count':'false'}
            if query:
                args['search'] = query
            if token:
                args['next_page_token'] = token
            data = request('https://api.elevenlabs.io/v2/voices?'+urlencode(args),
                           headers={'xi-api-key':keys['elevenlabs_key']},json_result=True)
            for x in data.get('voices',[]):
                labels = x.get('labels',{})
                rows.append({'id':x['voice_id'],'name':x.get('name',x['voice_id']),
                    'gender':labels.get('gender','unknown'),'age':labels.get('age',''),
                    'accent':labels.get('accent',''),'description':x.get('description',''),
                    'category':x.get('category',''),'preview_url':x.get('preview_url','')})
            token = data.get('next_page_token')
            if not data.get('has_more') or not token:
                break
        return list({x['id']:x for x in rows}.values())
    if provider == 'openai':
        return [{'id':v,'name':v,'gender':'unknown'} for v in state.load_asset('tts-providers.json')['openai_voices']]
    if provider == 'ha_cloud':
        return state.load_asset('cloud-voices.json')['voices']
    if not keys.get('fish_audio_key'):
        raise RuntimeError('Előbb mentsd el a Fish Audio API-kulcsot')
    rows = {}
    for own, pages in ((True,5),(False,3)):
        for page in range(1,pages+1):
            args = {'page_size':100,'page_number':page,'sort_by':'created_at' if own else 'task_count'}
            if own:
                args['self'] = 'true'
            elif licensed_only:
                args['licensed'] = 'true'
            if query:
                args['title'] = query
            data = request('https://api.fish.audio/model?'+urlencode(args),
                           headers={'Authorization':'Bearer '+keys['fish_audio_key']},json_result=True)
            items = data.get('items',[])
            for x in items:
                vid = x.get('_id') or x.get('id')
                if not vid or x.get('type','tts') not in ('','tts') or x.get('state','trained') not in ('','trained'):
                    continue
                tags = x.get('tags',[])
                gender = next((t for t in tags if t in ('female','male')), 'unknown')
                rows.setdefault(vid,{'id':vid,'name':x.get('title',vid),'gender':gender,
                    'description':x.get('description',''),'tags':tags,'languages':x.get('languages',[]),
                    'license':x.get('license',''),'owned':own})
            if len(items)<100:
                break
    return list(rows.values())
