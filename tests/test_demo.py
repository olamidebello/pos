import tempfile
import unittest
from pathlib import Path
from control import Control, DemoManager, DEMO_EMAIL, DEMO_PASSWORD

class DemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'live.db')
        self.live=Control(self.path);self.live.bootstrap('owner@example.test','a-real-owner-password')
        self.manager=DemoManager(self.path)
    def tearDown(self):self.live.db.close();self.tmp.cleanup()
    def start(self):return self.manager.create(DEMO_EMAIL,DEMO_PASSWORD)
    def test_sample_super_admin_separate_from_production(self):
        session=self.start();demo=self.manager.open(session['demo_id'])
        try:
            actor=demo.actor(session['token']);self.assertEqual(actor['super'],1)
            snapshot=demo.snapshot(actor)
            self.assertEqual(len(snapshot['tenants']),2);self.assertEqual(len(snapshot['inventory']),4)
            demo.mutate(actor,'tenants',{'name':'Demo edit'})
            self.assertEqual(self.live.db.execute('SELECT COUNT(*) FROM tenants').fetchone()[0],0)
            with self.assertRaises(PermissionError):self.live.actor(session['token'])
            with self.assertRaises(PermissionError):self.live.login(DEMO_EMAIL,DEMO_PASSWORD)
        finally:demo.db.close()
    def test_sessions_are_independent(self):
        first=self.start();second=self.start();one=self.manager.open(first['demo_id']);two=self.manager.open(second['demo_id'])
        try:
            one.mutate(one.actor(first['token']),'tenants',{'name':'Only first visitor'})
            self.assertEqual(two.db.execute('SELECT COUNT(*) FROM tenants').fetchone()[0],2)
            with self.assertRaises(PermissionError):two.actor(first['token'])
        finally:one.db.close();two.db.close()
    def test_expiry_and_cleanup(self):
        session=self.start();demo=self.manager.open(session['demo_id'])
        with demo.db:demo.db.execute('UPDATE demo_metadata SET expires=0')
        demo.db.close()
        with self.assertRaises(PermissionError):self.manager.open(session['demo_id'])
        self.start();self.assertFalse((self.manager.directory/(session['demo_id']+'.db')).exists())
        self.assertTrue(Path(self.path).exists())
    def test_bad_credentials_and_path_rejected(self):
        with self.assertRaises(PermissionError):self.manager.create(DEMO_EMAIL,'wrong')
        for identifier in ('../live','0'*31,'0'*32):
            with self.assertRaises(PermissionError):self.manager.open(identifier)
