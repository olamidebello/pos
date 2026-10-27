import json
import os
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from control import Control
from commerce import Commerce
import gateways

class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'db.sqlite3')
        self.c=Control(self.path,demo=True);self.c.bootstrap('root@example.test','long-test-password')
        self.root=self.c.actor(self.c.login('root@example.test','long-test-password'))
        for feature in ('marketplace','payments','integrations'):self.c.mutate(self.root,'features',dict(scope='global',feature=feature,enabled=True))
        self.tenant=self.c.mutate(self.root,'tenants',dict(name='Market'))['id']
        self.store=self.c.mutate(self.root,'stores',dict(tenant_id=self.tenant,name='Lagos Store',address='Example Lagos',country='NG',currency='NGN',timezone='Africa/Lagos',language='en',lat=6.5,lon=3.4))['id']
        self.item=self.c.mutate(self.root,'inventory',dict(store_id=self.store,name='Water',barcode='123',quantity=10,price_minor=40000))['id']
        self.c.mutate(self.root,'marketplace',dict(store_id=self.store,enabled=True,category='Groceries',description='Sample'))
        self.gateway=self.c.mutate(self.root,'gateways',dict(store_id=self.store,name='Demo',provider='demo',mode='test',enabled=True))['id']
        self.engine=Commerce(self.c,demo=True)
    def tearDown(self):self.c.db.close();self.temp.cleanup()
    def basket(self,key=None):
        meta=self.engine.marketplace()
        return dict(store_id=self.store,gateway_id=self.gateway,email='sample@example.test',checkout_key=key or secrets.token_hex(32),accepted=True,terms_version=meta['terms_version'],terms_digest=meta['terms_digest'],items=[dict(id=self.item,quantity=2)])
    def test_server_total_idempotency_and_stock_hold(self):
        data=self.basket();data['total_minor']=1;data['items'][0]['price_minor']=1
        order=self.engine.checkout(data)
        self.assertEqual(order['total_minor'],80000)
        self.assertEqual(self.engine.checkout(data)['id'],order['id'])
        self.assertEqual(self.c.db.execute('SELECT quantity FROM inventory WHERE id=?',(self.item,)).fetchone()[0],8)
        data['items'][0]['quantity']=3
        with self.assertRaises(ValueError):self.engine.checkout(data)
    def test_simulated_payment_never_becomes_real_paid(self):
        data=self.basket();order=self.engine.checkout(data)
        result=self.engine.verify_order(order['id'],data['checkout_key'],simulate='pay')
        self.assertEqual(result['status'],'demo_paid')
        with self.assertRaises(PermissionError):self.engine.verify_order(order['id'],'wrong',simulate='pay')
    def test_cancel_releases_stock_once(self):
        data=self.basket();order=self.engine.checkout(data)
        for _ in range(2):self.engine.verify_order(order['id'],data['checkout_key'],simulate='cancel')
        self.assertEqual(self.c.db.execute('SELECT quantity FROM inventory WHERE id=?',(self.item,)).fetchone()[0],10)
    def test_wrong_store_item_or_stale_agreement_rejected(self):
        data=self.basket();data['terms_digest']='stale'
        with self.assertRaises(ValueError):self.engine.checkout(data)
        data=self.basket();data['items'][0]['id']='another-store-item'
        with self.assertRaises(ValueError):self.engine.checkout(data)
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
    def test_parallel_checkout_cannot_oversell(self):
        with self.c.db:self.c.db.execute('UPDATE inventory SET quantity=1 WHERE id=?',(self.item,))
        baskets=[self.basket(),self.basket()]
        for b in baskets:b['items'][0]['quantity']=1
        def checkout(data):
            c=Control(self.path,demo=True)
            try:
                try:return Commerce(c,demo=True).checkout(data)['id']
                except ValueError:return None
            finally:c.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(checkout,baskets))
        self.assertEqual(sum(x is not None for x in results),1)
        self.assertEqual(self.c.db.execute('SELECT quantity FROM inventory WHERE id=?',(self.item,)).fetchone()[0],0)
    def test_scoped_keys_hash_revoke_and_rotate(self):
        issued=self.c.mutate(self.root,'api_keys',dict(store_id=self.store,name='Inventory partner',scopes='inventory:read',expires_days=1))
        raw=issued['secret'];self.assertTrue(raw.startswith('gpos_'))
        self.engine.api_actor(raw,'inventory:read',self.store)
        with self.assertRaises(PermissionError):self.engine.api_actor(raw,'inventory:write',self.store)
        with self.assertRaises(PermissionError):self.engine.api_actor(raw,'inventory:read','other')
        snapshot=self.c.snapshot(self.root)
        self.assertNotIn(raw,json.dumps(snapshot));self.assertNotIn('digest',snapshot['api_keys'][0])
        rotated=self.c.mutate(self.root,'api_keys',dict(id=issued['id'],store_id=self.store,action='rotate'))
        with self.assertRaises(PermissionError):self.engine.api_actor(raw,'inventory:read',self.store)
        self.engine.api_actor(rotated['secret'],'inventory:read',self.store)
        self.c.mutate(self.root,'api_keys',dict(id=rotated['id'],store_id=self.store,action='revoke'))
        with self.assertRaises(PermissionError):self.engine.api_actor(rotated['secret'],'inventory:read',self.store)
    def test_production_cannot_use_simulator_and_demo_cannot_use_live(self):
        production=Control(self.path)
        try:
            with self.assertRaises(PermissionError):production.mutate(self.root,'gateways',dict(store_id=self.store,provider='demo',mode='test',name='Forbidden',enabled=True))
        finally:production.db.close()
        with self.assertRaises(PermissionError):self.c.mutate(self.root,'gateways',dict(store_id=self.store,provider='stripe',mode='live',name='Forbidden',enabled=True,secret_ref='GLOBALPOS_STRIPE_LIVE_ONE'))
    def test_demo_never_contacts_external_provider(self):
        real=self.c.mutate(self.root,'gateways',dict(store_id=self.store,provider='stripe',mode='test',name='Config demo',enabled=True,secret_ref='GLOBALPOS_STRIPE_TEST_ONE'))['id']
        data=self.basket();data['gateway_id']=real
        with patch('gateways.request') as send:
            with self.assertRaises(PermissionError):self.engine.checkout(data)
            send.assert_not_called()
    def test_global_payment_and_integration_kill_switches(self):
        self.c.mutate(self.root,'features',dict(scope='global',feature='payments',enabled=False))
        with self.assertRaises(PermissionError):self.engine.checkout(self.basket())
        self.assertEqual(self.engine.marketplace()['stores'][0]['gateways'],[])
        key=self.c.mutate(self.root,'api_keys',dict(store_id=self.store,name='Key',scopes='inventory:read'))
        self.c.mutate(self.root,'features',dict(scope='global',feature='integrations',enabled=False))
        with self.assertRaises(PermissionError):self.engine.api_actor(key['secret'],'inventory:read',self.store)
    def test_real_adapter_checkout_and_authoritative_verification(self):
        live=Control(self.path)
        engine=Commerce(live)
        try:
            config=dict(store_id=self.store,provider='stripe',mode='test',name='Stripe test',enabled=True,secret_ref='GLOBALPOS_STRIPE_TEST_ONE')
            identifier=live.mutate(self.root,'gateways',config)['id'];data=self.basket();data['gateway_id']=identifier
            with patch.dict(os.environ,{'GLOBALPOS_STRIPE_TEST_ONE':'sk_test_FAKE'}):
                with patch('gateways.request',return_value={'id':'cs_test_unit','url':'https://checkout.stripe.com/c/pay/unit'}):order=engine.checkout(data)
                row=dict(live.db.execute('SELECT * FROM orders WHERE id=?',(order['id'],)).fetchone())
                wrong=dict(payment_status='paid',client_reference_id=row['reference'],amount_total=1,currency='ngn',livemode=False)
                with patch('gateways.request',return_value=wrong):self.assertEqual(engine.verify_order(order['id'],data['checkout_key'])['status'],'pending')
                correct=dict(wrong,amount_total=80000)
                with patch('gateways.request',return_value=correct):self.assertEqual(engine.verify_order(order['id'],data['checkout_key'])['status'],'paid')
        finally:live.db.close()
    def test_expired_key_and_suspended_store_denied(self):
        key=self.c.mutate(self.root,'api_keys',dict(store_id=self.store,name='Key',scopes='orders:read'))
        with self.c.db:self.c.db.execute('UPDATE api_keys SET expires=0 WHERE id=?',(key['id'],))
        with self.assertRaises(PermissionError):self.engine.api_actor(key['secret'],'orders:read',self.store)

