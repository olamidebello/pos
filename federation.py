"""Pinned first-party Ed25519 assertions; one-time, nonce-bound, short-lived merchant access."""
import base64,hashlib,json,os,secrets,time
from pathlib import Path

ROLES={'admin':'tenant_admin','super_admin':'tenant_admin','editor':'cashier','viewer':'auditor'}
def b64(value):return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
def unb64(value):
 try:return base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)
 except Exception:raise PermissionError('Invalid signed assertion') from None

def initialize(db):
 db.executescript('''
 CREATE TABLE IF NOT EXISTS suite_workspaces(issuer TEXT NOT NULL,external_id TEXT NOT NULL,tenant_id TEXT UNIQUE NOT NULL REFERENCES tenants(id),PRIMARY KEY(issuer,external_id));
 CREATE TABLE IF NOT EXISTS suite_users(issuer TEXT NOT NULL,subject TEXT NOT NULL,user_id TEXT UNIQUE NOT NULL REFERENCES users(id),email_label TEXT NOT NULL,PRIMARY KEY(issuer,subject));
 CREATE TABLE IF NOT EXISTS suite_grants(user_id TEXT NOT NULL,tenant_id TEXT NOT NULL,role TEXT NOT NULL,expires REAL NOT NULL,PRIMARY KEY(user_id,tenant_id));
 CREATE TABLE IF NOT EXISTS suite_assertions(id TEXT PRIMARY KEY,expires REAL NOT NULL);
 ''')

def configuration():
 issuer=os.environ.get('GLOBALPOS_SUITE_ISSUER','https://olamideolatayobello.com').rstrip('/')
 audience=os.environ.get('GLOBALPOS_PUBLIC_ORIGIN','https://pos.africandroid.com').rstrip('/')
 path=Path(os.environ.get('GLOBALPOS_SUITE_PUBLIC_KEY_FILE','/etc/pos-control/suite-pos-public.key'))
 return issuer,audience,path

def public_config():
 issuer,audience,path=configuration();return {'enabled':path.is_file(),'issuer':issuer,'audience':audience,'session_minutes':15,'notice':'Company identity and role come from the configured Business Suite. No password or payment credential is transferred.'}

def verify(assertion,nonce,scope='store-pos'):
 if not isinstance(assertion,str) or len(assertion)>12000 or not isinstance(nonce,str) or len(nonce)!=64:raise PermissionError('Invalid identity handoff')
 issuer,audience,path=configuration()
 if not path.is_file():raise PermissionError('Business Suite connection is not configured; ask the platform administrator')
 try:
  from cryptography.hazmat.primitives.serialization import load_pem_public_key
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
  head,payload,signature=assertion.split('.');header=json.loads(unb64(head));claims=json.loads(unb64(payload))
  if header!={'alg':'EdDSA','typ':'JWT'}:raise ValueError()
  public=load_pem_public_key(path.read_bytes())
  if not isinstance(public,Ed25519PublicKey):raise ValueError()
  public.verify(unb64(signature),(head+'.'+payload).encode())
  now=time.time()
  if claims.get('iss')!=issuer or claims.get('aud')!=audience or claims.get('nonce')!=nonce or claims.get('scope')!=scope:raise ValueError()
  if type(claims.get('iat')) is not int or type(claims.get('exp')) is not int or not now-90<=claims['iat']<=now+10 or not now<claims['exp']<=claims['iat']+60:raise ValueError()
  if claims.get('role') not in ROLES or claims.get('target') not in ('merchant','pos','restaurant'):raise ValueError()
  for field in ('sub','tenant','jti','company','email'):
   if not isinstance(claims.get(field),str) or not 1<=len(claims[field])<=254:raise ValueError()
 except Exception:raise PermissionError('Identity handoff invalid, expired or not intended for this POS') from None
 return claims

