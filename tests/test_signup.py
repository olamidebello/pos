import tempfile
import unittest
from pathlib import Path
from control import Control

class SignupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.c=Control(str(Path(self.temp.name)/'control.db'))
        self.data=dict(email='Owner@example.test',password='a-long-private-password',business_name='My business',accept_terms=True,super=True)
    def tearDown(self):
        self.c.db.close();self.temp.cleanup()
    def test_scoped_account_and_bootstrap_remains_separate(self):
        self.c.signup(self.data)
        actor=self.c.actor(self.c.login('owner@example.test',self.data['password']))
        self.assertFalse(actor['super'])
        self.assertEqual(len(self.c.snapshot(actor)['tenants']),1)
        with self.assertRaises(PermissionError):self.c.mutate(actor,'users',self.data)
        self.c.bootstrap('admin@example.test','another-private-password')
        with self.assertRaises(ValueError):self.c.bootstrap('other@example.test','another-private-password')
    def test_duplicate_is_atomic(self):
        self.c.signup(self.data)
        with self.assertRaises(ValueError):self.c.signup(self.data)
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM tenants').fetchone()[0],1)
    def test_consent_email_password_required(self):
        for patch in ({'accept_terms':False},{'email':'invalid'},{'password':'short'}):
            with self.assertRaises(ValueError):self.c.signup(dict(self.data,**patch))
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)
    def test_tenants_are_isolated(self):
        self.c.signup(self.data)
        self.c.signup(dict(self.data,email='other@example.test'))
        actor=self.c.actor(self.c.login('owner@example.test',self.data['password']))
        snapshot=self.c.snapshot(actor)
        self.assertEqual(len(snapshot['tenants']),1)
        other=self.c.db.execute('SELECT id FROM tenants WHERE id!=?',(snapshot['tenants'][0]['id'],)).fetchone()[0]
        with self.assertRaises(PermissionError):self.c.authorize(actor,other,write=True)
