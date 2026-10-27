#!/usr/bin/env python3
"""Update only Global POS in an existing Africa Apps Store installation.

No store code checkout, package upgrade or full database restore. A bounded
restart is needed only when the existing static-image allowlist is extended.
"""
import argparse
import fcntl
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

DOMAIN = 'pos.africandroid.world'
STORE_ORIGIN = 'https://africanappsstore.space'


def discover(prop=None, proc=Path('/proc')):
    prop = prop or (lambda name: subprocess.check_output(
        ['systemctl', 'show', 'africanappsstore.service', '-p', name, '--value'], text=True).strip())
    code = Path(prop('WorkingDirectory'))
    pid = prop('MainPID')
    env = dict(part.split('=', 1) for part in shlex.split(prop('Environment')) if '=' in part)
    if not pid.isdigit() or int(pid) <= 0:
        raise ValueError('Apps Store service must already be running')
    # Read only the needed effective environment values; never print it.
    for entry in (proc / pid / 'environ').read_bytes().split(b'\0'):
        key, sep, value = entry.partition(b'=')
        if sep and key in (b'STORE_DATA', b'PORT'):
            env[key.decode()] = value.decode()
    code = (proc / pid / 'cwd').resolve(strict=True)
    if not code.is_absolute() or not (code / 'static/app.js').is_file():
        raise ValueError('Running store frontend not found')
    data = Path(env.get('STORE_DATA', str(code / 'data')))
    if not data.is_absolute():
        data = code / data
    database = data / 'store.sqlite3'
    port = int(env.get('PORT', '8080'))
    if not database.is_file() or not 1 <= port <= 65535:
        raise ValueError('Running store database or port not found')
    return database, code, port


def metadata(path):
    value = json.loads(Path(path).read_text())
    if value.get('domain') != DOMAIN or value.get('artifact_kind') != 'global-pos-pwa' or value.get('apk_url') is not None:
        raise ValueError('Expected only the Global POS web-app listing')
    expected = {'website': '/', 'install_url': '/install.html', 'demo_url': '/demo.html',
                'privacy': '/privacy.html', 'terms_url': '/terms.html'}
    if any(value.get(key) != 'https://' + DOMAIN + suffix for key, suffix in expected.items()):
        raise ValueError('Unexpected Global POS URL')
    if value.get('icon_url') != STORE_ORIGIN+'/static/global-pos-icon.png' or value.get('artwork_url') != STORE_ORIGIN+'/static/global-pos-world.png':
        raise ValueError('Unexpected store asset URL')
    if not isinstance(value.get('features'), list) or not value['features']:
        raise ValueError('Missing listing features')
    return value


def frontend(text):
    start = text.find('async function hostedCards')
    if start < 0:
        raise ValueError('Unsupported Apps Store frontend: hostedCards missing')
    before, hosted = text[:start], text[start:]
    anchor = '<h3>${esc(a.name)}</h3>'
    if anchor not in hosted:
        raise ValueError('Unsupported hosted card template')
    icon = "${a.domain==='pos.africandroid.world'?'<img src=\"/static/global-pos-icon.png\" alt=\"Global POS\" width=\"120\" height=\"120\">':''}"
    if icon not in hosted:
        hosted = hosted.replace(anchor, icon + anchor, 1)
    # Give this PWA its correct platform text without changing other cards.
    if "'pos.africandroid.world'].includes(a.domain)?esc(a.platforms)" not in hosted:
        ending = '].includes(a.domain)?esc(a.platforms)'
        if ending not in hosted:
            raise ValueError('Unsupported platform label; update the store frontend first')
        hosted = hosted.replace(ending, ",'pos.africandroid.world'" + ending, 1)
    website = '<a href="${esc(a.website)}" target="_blank" rel="noopener">Open website</a>'
    if website not in hosted:
        raise ValueError('Unsupported store website-link template')
    if 'Install on your device' not in hosted:
        install = "${a.install_url&&a.install_url.startsWith('https://')?`<a class=\"button\" href=\"${esc(a.install_url)}\" target=\"_blank\" rel=\"noopener noreferrer\">Install on your device</a>`:''}"
        hosted = hosted.replace(website, install + website, 1)
    demo = "${a.domain==='pos.africandroid.world'?'<a class=\"button\" href=\"https://pos.africandroid.world/demo.html\" target=\"_blank\" rel=\"noopener noreferrer\">Try Global POS demo</a>':''}"
    if demo not in hosted:
        hosted = hosted.replace(website, demo + website, 1)
    return before + hosted


