import tempfile,unittest,json,os,time,secrets
from pathlib import Path
from unittest.mock import patch
from control import Control
from storefronts import Merchant
import federation
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
class StorefrontTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.c=Control(str(Path(self.tmp.name)/'db'));self.m=Merchant(self.c)
  self.data=dict(email='owner@example.test',password='private-store-password',business_name='First merchant',accept_terms=True,create_storefront=True,country='US',currency='USD',timezone='UTC')
  self.c.signup(self.data);self.actor=self.c.actor(self.c.login(self.data['email'],self.data['password']));self.store=self.m.context(self.actor)['stores'][0]
 def tearDown(self):self.c.db.close();self.tmp.cleanup()
 def test_opt_in_isolation_publication(self):
  s=self.store;self.assertEqual(s['status'],'draft');self.assertEqual(self.c.discover(),[])
  with self.assertRaises(ValueError):self.m.public(s['storefront']['slug'])
  self.c.signup(dict(self.data,email='other@example.test'));other=self.c.actor(self.c.login('other@example.test',self.data['password']))
  self.assertEqual(len(self.m.context(other)['stores']),1)
  with self.assertRaises(PermissionError):self.m.dispatch(other,'inventory',dict(store_id=s['id'],name='Intruder',barcode='1',quantity=1,price_minor=1))
  data=dict(s,**s['storefront']);data.update(store_id=s['id'],status='active',address='Online business address',virtual_only=True)
  result=self.m.dispatch(self.actor,'save',data);self.assertEqual(self.c.discover(),[])
  with self.assertRaises(ValueError):self.c.public_catalog(s['id'])
  self.c.mutate(self.actor,'inventory',dict(store_id=s['id'],name='Product',barcode='1',quantity=3,price_minor=1200))
  body=dict(store_id=s['id'],revision=result['revision'],publish_confirmed=True,terms_digest=self.m.context(self.actor)['terms_digest'])
  with self.assertRaises(PermissionError):self.m.dispatch(self.actor,'publish',body)
  with self.c.db:self.c.db.execute("INSERT INTO features VALUES('global','marketplace',1)")
  with self.assertRaises(ValueError):self.m.dispatch(self.actor,'publish',dict(body,publish_confirmed=False))
  self.m.dispatch(self.actor,'publish',body);self.assertEqual(self.m.public(result['slug'])['store']['products'][0]['name'],'Product');self.assertEqual(self.c.discover(lat=0,lon=0),[])
  with self.assertRaises(ValueError):self.m.dispatch(self.actor,'unpublish',body)
  self.m.dispatch(self.actor,'unpublish',dict(body,revision=body['revision']+1))
  with self.assertRaises(ValueError):self.m.public(result['slug'])
 def test_invalid_registration_atomic_and_no_opt_in_no_store(self):
  with self.assertRaises(ValueError):self.c.signup(dict(self.data,email='invalid@example.test',timezone='No/such_zone'))
  self.assertEqual(self.c.db.execute('SELECT count(*) FROM tenants').fetchone()[0],1)
  self.c.signup(dict(self.data,email='without@example.test',create_storefront=False));self.assertEqual(self.c.db.execute('SELECT count(*) FROM storefronts').fetchone()[0],1)
class FederationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.c=Control(str(Path(self.tmp.name)/'db'));self.key=Ed25519PrivateKey.generate();p=Path(self.tmp.name)/'pub';p.write_bytes(self.key.public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo));self.env=patch.dict(os.environ,GLOBALPOS_SUITE_PUBLIC_KEY_FILE=str(p),GLOBALPOS_PUBLIC_ORIGIN='https://pos.africandroid.com');self.env.start();self.nonce=secrets.token_hex(32)
 def tearDown(self):self.env.stop();self.c.db.close();self.tmp.cleanup()
 def body(self,**changes):
  now=int(time.time());claims=dict(iss='https://olamideolatayobello.com',aud='https://pos.africandroid.com',scope='store-pos',sub='4',tenant='2',role='admin',company='Business',email='same@example.test',iat=now,exp=now+60,jti=secrets.token_hex(32),nonce=self.nonce,target='merchant',storefront=dict(country='US',currency='USD',timezone='UTC'));claims.update(changes)
  raw=federation.b64(json.dumps(dict(alg='EdDSA',typ='JWT')).encode())+'.'+federation.b64(json.dumps(claims).encode());return dict(assertion=raw+'.'+federation.b64(self.key.sign(raw.encode())),nonce=self.nonce)
 def test_replay_scopes_and_idempotent_provision(self):
  body=self.body(scope='storefront-provision');result=federation.exchange(self.c,body,True)
  with self.assertRaises(PermissionError):federation.exchange(self.c,body,True)
  second=federation.exchange(self.c,self.body(scope='storefront-provision'),True);self.assertEqual(result['storefront']['store_id'],second['storefront']['store_id'])
  login=federation.exchange(self.c,self.body());actor=self.c.actor(login['token']);self.assertFalse(actor['super']);self.assertEqual(len(Merchant(self.c).context(actor)['stores']),1)
  login2=federation.exchange(self.c,self.body(sub='5',tenant='3'));self.assertEqual(len(Merchant(self.c).context(self.c.actor(login2['token']))['stores']),1);self.assertEqual(self.c.db.execute('SELECT count(*) FROM tenants').fetchone()[0],2)
 def test_invalid_handoffs_do_not_create_tenants(self):
  for changes in ({'aud':'https://evil.example'},{'exp':int(time.time())-1},{'scope':'storefront-provision'},{'nonce':'0'*64},{'role':'platform_owner'}):
   with self.assertRaises(PermissionError):federation.exchange(self.c,self.body(**changes))
  self.assertEqual(self.c.db.execute('SELECT count(*) FROM tenants').fetchone()[0],0)
 def test_expired_grant_is_denied(self):
  login=federation.exchange(self.c,self.body());actor=self.c.actor(login['token']);tid=self.c.db.execute('SELECT id FROM tenants').fetchone()[0]
  with self.c.db:self.c.db.execute('UPDATE suite_grants SET expires=0')
  with self.assertRaises(PermissionError):self.c.authorize(actor,tid,write=True)
