"""A single durable queue worker, independent of browser requests."""
import fcntl
import time
import voice_builder_state as state
from voice_builder_engine import run_job, Paused, ProviderError


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
                row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    c.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
            if not row:
                idle += 1
                time.sleep(1)
                continue
            idle = 0
            job = state.public_job(row)
            try:
                with state.locked(job['id'] + '.lock'):
                    run_job(job)
            except Paused:
                state.log(job['id'], 'Feladat szüneteltetve. A kész hangok megmaradtak.')
            except Exception as err:
                # Unexpected exceptions can contain provider responses or credentials.
                message = str(err) if isinstance(err, (ProviderError, RuntimeError, ValueError)) else 'A feldolgozás megszakadt. A kész fájlok megmaradtak.'
                state.update(job['id'], status='failed', error=message[:500])
                state.log(job['id'], message[:500])


if __name__ == '__main__':
    import os
    os.nice(10)
    main()
