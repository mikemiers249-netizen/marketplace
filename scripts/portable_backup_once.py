"""One-time read-only export to protected persistent storage before web startup."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile

import psycopg2
from psycopg2 import sql


def main():
    root = Path(__file__).resolve().parents[1]
    uploads = Path(os.environ.get('UPLOAD_FOLDER', '/app/app/static/uploads'))
    output = uploads / '.private' / 'portable-backup-20261009'
    marker = output / 'COMPLETE.json'
    if marker.exists():
        print('Portable backup: existing completed export retained.', flush=True)
        return
    output.mkdir(parents=True, exist_ok=True)
    uri = os.environ.get('DATABASE_URI') or os.environ.get('DATABASE_URL')
    if not uri:
        raise RuntimeError('Missing database configuration')
    uri = uri.replace('postgresql+psycopg2://', 'postgresql://', 1)
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='wimli-backup-') as temporary:
        temp = Path(temporary)
        with psycopg2.connect(uri, connect_timeout=15) as connection:
            connection.set_session(isolation_level='REPEATABLE READ')
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '15s'")
                cursor.execute("SELECT schemaname, tablename FROM pg_tables "
                               "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                               "ORDER BY schemaname, tablename")
                tables = cursor.fetchall()
                if tables:
                    # Prevent business writes during the database + file snapshot.
                    cursor.execute(sql.SQL('LOCK TABLE {} IN SHARE MODE').format(
                        sql.SQL(', ').join(sql.Identifier(schema, name) for schema, name in tables)))
                cursor.execute('SELECT pg_export_snapshot()')
                snapshot = cursor.fetchone()[0]
                env = os.environ.copy()
                # Unlike psycopg2, pg_dump does not expand a URI supplied through
                # PGDATABASE. Pass parsed libpq parameters via individual env keys.
                pg_names = {'dbname': 'PGDATABASE', 'application_name': 'PGAPPNAME'}
                for key, value in psycopg2.extensions.parse_dsn(uri).items():
                    env[pg_names.get(key, 'PG' + key.upper())] = value
                env['PGCONNECT_TIMEOUT'] = '15'
                result = subprocess.run(['pg_dump', '--format=custom', '--snapshot=' + snapshot,
                    '--file=' + str(temp / 'mp.dump')], env=env, capture_output=True)
                if result.returncode:
                    # Keep diagnostic details private and strip credentials before logging.
                    detail = result.stderr.decode(errors='replace').replace(uri, '[database]')
                    for item in psycopg2.extensions.parse_dsn(uri).values():
                        if item and len(item) > 3:
                            detail = detail.replace(item, '[redacted]')
                    (output / 'FAILED.json').write_text(json.dumps(dict(
                        client=subprocess.check_output(['pg_dump', '--version']).decode().strip(),
                        server=connection.server_version, error=detail), indent=2))
                    raise RuntimeError('pg_dump failed: ' + detail[:1500])
                counts = {}
                for schema, name in tables:
                    cursor.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(schema, name)))
                    counts[schema + '.' + name] = cursor.fetchone()[0]
                file_manifest = []
                with zipfile.ZipFile(temp / 'uploads.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(uploads.rglob('*')):
                        if output in path.parents or not path.is_file():
                            continue
                        if path.is_symlink():
                            raise RuntimeError('Unexpected symlink in uploads')
                        before = path.stat()
                        data = path.read_bytes()
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise RuntimeError('Upload changed during snapshot')
                        relative = path.relative_to(uploads).as_posix()
                        archive.writestr(relative, data)
                        file_manifest.append(dict(path=relative, bytes=len(data),
                                                  sha256=hashlib.sha256(data).hexdigest()))
                manifest = dict(started_utc=started, completed_utc=datetime.now(timezone.utc).isoformat(),
                    table_rows=counts, files=file_manifest, consistency='database SHARE locks + exported snapshot')
                (temp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        # Locks released; no data updates were made by this exporter.
        settings = set(re.findall(r'_env(?:_bool|_int)?\("([A-Z0-9_]+)"',
                                  (root / 'config.py').read_text()))
        settings.update(['DATABASE_URI', 'DATABASE_URL', 'PORT', 'APP_CONFIG'])
        config = {key: os.environ[key] for key in sorted(settings) if key in os.environ}
        (temp / 'environment.json').write_text(json.dumps(config, indent=2))
        result = subprocess.run(['pg_restore', '--list', str(temp / 'mp.dump')], capture_output=True)
        if result.returncode:
            raise RuntimeError('pg_restore archive validation failed')
        (temp / 'database-contents.txt').write_bytes(result.stdout)
        for name in ['uploads.zip']:
            with zipfile.ZipFile(temp / name) as archive:
                if archive.testzip():
                    raise RuntimeError('Upload archive validation failed')
        bundle = output / 'portable-data.zip.part'
        with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(temp.iterdir()):
                archive.write(path, path.name)
        with zipfile.ZipFile(bundle) as archive:
            if archive.testzip():
                raise RuntimeError('Bundle validation failed')
        bundle.rename(output / 'portable-data.zip')
        marker.write_text(json.dumps(dict(completed_utc=manifest['completed_utc'],
            sha256=hashlib.sha256((output / 'portable-data.zip').read_bytes()).hexdigest(),
            tables=len(counts), files=len(file_manifest)), indent=2))
        print(f'Portable backup complete: {len(counts)} tables, {len(file_manifest)} files.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Export failure must never prevent the marketplace from starting.
        detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print('Portable backup failed: ' + detail, flush=True)
