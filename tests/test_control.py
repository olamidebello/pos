import sqlite3
import tempfile
import unittest
from pathlib import Path
from control import Control

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.c=Control(str(Path(self.temp.name)/'test.db'))
        self.c.bootstrap('root@example.test','a-long-test-password')
        self.root=self.c.actor(self.c.login('root@example.test','a-long-test-password'))
        self.t=self.c.mutate(self.root,'tenants',{'name':'First'})['id']
        self.other=self.c.mutate(self.root,'tenants',{'name':'Other'})['id']
        self.s=self.store(self.t,0); self.s2=self.store(self.other,50)
        uid=self.c.mutate(self.root,'users',{'email':'staff@example.test','password':'a-long-test-password'})['id']
        self.c.mutate(self.root,'memberships',{'user_id':uid,'tenant_id':self.t,'store_id':self.s,'role':'store_manager'})
        self.staff=self.c.actor(self.c.login('staff@example.test','a-long-test-password'))
    def tearDown(self):
        self.c.db.close();self.temp.cleanup()
    def store(self,tenant,lat):
        return self.c.mutate(self.root,'stores',dict(tenant_id=tenant,name='Store',address='Street',country='NG',currency='NGN',timezone='Africa/Lagos',language='yo',lat=lat,lon=0))['id']
    def stock(self,store):
        return dict(store_id=store,barcode='12345',name='Water',quantity=20,price_minor=40000)
    def test_inventory_inherits_location(self):
        self.c.mutate(self.staff,'inventory',self.stock(self.s))
        row=self.c.catalog(self.staff,self.s)[0]
        self.assertEqual((row['lat'],row['address'],row['currency']),(0,'Street','NGN'))
        self.c.mutate(self.staff,'stores',dict(id=self.s,tenant_id=self.t,name='Moved',address='New street',country='NG',currency='NGN',timezone='UTC',language='en',lat=30,lon=-90))
        self.assertEqual(self.c.catalog(self.staff,self.s)[0]['lat'],30)
    def test_cross_tenant_denied(self):
        with self.assertRaises(PermissionError): self.c.catalog(self.staff,self.s2)
        with self.assertRaises(PermissionError): self.c.mutate(self.staff,'inventory',self.stock(self.s2))
        self.assertEqual([s['id'] for s in self.c.snapshot(self.staff)['stores']],[self.s])
    def test_manager_cannot_escalate(self):
        for resource,data in [('users',{'email':'x'}),('tenants',{'name':'x'}),('memberships',{'tenant_id':self.t,'user_id':self.staff['id'],'role':'tenant_admin'}),('features',{'scope':'global','feature':'autopay','enabled':True})]:
            with self.assertRaises(PermissionError): self.c.mutate(self.staff,resource,data)
    def test_same_barcode_separate_stock(self):
        self.c.mutate(self.root,'inventory',self.stock(self.s))
        self.c.mutate(self.root,'inventory',self.stock(self.s2))
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM inventory').fetchone()[0],2)
    def test_inventory_cannot_be_reassigned(self):
        identifier=self.c.mutate(self.root,'inventory',self.stock(self.s))['id']
        with self.assertRaises(PermissionError): self.c.mutate(self.root,'inventory',dict(self.stock(self.s2),id=identifier))
    def test_last_super_admin_preserved(self):
        with self.assertRaises(ValueError): self.c.mutate(self.root,'users',dict(id=self.root['id'],email=self.root['email'],super=False))
    def test_sessions_revoked(self):
        token=self.c.login('staff@example.test','a-long-test-password')
        self.c.mutate(self.root,'users',dict(id=self.staff['id'],email=self.staff['email'],active=False))
        with self.assertRaises(PermissionError): self.c.actor(token)
    def test_lockout(self):
        for _ in range(5):
            with self.assertRaises(PermissionError): self.c.login('staff@example.test','wrong-password-value')
        with self.assertRaises(PermissionError): self.c.login('staff@example.test','a-long-test-password')
    def test_suspension_hides_discovery(self):
        self.c.mutate(self.root,'tenants',dict(id=self.t,name='First',status='suspended'))
        self.assertEqual([s['id'] for s in self.c.discover(0,0)],[self.s2])
    def test_audit_protected(self):
        self.assertNotIn('a-long-test-password',' '.join(x[0] for x in self.c.db.execute('SELECT detail FROM audit')))
        with self.assertRaises(sqlite3.IntegrityError): self.c.db.execute('DELETE FROM audit')
    def test_invalid_location(self):
        with self.assertRaises(ValueError): self.c.discover(float('nan'),0)
    def test_feature_parent_disable_cannot_be_overridden(self):
        self.c.mutate(self.root,'features',dict(scope='store:'+self.s,feature='autopay',enabled=True))
        store=dict(self.c.db.execute('SELECT * FROM stores WHERE id=?',(self.s,)).fetchone())
        self.assertFalse(self.c.effective_features(store)['autopay'])
        self.c.mutate(self.root,'features',dict(scope='global',feature='autopay',enabled=True))
        self.assertTrue(self.c.effective_features(store)['autopay'])
        self.c.mutate(self.root,'features',dict(scope='tenant:'+self.t,feature='autopay',enabled=False))
        self.assertFalse(self.c.effective_features(store)['autopay'])
    def test_membership_removal(self):
        self.c.mutate(self.root,'memberships',dict(user_id=self.staff['id'],tenant_id=self.t,store_id=self.s,role='store_manager',remove=True))
        with self.assertRaises(PermissionError): self.c.catalog(self.staff,self.s)
    def test_suspension_blocks_staff_writes(self):
        self.c.mutate(self.root,'tenants',dict(id=self.t,name='First',status='suspended'))
        with self.assertRaises(PermissionError): self.c.mutate(self.staff,'inventory',self.stock(self.s))
    def test_public_storefront_inherits_address_and_hides_suspension(self):
        self.c.mutate(self.root,'inventory',self.stock(self.s))
        row=self.c.public_catalog(self.s)[0]
        self.assertEqual(row['address'],'Street')
        self.assertNotIn('tenant_id',row)
        self.c.mutate(self.root,'tenants',dict(id=self.t,name='First',status='suspended'))
        with self.assertRaises(ValueError): self.c.public_catalog(self.s)
    def test_nearest_store_order(self):
        self.assertEqual(self.c.discover(0,0)[0]['id'],self.s)
        self.assertEqual(self.c.discover(50,0)[0]['id'],self.s2)