def exchange(control,body,provision=False):
 if control.demo:raise PermissionError('Production identity handoffs cannot enter an isolated demo')
 claims=verify(body.get('assertion'),body.get('nonce'),'storefront-provision' if provision else 'store-pos');db=control.db;now=time.time();role=ROLES[claims['role']]
 with db:
  db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM suite_assertions WHERE expires<?',(now-300,))
  if db.execute('SELECT 1 FROM suite_assertions WHERE id=?',(claims['jti'],)).fetchone():raise PermissionError('This identity handoff has already been used; reopen Store & POS')
  workspace=db.execute('SELECT tenant_id FROM suite_workspaces WHERE issuer=? AND external_id=?',(claims['iss'],claims['tenant'])).fetchone()
  if not workspace:
   if role!='tenant_admin':raise PermissionError('A business administrator must connect the company before staff can use POS')
   tid=secrets.token_hex(12);db.execute('INSERT INTO tenants VALUES(?,?,?)',(tid,claims['company'],'active'));db.execute('INSERT INTO suite_workspaces VALUES(?,?,?)',(claims['iss'],claims['tenant'],tid))
  else:
   tid=workspace['tenant_id'];tenant=db.execute('SELECT status FROM tenants WHERE id=?',(tid,)).fetchone()
   if not tenant or tenant['status']!='active':raise PermissionError('The POS merchant workspace is suspended')
  if claims.get('storefront') and not db.execute('SELECT 1 FROM storefronts f JOIN stores s ON s.id=f.store_id WHERE s.tenant_id=?',(tid,)).fetchone():
   if role!='tenant_admin':raise PermissionError('Only a business administrator can provision the opted-in storefront')
   import storefronts
   storefronts.create_draft(db,tid,claims['company'],claims['storefront'])
  user=db.execute('SELECT user_id FROM suite_users WHERE issuer=? AND subject=?',(claims['iss'],claims['sub'])).fetchone()
  if not user:
   uid=secrets.token_hex(12);email='linked-'+secrets.token_hex(16)+'@suite.invalid';db.execute('INSERT INTO users VALUES(?,?,?,0,1)',(uid,email,control.password(secrets.token_urlsafe(48))));db.execute('INSERT INTO suite_users VALUES(?,?,?,?)',(claims['iss'],claims['sub'],uid,claims['email']))
  else:
   uid=user['user_id'];active=db.execute('SELECT active,super FROM users WHERE id=?',(uid,)).fetchone()
   if not active or not active['active'] or active['super']:raise PermissionError('Linked account unavailable; contact the POS administrator')
   db.execute('UPDATE suite_users SET email_label=? WHERE user_id=?',(claims['email'],uid))
  # Only this signed workspace's managed memberships are refreshed. No email-based account merging.
  db.execute('DELETE FROM memberships WHERE user_id=? AND tenant_id=?',(uid,tid))
  if role=='tenant_admin':db.execute('INSERT INTO memberships VALUES(?,?,?,?)',(uid,tid,'',role))
  else:
   for store in db.execute("SELECT id FROM stores WHERE tenant_id=? AND status='active'",(tid,)).fetchall():db.execute('INSERT INTO memberships VALUES(?,?,?,?)',(uid,tid,store['id'],role))
  expires=now+900;db.execute('INSERT OR REPLACE INTO suite_grants VALUES(?,?,?,?)',(uid,tid,role,expires));db.execute('INSERT INTO suite_assertions VALUES(?,?)',(claims['jti'],claims['exp']))
  if provision:
   front=db.execute('SELECT f.store_id,f.slug,f.state FROM storefronts f JOIN stores s ON s.id=f.store_id WHERE s.tenant_id=? ORDER BY f.updated LIMIT 1',(tid,)).fetchone()
   if not front:raise ValueError('No opted-in storefront was provisioned')
   control.log({'id':uid},'suite.storefront_provisioned',tid,{'store_id':front['store_id']})
   return {'storefront':dict(front),'tenant_id':tid,'state':'ready'}
  token=secrets.token_urlsafe(32);db.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),uid,expires));control.log({'id':uid},'suite.pos_login',tid,{'source':claims['iss'],'role':role,'expires':expires})
 return {'token':token,'page':claims['target']+'.html','expires':expires,'company':claims['company']}

def require_grant(control,actor,tenant):
 if not control.db.execute('SELECT 1 FROM suite_users WHERE user_id=?',(actor['id'],)).fetchone():return
 row=control.db.execute('SELECT expires FROM suite_grants WHERE user_id=? AND tenant_id=?',(actor['id'],tenant)).fetchone()
 if not row or row['expires']<=time.time():raise PermissionError('Connected access expired; reopen Store & POS from your Business Suite')
