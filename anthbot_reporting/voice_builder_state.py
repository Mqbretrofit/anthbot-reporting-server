"""Persistent, private state for the admin-only server Voice Builder."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, field_validator

ASSETS = Path(__file__).with_name('voice_builder_assets')
SECRET_NAMES = ('elevenlabs_key', 'openai_key', 'fish_audio_key', 'ha_token')
FIXED = {
    'A001.mp3': '33c93e4390da546d77d745f0d1c0119184101837dc5902c996a3d29e15b58b0f',
    'A003.mp3': '676e4d10dfdcf4c74a7db9a2de099c3f3e979f67a37cf44634efccb28603f218',
    'A030.mp3': 'c077b272367dd010189f47e994dc8cb03b30a6613715fc4922467b77643d6d58',
}


def root() -> Path:
    default = Path(os.environ.get('ANTHBOT_DB_PATH', '/data/anthbot_reporting.sqlite3')).parent / 'voice_builder'
    p = Path(os.environ.get('ANTHBOT_VOICE_BUILDER_DIR', str(default)))
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def load_asset(name):
    return json.loads((ASSETS / name).read_text(encoding='utf-8-sig'))


def atomic(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@contextmanager
def locked(name, *, blocking=True):
    with (root() / name).open('a+b') as f:
        os.chmod(f.name, 0o600)
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def db():
    c = sqlite3.connect(root() / 'builder.sqlite3', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, spec TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER DEFAULT 0,
            current_file TEXT DEFAULT '', error TEXT DEFAULT '',
            created REAL NOT NULL, updated REAL NOT NULL, published TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            created REAL NOT NULL, message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS budgets (
            id TEXT PRIMARY KEY, credit_limit INTEGER NOT NULL, reserved REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS batch_options (
            id TEXT PRIMARY KEY, continue_on_error INTEGER DEFAULT 1,
            upload_enabled INTEGER DEFAULT 1
        );
    ''')
    try:
        yield c
        c.commit()
    finally:
        c.close()


class Spec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(default='elevenlabs', pattern=r'^(elevenlabs|openai|fish_audio|ha_cloud)$')
    locale: str = Field(default='hu-HU', pattern=r'^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8}){0,2}$')
    language: str = Field(default='Magyar', min_length=1, max_length=64)
    voice_id: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.|:-]+$')
    voice_display_name: str = Field(default='', max_length=100)
    provider_voice_name: str = Field(default='', max_length=200)
    voice_gender: str = Field(default='unknown', pattern=r'^(male|female|unknown)$')
    text_style: str = 'standard'
    delivery: str = 'natural'
    character_effect: str = 'none'
    model: str = Field(default='', max_length=80, pattern=r'^[a-zA-Z0-9_.-]*$')
    error_model: str = Field(default='eleven_flash_v2_5', max_length=80, pattern=r'^[a-zA-Z0-9_.-]+$')
    hybrid: bool = True
    translation_model: str = Field(default='gpt-4o-mini', max_length=80, pattern=r'^[a-zA-Z0-9_.-]+$')
    translation_target: str = Field(default='', pattern=r'^(?:[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8}){0,2})?$')
    legacy_theme: str = Field(default='', pattern=r'^(|standard|funny|robot|scifi|deep|radio)$')
    ha_url: str = Field(default='', max_length=2048)
    ha_engine: str = Field(default='tts.home_assistant_cloud', max_length=128, pattern=r'^tts\.[a-zA-Z0-9_]+$')

    @field_validator('text_style', 'delivery', 'character_effect')
    @classmethod
    def known_style(cls, value, info):
        filename, key = {'text_style': ('text-styles.json', 'styles'), 'delivery': ('delivery-styles.json', 'styles'), 'character_effect': ('character-effects.json', 'effects')}[info.field_name]
        if value not in {x['id'] for x in load_asset(filename)[key]}:
            raise ValueError('Ismeretlen stílus vagy effekt')
        return value

    @field_validator('ha_url')
    @classmethod
    def valid_url(cls, value):
        if value:
            u = urlsplit(value)
            if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('Érvénytelen Home Assistant URL')
        return value.rstrip('/')


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selections: list[Spec] = Field(default_factory=list, max_length=4096)
    credit_limit: int = Field(default=10000, ge=0, le=10000000)
    secret_updates: dict[str, str] = Field(default_factory=dict)
    draft: dict = Field(default_factory=dict)

    @field_validator('draft')
    @classmethod
    def valid_draft(cls, value):
        allowed = {'provider','locales','voices','text_style','delivery','character_effect','model','error_model',
                   'hybrid','translation_model','ha_url','ha_engine','custom_voice','custom_name','custom_gender',
                   'custom_locale','custom_language','custom_voices','custom_languages','voice_catalog',
                   'translation_target','legacy_theme','voice_query','licensed_only','batch_scope','continue_on_error',
                   'locale_selections','follow_log','skip_completed','upload_enabled'}
        if not set(value) <= allowed or len(canonical(value)) > 1024*1024:
            raise ValueError('Érvénytelen felületi beállítások')
        def has_secret(obj):
            if isinstance(obj, dict):
                return any(any(word in k.lower() for word in ('api_key','token','password','secret')) or has_secret(v) for k,v in obj.items())
            return isinstance(obj, list) and any(has_secret(x) for x in obj)
        if has_secret(value):
            raise ValueError('Kulcsok nem menthetők a felületi beállításokba')
        return value

    @field_validator('secret_updates')
    @classmethod
    def valid_secrets(cls, value):
        if any(k not in SECRET_NAMES or len(v) > 4096 for k, v in value.items()):
            raise ValueError('Érvénytelen kulcsmező')
        return value


def cipher():
    path = root() / 'encryption.key'
    # Exclusive creation prevents concurrent API/worker initialization races.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as f:
            f.write(Fernet.generate_key())
            f.flush()
            os.fsync(f.fileno())
    return Fernet(path.read_bytes())


def credentials():
    with locked('settings.lock'):
        path = root() / 'secrets.enc'
        return json.loads(cipher().decrypt(path.read_bytes())) if path.exists() else {}


def settings():
    with locked('settings.lock'):
        path = root() / 'settings.json'
        value = json.loads(path.read_text()) if path.exists() else {'selections': [], 'credit_limit': 10000}
        sec = root() / 'secrets.enc'
        keys = json.loads(cipher().decrypt(sec.read_bytes())) if sec.exists() else {}
    value['configured'] = {k: bool(keys.get(k)) for k in SECRET_NAMES}
    return value


def save_settings(value: Settings):
    with locked('settings.lock'):
        sec = root() / 'secrets.enc'
        keys = json.loads(cipher().decrypt(sec.read_bytes())) if sec.exists() else {}
        for k, v in value.secret_updates.items():
            if v:
                keys[k] = v
            else:
                keys.pop(k, None)
        atomic(sec, cipher().encrypt(canonical(keys).encode()))
        atomic(root() / 'settings.json', canonical(value.model_dump(exclude={'secret_updates'})).encode())
    return settings()


def job_dir(job_id):
    if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('Érvénytelen feladatazonosító')
    return root() / 'jobs' / job_id


def public_job(row):
    x = dict(row)
    x['spec'] = json.loads(x['spec'])
    x['published'] = json.loads(x['published']) if x['published'] else None
    return x


def get_job(job_id):
    job_dir(job_id)
    with db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    return public_job(row)


def jobs():
    with db() as c:
        return [public_job(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]


def pending_work():
    with db() as c:
        return c.execute("""SELECT 1 FROM jobs WHERE status IN ('queued','running') OR
            (status='completed' AND mode='build' AND published='' AND error='' AND
             NOT EXISTS (SELECT 1 FROM batch_options b WHERE b.id=jobs.batch_id AND b.upload_enabled=0)) LIMIT 1""").fetchone() is not None


def create_jobs(specs, mode, credit_limit, *, continue_on_error=True, upload_enabled=True):
    batch = secrets.token_hex(16)
    ids = []
    with db() as c:
        c.execute('INSERT INTO budgets(id, credit_limit) VALUES (?,?)', (batch, credit_limit))
        c.execute('INSERT INTO batch_options(id,continue_on_error,upload_enabled) VALUES (?,?,?)',
                  (batch,int(continue_on_error),int(upload_enabled)))
        for spec in specs:
            jid = secrets.token_hex(16)
            ids.append(jid)
            c.execute('INSERT INTO jobs(id,batch_id,spec,mode,status,created,updated) VALUES (?,?,?,?,?,?,?)',
                      (jid, batch, canonical(spec.model_dump()), mode, 'queued', time.time(), time.time()))
    return ids


def update(job_id, **values):
    allowed = {'status', 'progress', 'current_file', 'error', 'published', 'mode'}
    if not values or not set(values) <= allowed:
        raise ValueError('Invalid job update')
    with db() as c:
        c.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ',updated=? WHERE id=?',
                  [*values.values(), time.time(), job_id])


def log(job_id, message):
    # Only controlled messages are logged; never provider responses, URLs or keys.
    with db() as c:
        c.execute('INSERT INTO logs(job_id,created,message) VALUES (?,?,?)', (job_id, time.time(), message[:1000]))
        c.execute('DELETE FROM logs WHERE job_id=? AND id NOT IN (SELECT id FROM logs WHERE job_id=? ORDER BY id DESC LIMIT 2000)', (job_id, job_id))


def reserve(batch_id, amount):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM budgets WHERE id=?', (batch_id,)).fetchone()
        if row['credit_limit'] and row['reserved'] + amount > row['credit_limit']:
            raise RuntimeError('A teljes köteg elérte a beállított ElevenLabs kreditlimitet. A kész hangok megmaradtak.')
        c.execute('UPDATE budgets SET reserved=reserved+? WHERE id=?', (amount, batch_id))


def set_status(job_id, status):
    with db() as c:
        row = c.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        if status == 'paused':
            if row['status'] not in ('queued', 'running'):
                raise ValueError('Csak várakozó vagy futó feladat szüneteltethető')
        elif status == 'queued':
            if row['status'] not in ('failed', 'paused', 'completed'):
                raise ValueError('Ez a feladat már fut vagy várakozik')
        c.execute('UPDATE jobs SET status=?,error=?,updated=? WHERE id=?', (status, '', time.time(), job_id))


def refund_reservation(batch_id, amount):
    with db() as c:
        c.execute('UPDATE budgets SET reserved=MAX(0,reserved-?) WHERE id=?',(amount,batch_id))


def launch_worker():
    if os.environ.get('ANTHBOT_VOICE_BUILDER_NO_WORKER') == '1':
        return
    # Spawn arbitration is separate from the lifetime lock held by the worker.
    with locked('launch.lock'):
        lock = (root() / 'worker.lock').open('a+b')
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            # The child owns worker.lock for its entire run. Multiple children
            # racing this tiny gap exit immediately if the lock is already held.
            subprocess.Popen([sys.executable, str(Path(__file__).with_name('voice_builder_worker.py'))],
                             cwd=Path(__file__).parent, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        finally:
            lock.close()
