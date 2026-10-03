"""A single durable queue worker, independent of browser requests."""
import fcntl
import time
import voice_builder_state as state
from voice_builder_engine import run_job, Paused, ProviderError


def publish_completed(job_id):
    from voice_builder_publish import publish_job
    job = state.get_job(job_id)
    if job['status'] != 'completed' or job['mode'] != 'build' or job['published']:
        return
    with state.db() as c:
        option = c.execute('SELECT upload_enabled FROM batch_options WHERE id=?',(job['batch_id'],)).fetchone()
    if option and not option['upload_enabled']:
        return
    state.log(job_id, 'A teljes csomag elkészült; automatikus Hangbolt-feltöltés indul.')
    try:
        publish_job(job_id)
    except Exception:
        # Publishing is separate from generation: keep downloadable output and
        # avoid automatic retry loops or paid TTS calls after an upload failure.
        message = 'Automatikus Hangbolt-feltöltés sikertelen. A csomag elkészült és megmaradt; a feltöltés külön újrapróbálható.'
        state.update(job_id, error=message)
        state.log(job_id, message)
    else:
        state.update(job_id, error='')


def main():
    with (state.root() / 'worker.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Only this lock owner can recover jobs left running by a process restart.
        with state.db() as c:
            c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        idle = 0
        while idle < 15:
            with state.db() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute("""SELECT * FROM jobs WHERE status='queued' OR
                    (status='completed' AND mode='build' AND published='' AND error='' AND
                     NOT EXISTS (SELECT 1 FROM batch_options b WHERE b.id=jobs.batch_id AND b.upload_enabled=0))
                    ORDER BY created LIMIT 1""").fetchone()
                if row and row['status'] == 'queued':
                    c.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
            if not row:
                idle += 1
                time.sleep(1)
                continue
            idle = 0
            job = state.public_job(row)
            try:
                with state.locked(job['id'] + '.lock'):
                    if job['status'] != 'completed':
                        run_job(job)
                    publish_completed(job['id'])
            except Paused:
                state.log(job['id'], 'Feladat szüneteltetve. A kész hangok megmaradtak.')
            except Exception as err:
                # Unexpected exceptions can contain provider responses or credentials.
                message = str(err) if isinstance(err, (ProviderError, RuntimeError, ValueError)) else 'A feldolgozás megszakadt. A kész fájlok megmaradtak.'
                state.update(job['id'], status='failed', error=message[:500])
                state.log(job['id'], message[:500])
                with state.db() as c:
                    options = c.execute('SELECT continue_on_error FROM batch_options WHERE id=?',(job['batch_id'],)).fetchone()
                    if options and not options['continue_on_error']:
                        c.execute("UPDATE jobs SET status='paused',error=? WHERE batch_id=? AND status='queued'",
                                  ('A köteg az előző feladat hibája miatt szünetel.',job['batch_id']))


if __name__ == '__main__':
    import os
    os.nice(10)
    main()
