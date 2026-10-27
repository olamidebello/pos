import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from deploy.install_global_pos_store import install,frontend,backend,DOMAIN,discover
from deploy.ssl_preflight import check_dns,vhost

ROOT=Path(__file__).resolve().parents[1]
class StorePublishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.code=self.root/'code';(self.code/'static').mkdir(parents=True)
        self.js=self.code/'static/app.js';self.original=(ROOT/'tests/fixtures/store-hosted.js').read_text();self.js.write_text(self.original)
        (self.code/'app.py').write_text((ROOT/'tests/fixtures/store-static.py.txt').read_text())
        self.original_backend=(self.code/'app.py').read_bytes()
        self.database=self.root/'store.sqlite3';self.state=self.root/'state'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE hosted_apps(domain TEXT PRIMARY KEY,metadata TEXT NOT NULL,visible INTEGER NOT NULL DEFAULT 1,updated INTEGER NOT NULL)')
            db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,email TEXT)')
            db.execute('INSERT INTO users VALUES(1,?)',('existing@store.test',))
            db.execute('INSERT INTO hosted_apps VALUES(?,?,?,?)',('other.example',json.dumps({'name':'Preserve me'}),0,42))
    def tearDown(self):self.temp.cleanup()
    def run_install(self,**kwargs):return install(self.database,self.code,ROOT/'catalog/global-pos.json',self.state,**kwargs)
    def test_add_and_idempotent_update_preserves_other_data(self):
        first=self.run_install();self.assertTrue(first['changed']);self.assertTrue(first['visible'])
        second=self.run_install();self.assertFalse(second['changed'])
        self.assertIn('/static/global-pos-icon.png',self.js.read_text())
        self.assertEqual(self.js.read_text().count('Try Global POS demo'),1)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT visible,updated FROM hosted_apps WHERE domain=?',('other.example',)).fetchone(),(0,42))
            self.assertEqual(db.execute('SELECT email FROM users').fetchone()[0],'existing@store.test')
        self.assertEqual(len(list(self.state.glob('backup-*'))),1)
    def test_backend_restart_only_when_static_compatibility_changes(self):
        calls=[]
        self.run_install(restart=lambda:calls.append('restart'))
        self.assertEqual(calls,['restart'])
        self.run_install(restart=lambda:calls.append('restart'))
        self.assertEqual(calls,['restart'])
        source=(self.code/'app.py').read_text()
        self.assertEqual(backend(source),source)
        header=next(line for line in source.splitlines() if 'self.headers_out(' in line)
        self.assertIn("else 'image/png' if path.endswith('.png')",header)
    def test_failed_verification_restores_backend_and_restarts_restored_version(self):
        calls=[]
        def fail(visible):raise ValueError('Unhealthy')
        with self.assertRaises(ValueError):self.run_install(restart=lambda:calls.append('restart'),verify=fail)
        self.assertEqual(calls,['restart','restart'])
        self.assertEqual((self.code/'app.py').read_bytes(),self.original_backend)
    def test_preserve_hidden_listing_and_allow_explicit_publish(self):
        self.run_install()
        with sqlite3.connect(self.database) as db:db.execute('UPDATE hosted_apps SET visible=0 WHERE domain=?',(DOMAIN,))
        seen=[];result=self.run_install(verify=lambda visible:seen.append(visible))
        self.assertFalse(result['visible']);self.assertEqual(seen,[False])
        self.assertTrue(self.run_install(publish=True)['visible'])
    def test_verification_failure_rolls_back_only_own_changes(self):
        def fail(visible):
            with sqlite3.connect(self.database) as db:db.execute('INSERT INTO users VALUES(2,?)',('new@store.test',))
            raise ValueError('Public verification failed')
        with self.assertRaises(ValueError):self.run_install(verify=fail)
        with sqlite3.connect(self.database) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM hosted_apps WHERE domain=?',(DOMAIN,)).fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],2)
        self.assertEqual(self.js.read_text(),self.original)
        self.assertEqual((self.code/'app.py').read_bytes(),self.original_backend)
    def test_unrecognized_frontend_and_missing_db_fail_without_writes(self):
        self.js.write_text('do not overwrite')
        with self.assertRaises(ValueError):self.run_install()
        self.assertEqual(self.js.read_text(),'do not overwrite')
        with self.assertRaises(ValueError):install(self.root/'missing.db',self.code,ROOT/'catalog/global-pos.json',self.state)
        self.assertFalse((self.root/'missing.db').exists())
    def test_discovery_uses_effective_process_environment(self):
        proc=self.root/'proc';process=proc/'123';process.mkdir(parents=True)
        (process/'cwd').symlink_to(self.code,target_is_directory=True)
        (process/'environ').write_bytes(('STORE_DATA='+str(self.root)+'\0PORT=9090\0SECRET=never-print').encode())
        props={'WorkingDirectory':'/unused','MainPID':'123','Environment':'STORE_DATA=/wrong PORT=8080'}
        database,code,port=discover(props.get,proc)
        self.assertEqual((database,code,port),(self.database,self.code,9090))

class SSLPreflightTests(unittest.TestCase):
    def test_stale_aaaa_fails_before_certificate_changes(self):
        def resolve(*args,**kwargs):return [(0,0,0,'',('8.8.8.8',80)),(0,0,0,'',('2606:4700:4700::1111',80))]
        with self.assertRaises(ValueError):check_dns('pos.africandroid.world','8.8.8.8',[],resolve)
        self.assertEqual(len(check_dns('pos.africandroid.world','8.8.8.8',['2606:4700:4700::1111'],resolve)),2)
    def test_dedicated_vhost_required(self):
        text='# configuration file /etc/nginx/sites-enabled/test:\nserver_name pos.africandroid.world;\nserver_name pos.africandroid.world;'
        self.assertTrue(vhost(text,'pos.africandroid.world').endswith('/test'))
        with self.assertRaises(ValueError):vhost(text.replace('server_name pos.africandroid.world;','server_name pos.africandroid.world unrelated.example;'),'pos.africandroid.world')
        with self.assertRaises(ValueError):vhost('server_name unrelated.example;','pos.africandroid.world')