def backend(text):
    pattern=r"(?m)^([ \t]*)if self.command in \('GET','HEAD'\) and path in \(([^\n]+)\):$"
    matches=[match for match in re.finditer(pattern,text) if repr('/static/app.js') in match[2] and repr('/static/style.css') in match[2]]
    if len(matches)!=1:raise ValueError('Unsupported store static allowlist; refusing to replace its backend')
    match=matches[0]
    paths=match[2]
    for path in ('/static/global-pos-icon.png','/static/global-pos-world.png'):
        if repr(path) not in paths:paths+=','+repr(path)
    text=text[:match.start()]+match[1]+"if self.command in ('GET','HEAD') and path in ("+paths+"):"+text[match.end():]
    png="else 'image/png' if path.endswith('.png') "
    anchor="else 'image/jpeg' if path.endswith('.jpg')"
    route=text.index("if self.command in ('GET','HEAD') and path in ("+paths+"):")
    header_start=text.index('self.headers_out(',route)
    header_end=text.index('\n',header_start)
    header=text[header_start:header_end]
    if png not in header:
        if anchor not in header:raise ValueError('Unsupported store image MIME handler')
        header=header.replace(anchor,png+anchor,1)
        text=text[:header_start]+header+text[header_end:]
    compile(text,'store-app.py','exec')
    return text


def atomic_write(path, content, original_stat):
    fd, name = tempfile.mkstemp(prefix='.global-pos-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.S_IMODE(original_stat.st_mode))
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), original_stat.st_uid, original_stat.st_gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(database, code, manifest, state, publish=False, verify=None, restart=None):
    database, code, state = Path(database), Path(code), Path(state)
    if not database.is_file():
        raise ValueError('Existing store database missing; refusing to create a new database')
    meta = metadata(manifest)
    target = code / 'static/app.js'
    service = code / 'app.py'
    if target.is_symlink() or service.is_symlink():raise ValueError('Explicitly reconcile symlinked store source files before updating')
    old_service=service.read_bytes();service_stat=service.stat()
    new_service=backend(old_service.decode()).encode()
    old = target.read_bytes()
    old_stat = target.stat()
    new = frontend(old.decode()).encode()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    with (state / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(database, timeout=20) as db:
            db.execute('SELECT domain,metadata,visible,updated FROM hosted_apps LIMIT 0')
            previous = db.execute('SELECT metadata,visible,updated FROM hosted_apps WHERE domain=?', (DOMAIN,)).fetchone()
            visible = 1 if publish or previous is None else previous[1]
            same = previous is not None and json.loads(previous[0]) == meta and previous[1] == visible
            if same and new == old and new_service == old_service:
                if verify:
                    verify(bool(visible))
                return dict(changed=False, domain=DOMAIN, visible=bool(visible), status='already_current')
            backup = state / ('backup-' + str(time.time_ns()))
            backup.mkdir(mode=0o700)
            with sqlite3.connect(backup / 'store.sqlite3') as copy:
                db.backup(copy)
            os.chmod(backup / 'store.sqlite3', 0o600)
            (backup / 'app.py').write_bytes(old_service)
            os.chmod(backup / 'app.py', 0o600)
            (backup / 'app.js').write_bytes(old)
            os.chmod(backup / 'app.js', 0o600)
            encoded = json.dumps(meta, sort_keys=True)
            updated = int(time.time())
            try:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT metadata,visible,updated FROM hosted_apps WHERE domain=?', (DOMAIN,)).fetchone()
                if current != previous or target.read_bytes() != old or service.read_bytes() != old_service:
                    raise ValueError('Store changed during preparation; rerun the update')
                db.execute('INSERT INTO hosted_apps(domain,metadata,visible,updated) VALUES(?,?,?,?) ON CONFLICT(domain) DO UPDATE SET metadata=excluded.metadata,visible=excluded.visible,updated=excluded.updated', (DOMAIN, encoded, visible, updated))
                if new != old:
                    atomic_write(target, new, old_stat)
                if new_service != old_service:
                    atomic_write(service,new_service,service_stat)
                db.commit()
                if new_service != old_service and restart:
                    restart()
                if verify:
                    verify(bool(visible))
            except Exception:
                db.rollback()
                # Restore only our row, with a comparison guard. Never restore the
                # whole DB: that could discard concurrent accounts and other apps.
                with db:
                    current = db.execute('SELECT metadata,visible,updated FROM hosted_apps WHERE domain=?', (DOMAIN,)).fetchone()
                    if current == (encoded, visible, updated):
                        if previous:
                            db.execute('UPDATE hosted_apps SET metadata=?,visible=?,updated=? WHERE domain=?', (*previous, DOMAIN))
                        else:
                            db.execute('DELETE FROM hosted_apps WHERE domain=?', (DOMAIN,))
                if new != old and target.read_bytes() == new:
                    atomic_write(target, old, old_stat)
                if new_service != old_service and service.read_bytes() == new_service:
                    atomic_write(service,old_service,service_stat)
                    if restart:restart()
                raise
    return dict(changed=True, domain=DOMAIN, visible=bool(visible), status='listing_updated', backup=str(backup))


def fetch_json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=20) as response:
        if response.status != 200:
            raise ValueError('Catalog HTTP health check failed')
        return json.load(response)