class GatewayTests(unittest.TestCase):
    def test_paystack_verifies_mode_reference_amount_and_currency(self):
        config=dict(provider='paystack',mode='test',secret_ref='GLOBALPOS_PAYSTACK_TEST_ONE')
        order=dict(reference='gpos_sample',currency='NGN',total_minor=40000)
        good={'status':True,'data':dict(status='success',reference='gpos_sample',currency='NGN',amount=40000,domain='test')}
        with patch.dict(os.environ,{'GLOBALPOS_PAYSTACK_TEST_ONE':'sk_test_FAKE'}):
            with patch('gateways.request',return_value=good):self.assertTrue(gateways.verify(config,order))
            good['data']['domain']='live'
            with patch('gateways.request',return_value=good):self.assertFalse(gateways.verify(config,order))
    def test_flutterwave_exact_decimal_amount_and_reference(self):
        config=dict(provider='flutterwave',mode='test',secret_ref='GLOBALPOS_FLUTTERWAVE_TEST_ONE')
        order=dict(reference='gpos_sample',currency='NGN',total_minor=40000)
        good=dict(status='success',data=dict(status='successful',tx_ref='gpos_sample',currency='NGN',amount='400.00'))
        with patch.dict(os.environ,{'GLOBALPOS_FLUTTERWAVE_TEST_ONE':'FLWSECK_TEST-FAKE'}):
            with patch('gateways.request',return_value=good):self.assertTrue(gateways.verify(config,order))
            good['data']['tx_ref']='someone_else'
            with patch('gateways.request',return_value=good):self.assertFalse(gateways.verify(config,order))
    def test_test_reference_cannot_resolve_to_live_secret(self):
        config=dict(provider='stripe',mode='test',secret_ref='GLOBALPOS_STRIPE_TEST_ONE')
        with patch.dict(os.environ,{'GLOBALPOS_STRIPE_TEST_ONE':'sk_live_FAKE'}):
            with self.assertRaises(gateways.GatewayError):gateways.secret(config)
