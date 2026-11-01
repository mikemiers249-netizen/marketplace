"""Start a new loopback-only disposable cluster; never read a production DSN.

Windows: install @embedded-postgres/windows-x64 into .sandbox/pg-runtime first.
Artifacts are retained for inspection. No recursive deletion is performed.
"""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

import psycopg2
from psycopg2 import sql


def main():
    workspace = Path(__file__).resolve().parents[1]
    binary = workspace / '.sandbox/pg-runtime/node_modules/@embedded-postgres/windows-x64/native/bin'
    if not (binary / 'initdb.exe').is_file():
        raise SystemExit('Install the pinned Windows PostgreSQL runtime described in docs/tariffs-v2.md.')
    run_root = (workspace / '.sandbox/pg-runs').resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    run = run_root / ('run-' + secrets.token_hex(8))
    run.mkdir()
    cluster = run / 'cluster'
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    def execute(name, args):
        output = run / (name + '-output.log')
        # A background Windows server can inherit pipe handles; communicate()
        # would then wait for its lifetime. Redirect to a file, never a PIPE.
        with output.open('ab') as stream:
            result = subprocess.run([str(binary / (name + '.exe')), *args], cwd=workspace,
                creationflags=flags, stdout=stream, stderr=subprocess.STDOUT, timeout=60)
        if result.returncode:
            raise RuntimeError(output.read_text(encoding='utf-8', errors='replace')[-4000:])
    print('Initializing a new local disposable PostgreSQL cluster.', flush=True)
    execute('initdb', ['-D', str(cluster), '-U', 'wimli_test_bootstrap', '-A', 'trust', '--encoding=UTF8', '--locale=C'])
    started = False
    try:
        started = True
        execute('pg_ctl', ['-D', str(cluster), '-l', str(run / 'postgres.log'), '-o', f'-h 127.0.0.1 -p {port}', '-w', 'start'])
        suffix = secrets.token_hex(6)
        identity = 'wimli_tariffs_v2_test_' + suffix
        connection = psycopg2.connect(host='127.0.0.1', port=port, dbname='postgres', user='wimli_test_bootstrap')
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(identity)))
                cursor.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(identity), sql.Identifier(identity)))
                cursor.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '12s'").format(sql.Identifier(identity)))
                cursor.execute(sql.SQL("ALTER ROLE {} SET lock_timeout = '6s'").format(sql.Identifier(identity)))
                cursor.execute('SELECT version()')
                print(cursor.fetchone()[0], flush=True)
        finally:
            connection.close()
        environment = dict(os.environ)
        # Ignore any application DATABASE_URI. Only this new test identity is passed.
        environment['WIMLI_TARIFF_V2_PG_DSN'] = f'postgresql+psycopg2://{identity}@127.0.0.1:{port}/{identity}'
        test_log = run / 'tests.log'
        with test_log.open('wb') as stream:
            result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                '-p', 'test_tariffs_v2_postgres.py', '-v'], cwd=workspace, env=environment,
                stdout=stream, stderr=subprocess.STDOUT, creationflags=flags, timeout=240)
        print(test_log.read_text(encoding='utf-8', errors='replace'), flush=True)
        return result.returncode
    finally:
        # The stop target is the just-created exact cluster, never a service name.
        if started and (cluster / 'postmaster.pid').exists():
            if not cluster.resolve().is_relative_to(run_root):
                raise RuntimeError('Unexpected cluster path; refusing stop command.')
            execute('pg_ctl', ['-D', str(cluster), '-m', 'fast', '-w', 'stop'])
        print(f'Local cluster stopped. Artifacts retained: {run}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