def verify_catalog(url, visible):
    rows = fetch_json(url + '?global_pos_check=' + str(time.time_ns()))
    if not isinstance(rows, list):
        raise ValueError('Store hosted-app API must return a list')
    matches = [row for row in rows if row.get('domain') == DOMAIN]
    if not visible:
        if matches:
            raise ValueError('Hidden listing unexpectedly public')
        return
    if len(matches) != 1 or matches[0].get('install_url') != 'https://' + DOMAIN + '/install.html' or matches[0].get('artifact_kind') != 'global-pos-pwa':
        raise ValueError('Visible Global POS catalog listing is missing or stale')


def verify_assets(origin):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name in ('global-pos-icon.png','global-pos-world.png'):
        request=urllib.request.Request(origin+'/static/'+name,method='HEAD')
        with opener.open(request,timeout=20) as response:
            if response.status!=200 or response.headers.get_content_type()!='image/png':raise ValueError('Store branding is not served as PNG')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--discover', action='store_true')
    parser.add_argument('--database')
    parser.add_argument('--code')
    parser.add_argument('--manifest')
    parser.add_argument('--inspect', action='store_true', help='Print only detected code/data paths and port')
    parser.add_argument('--state', default='/var/lib/global-pos-store-sync')
    parser.add_argument('--publish', action='store_true', help='Explicitly unhide only Global POS')
    parser.add_argument('--verify-public', action='store_true')
    args = parser.parse_args()
    if args.inspect:
        database, code, port = discover()
        print(json.dumps(dict(database=str(database),code=str(code),port=port)))
        return
    if not args.manifest: parser.error('--manifest required for an update')
    database, code, port = args.database, args.code, None
    if args.discover:
        database, code, port = discover()
    if not database or not code:
        parser.error('Use --discover, or supply both --database and --code')
    def verify(visible):
        if port is not None:
            local='http://127.0.0.1:' + str(port)
            for attempt in range(10):
                try:
                    verify_catalog(local+'/api/hosted-apps',visible)
                    verify_assets(local)
                    break
                except Exception:
                    if attempt==9:raise
                    time.sleep(0.5)
        if args.verify_public:
            verify_catalog(STORE_ORIGIN + '/api/hosted-apps', visible)
            verify_assets(STORE_ORIGIN)
    def restart():
        if not args.discover:raise ValueError('Backend changes require --discover for a bounded service restart')
        subprocess.run(['systemctl','restart','africanappsstore.service'],check=True)
    result = install(database, code, args.manifest, args.state, args.publish, verify=verify, restart=restart)
    print(json.dumps(result))


if __name__ == '__main__':
    os.umask(0o077)
    main()
