"""Global POS control plane and marketplace. Run behind trusted HTTPS proxy."""
import argparse
import getpass
import hashlib
import ipaddress
import hmac
import json
import math
import os
import secrets
import re
import threading
import sqlite3
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo
import pos_engine
import restaurant
import federation
import storefronts
from discovery import discover
from product_search import search as search_products
from commerce import Commerce, initialize as initialize_commerce, RESOURCES as COMMERCE_RESOURCES

DEMO_EMAIL = 'demo@globalpos.example'
DEMO_PASSWORD = 'GlobalPOS-Demo!2026'
DEMO_LOCK = threading.Lock()

FEATURES = ('pos', 'restaurant', 'scan_go', 'autopay', 'inventory', 'returns', 'loyalty', 'promotions', 'offline', 'integrations', 'marketplace', 'payments')
ROLES = ('tenant_admin', 'store_manager', 'cashier', 'exit_verifier', 'auditor', 'inventory_manager', 'accountant', 'waiter', 'host', 'chef', 'kitchen_staff', 'delivery_dispatcher', 'delivery_driver')

class Control:
    def __init__(self, path, *, demo=False):
        self.demo=demo
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,super INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),name TEXT NOT NULL,address TEXT NOT NULL,country TEXT NOT NULL,currency TEXT NOT NULL,timezone TEXT NOT NULL,language TEXT NOT NULL,lat REAL NOT NULL,lon REAL NOT NULL,status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS inventory(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),barcode TEXT NOT NULL,name TEXT NOT NULL,quantity INTEGER NOT NULL,price_minor INTEGER NOT NULL,UNIQUE(store_id,barcode));
        CREATE TABLE IF NOT EXISTS memberships(user_id TEXT NOT NULL REFERENCES users(id),tenant_id TEXT NOT NULL REFERENCES tenants(id),store_id TEXT NOT NULL DEFAULT '',role TEXT NOT NULL,PRIMARY KEY(user_id,tenant_id,store_id));
        CREATE TABLE IF NOT EXISTS features(scope TEXT NOT NULL,feature TEXT NOT NULL,enabled INTEGER NOT NULL,PRIMARY KEY(scope,feature));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,detail TEXT NOT NULL,created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TABLE IF NOT EXISTS request_limits(subject TEXT PRIMARY KEY,slot INTEGER NOT NULL,count INTEGER NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_limits(email TEXT PRIMARY KEY,failures INTEGER NOT NULL,until REAL NOT NULL);
        ''')
        initialize_commerce(self.db)
        pos_engine.initialize(self.db)
        restaurant.initialize(self.db)
        federation.initialize(self.db)
        storefronts.initialize(self.db)

    def rate_limit(self, subject, maximum, window):
        now=time.time();slot=int(now//window);digest=hashlib.sha256(subject.encode()).hexdigest()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM request_limits WHERE expires<=?',(now,))
            row=self.db.execute('SELECT slot,count FROM request_limits WHERE subject=?',(digest,)).fetchone()
            count=row['count'] if row and row['slot']==slot else 0
            if count>=maximum:raise PermissionError('Request rate exceeded; retry later')
            self.db.execute('INSERT OR REPLACE INTO request_limits VALUES(?,?,?,?)',(digest,slot,count+1,(slot+1)*window))

    @staticmethod
    def password(value, salt=None):
        if not isinstance(value, str) or len(value) < 12 or len(value) > 1024:
            raise ValueError('Password must contain 12–1024 characters')
        salt = salt or secrets.token_hex(16)
        return salt + ':' + hashlib.scrypt(value.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()

    def log(self, actor, action, target, detail):
        self.db.execute('INSERT INTO audit(actor,action,target,detail) VALUES(?,?,?,?)', (actor['id'], action, target, json.dumps(detail)))

    def bootstrap(self, email, password):
        with self.db:
            if self.db.execute('SELECT 1 FROM users WHERE super=1').fetchone():
                raise ValueError('Super admin already exists; use authenticated user management')
            self.db.execute('INSERT INTO users VALUES(?,?,?,1,1)', (secrets.token_hex(12), email.lower().strip(), self.password(password)))

    def signup(self, data):
        email=self.required(data,'email').lower()
        if len(email)>254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email):
            raise ValueError('Enter a valid email address')
        if data.get('accept_terms') is not True:
            raise ValueError('Accept the terms and privacy policy to continue')
        password=self.password(data.get('password'))
        business=self.required(data,'business_name')
        preferences=storefronts.validate_preferences(data) if data.get('create_storefront') is True else None
        user,tenant=secrets.token_hex(12),secrets.token_hex(12)
        with self.db:
            if self.db.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone():
                raise ValueError('Unable to create account with this email; sign in or contact your administrator')
            self.db.execute('INSERT INTO users VALUES(?,?,?,0,1)',(user,email,password))
            self.db.execute('INSERT INTO tenants VALUES(?,?,?)',(tenant,business,'active'))
            self.db.execute('INSERT INTO memberships VALUES(?,?,?,?)',(user,tenant,'','tenant_admin'))
            if preferences:storefronts.create_draft(self.db,tenant,business,preferences)
            self.log({'id':user},'account.signup',tenant,{'terms':'terms-v2','privacy':'privacy-v2'})
        return {'ok':True,'message':'Account created. Sign in to Store & POS to manage your business.'}

    def login(self, email, password):
        email = email.lower().strip()
        limit = self.db.execute('SELECT * FROM login_limits WHERE email=?', (email,)).fetchone()
        if limit and limit['until'] > time.time():
            raise PermissionError('Login temporarily locked; retry later')
        row = self.db.execute('SELECT * FROM users WHERE email=? AND active=1', (email,)).fetchone()
        stored = row['password'] if row else '0'*32 + ':' + '0'*128
        try:
            valid = hmac.compare_digest(stored, self.password(password, stored.split(':')[0]))
        except ValueError:
            valid = False
        if not row or not valid:
            with self.db:
                failures = (limit['failures'] if limit else 0) + 1
                self.db.execute('INSERT OR REPLACE INTO login_limits VALUES(?,?,?)', (email, failures, time.time()+300 if failures >= 5 else 0))
            raise PermissionError('Invalid credentials')
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('DELETE FROM login_limits WHERE email=?', (email,))
            self.db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            self.db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), row['id'], time.time()+3600))
        return token

    def actor(self, token):
        row = self.db.execute('SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row:
            raise PermissionError('Authentication required')
        actor=dict(row)
        linked=self.db.execute('SELECT email_label FROM suite_users WHERE user_id=?',(actor['id'],)).fetchone()
        if linked:actor['email']=linked['email_label']
        return actor

    def authorize(self, actor, tenant, store='', write=False):
        federation.require_grant(self,actor,tenant)
        if actor['super']:
            return
        tenant_row=self.db.execute('SELECT status FROM tenants WHERE id=?',(tenant,)).fetchone()
        if not tenant_row or tenant_row['status']!='active': raise PermissionError('Tenant suspended or unavailable')
        draft=False
        if store:
            store_row=self.db.execute('SELECT status FROM stores WHERE id=? AND tenant_id=?',(store,tenant)).fetchone()
            if not store_row or store_row['status'] not in ('active','draft'): raise PermissionError('Store suspended or unavailable')
            draft=store_row['status']=='draft'
        if actor.get('api_store'):
            if actor['api_store']==store and ('inventory:write' if write else 'inventory:read') in actor['api_scopes']:return
            raise PermissionError('API key store or scope denied')
        rows = self.db.execute('SELECT * FROM memberships WHERE user_id=? AND tenant_id=?', (actor['id'], tenant)).fetchall()
        for row in rows:
            if row['role'] == 'tenant_admin' or (store and not draft and row['store_id'] == store and (not write or row['role'] == 'store_manager')):
                return
        raise PermissionError('Tenant or store access denied')

    @staticmethod
    def required(data, key):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError('Invalid '+key)
        return value.strip()

    def mutate(self, actor, resource, data):
        if resource in COMMERCE_RESOURCES:return Commerce(self,demo=self.demo).mutate(actor,resource,data)
        with self.db:
            if resource == 'tenants':
                if not actor['super']: raise PermissionError('Super admin required')
                target = data.get('id') or secrets.token_hex(12)
                status = data.get('status', 'active')
                if status not in ('active', 'suspended'): raise ValueError('Invalid status')
                self.db.execute('INSERT INTO tenants VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,status=excluded.status', (target, self.required(data,'name'), status))
            elif resource == 'stores':
                tenant = self.required(data,'tenant_id')
                target = data.get('id') or secrets.token_hex(12)
                existing = self.db.execute('SELECT tenant_id,currency FROM stores WHERE id=?', (target,)).fetchone()
                if existing and existing['tenant_id'] != tenant: raise PermissionError('Cannot move store between tenants')
                self.authorize(actor, tenant, target if existing else '', write=True)
                country, currency = self.required(data,'country').upper(), self.required(data,'currency').upper()
                if len(country)!=2 or not country.isascii() or not country.isalpha() or len(currency)!=3 or not currency.isascii() or not currency.isalpha(): raise ValueError('Use country and currency codes')
                if existing and existing['currency']!=currency and self.db.execute('SELECT 1 FROM inventory WHERE store_id=?',(target,)).fetchone():raise ValueError('Cannot change currency while inventory exists; reprice through a controlled migration')
                zone = self.required(data,'timezone')
                try: ZoneInfo(zone)
                except (KeyError, ValueError): raise ValueError('Invalid timezone')
                lat, lon = float(data['lat']), float(data['lon'])
                if not (-90<=lat<=90 and -180<=lon<=180): raise ValueError('Invalid coordinates')
                status=data.get('status','active')
                if status not in ('active','suspended','draft'): raise ValueError('Invalid status')
                values=(target,tenant,self.required(data,'name'),self.required(data,'address'),country,currency,zone,self.required(data,'language'),lat,lon,status)
                self.db.execute('INSERT INTO stores VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,address=excluded.address,country=excluded.country,currency=excluded.currency,timezone=excluded.timezone,language=excluded.language,lat=excluded.lat,lon=excluded.lon,status=excluded.status',values)
            elif resource == 'inventory':
                self.db.execute('BEGIN IMMEDIATE')
                store=self.required(data,'store_id')
                row=self.db.execute('SELECT tenant_id FROM stores WHERE id=?',(store,)).fetchone()
                if not row: raise ValueError('Unknown store')
                self.authorize(actor,row['tenant_id'],store,write=True)
                target=data.get('id') or secrets.token_hex(12)
                existing=self.db.execute('SELECT store_id,quantity FROM inventory WHERE id=?',(target,)).fetchone()
                if existing and existing['store_id']!=store: raise PermissionError('Cannot move inventory between stores')
                if existing and 'expected_quantity' in data and data['expected_quantity']!=existing['quantity']:raise ValueError('Stock changed; reload inventory before applying an absolute count')
                quantity=data.get('quantity'); price=data.get('price_minor')
                if type(quantity) is not int or quantity<0 or type(price) is not int or not 0<=price<=10000000000 or quantity>1000000000: raise ValueError('Nonnegative integer quantity and price_minor required')
                self.db.execute('INSERT INTO inventory VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET barcode=excluded.barcode,name=excluded.name,quantity=excluded.quantity,price_minor=excluded.price_minor',(target,store,self.required(data,'barcode'),self.required(data,'name'),quantity,price))
                pos_engine.POS(self).movement(actor,store,target,quantity-(existing['quantity'] if existing else 0),'admin_inventory_edit',target)
            elif resource == 'users':
                if not actor['super']: raise PermissionError('Super admin required')
                target=data.get('id') or secrets.token_hex(12)
                existing=self.db.execute('SELECT * FROM users WHERE id=?',(target,)).fetchone()
                superuser=data.get('super',False); active=data.get('active',True)
                if type(superuser) is not bool or type(active) is not bool: raise ValueError('Boolean flags required')
                if superuser and self.db.execute('SELECT 1 FROM suite_users WHERE user_id=?',(target,)).fetchone():raise PermissionError('Connected identities cannot become POS platform super administrators')
                if existing and existing['super'] and existing['active'] and not (superuser and active):
                    if self.db.execute('SELECT COUNT(*) FROM users WHERE super=1 AND active=1').fetchone()[0]<=1: raise ValueError('Cannot remove last active super admin')
                password=self.password(data['password']) if data.get('password') else existing['password'] if existing else None
                if not password: raise ValueError('Password required for new user')
                self.db.execute('INSERT INTO users VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email,password=excluded.password,super=excluded.super,active=excluded.active',(target,self.required(data,'email').lower(),password,superuser,active))
                self.db.execute('DELETE FROM sessions WHERE user_id=?',(target,))
            elif resource == 'memberships':
                tenant=self.required(data,'tenant_id'); self.authorize(actor,tenant,write=True)
                target=self.required(data,'user_id'); role=self.required(data,'role'); store=data.get('store_id','')
                if role not in ROLES: raise ValueError('Invalid role')
                if role=='tenant_admin' and store: raise ValueError('Tenant admin must be tenant scoped')
                if role!='tenant_admin' and not store: raise ValueError('Store role requires store')
                if store and not self.db.execute('SELECT 1 FROM stores WHERE id=? AND tenant_id=?',(store,tenant)).fetchone(): raise ValueError('Store belongs to another tenant')
                if data.get('remove') is True:
                    self.db.execute('DELETE FROM memberships WHERE user_id=? AND tenant_id=? AND store_id=?',(target,tenant,store))
                else:
                    self.db.execute('INSERT OR REPLACE INTO memberships VALUES(?,?,?,?)',(target,tenant,store,role))
                self.db.execute('DELETE FROM sessions WHERE user_id=?',(target,))
            elif resource == 'features':
                scope=self.required(data,'scope'); feature=self.required(data,'feature')
                if scope!='global':
                    kind, sep, identifier=scope.partition(':')
                    if kind=='tenant': self.authorize(actor,identifier,write=True)
                    elif kind=='store':
                        row=self.db.execute('SELECT tenant_id FROM stores WHERE id=?',(identifier,)).fetchone()
                        if not row: raise ValueError('Unknown store')
                        self.authorize(actor,row['tenant_id'],identifier,write=True)
                    else: raise ValueError('Invalid scope')
                elif not actor['super']: raise PermissionError('Super admin required')
                if feature not in FEATURES or type(data.get('enabled')) is not bool: raise ValueError('Invalid feature or enabled flag')
                target=scope
                self.db.execute('INSERT OR REPLACE INTO features VALUES(?,?,?)',(scope,feature,data['enabled']))
            else: raise ValueError('Unknown resource')
            self.log(actor,resource+'.save',target,{k:v for k,v in data.items() if k!='password'})
        return {'id':target}

    def snapshot(self, actor):
        tenants=[dict(x) for x in self.db.execute('SELECT * FROM tenants ORDER BY name')]
        if not actor['super']:
            ids={x[0] for x in self.db.execute('SELECT tenant_id FROM memberships WHERE user_id=?',(actor['id'],))}
            tenants=[t for t in tenants if t['id'] in ids]
        stores=[]
        for row in self.db.execute('SELECT * FROM stores ORDER BY name'):
            try: self.authorize(actor,row['tenant_id'],row['id'])
            except PermissionError: continue
            store=dict(row)
            store['effective_features']=self.effective_features(store)
            stores.append(store)
        result={'me':{k:actor[k] for k in ('id','email','super')},'tenants':tenants,'stores':stores,'feature_names':FEATURES,'roles':ROLES}
        if actor['super']:
            result.update(users=[dict(x) for x in self.db.execute('SELECT id,email,super,active FROM users')],memberships=[dict(x) for x in self.db.execute('SELECT * FROM memberships')],features=[dict(x) for x in self.db.execute('SELECT * FROM features')],audit=[dict(x) for x in self.db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200')])
        result['inventory']=[]
        for store in stores:
            result['inventory'].extend(self.catalog(actor,store['id']))
        result.update(Commerce(self,demo=self.demo).snapshot(actor,stores))
        return result

    def effective_features(self, store):
        result={}
        for feature in FEATURES:
            enabled=False
            for scope in ('global','tenant:'+store['tenant_id'],'store:'+store['id']):
                row=self.db.execute('SELECT enabled FROM features WHERE scope=? AND feature=?',(scope,feature)).fetchone()
                if scope=='global': enabled=bool(row and row[0])
                elif row: enabled=enabled and bool(row[0])
            result[feature]=enabled
        return result

    def catalog(self, actor, store):
        row=self.db.execute('SELECT tenant_id FROM stores WHERE id=?',(store,)).fetchone()
        if not row: raise ValueError('Unknown store')
        self.authorize(actor,row['tenant_id'],store)
        return [dict(x) for x in self.db.execute('SELECT i.*,s.tenant_id,s.lat,s.lon,s.address,s.country,s.currency FROM inventory i JOIN stores s ON i.store_id=s.id WHERE s.id=?',(store,))]

    def public_catalog(self, store):
        row=self.db.execute("SELECT s.id FROM stores s JOIN tenants t ON s.tenant_id=t.id WHERE s.id=? AND s.status='active' AND t.status='active' AND NOT EXISTS(SELECT 1 FROM storefronts f WHERE f.store_id=s.id AND f.state!='published')",(store,)).fetchone()
        if not row: raise ValueError('Store unavailable')
        return [dict(x) for x in self.db.execute('SELECT i.id,i.store_id,i.barcode,i.name,i.quantity,i.price_minor,s.address,s.lat,s.lon,s.currency FROM inventory i JOIN stores s ON i.store_id=s.id WHERE s.id=? ORDER BY i.name',(store,))]

    def discover(self, lat=None, lon=None):
        stores=[dict(x) for x in self.db.execute("SELECT s.* FROM stores s JOIN tenants t ON t.id=s.tenant_id WHERE s.status='active' AND t.status='active' AND NOT EXISTS(SELECT 1 FROM storefronts f WHERE f.store_id=s.id AND f.state!='published')")]
        if lat is not None:
            lat,lon=float(lat),float(lon)
            if not (-90<=lat<=90 and -180<=lon<=180): raise ValueError('Invalid coordinates')
            stores=[s for s in stores if not (lambda r: r and json.loads(r[0]).get('virtual_only'))(self.db.execute('SELECT payload FROM storefronts WHERE store_id=?',(s['id'],)).fetchone())]
            for s in stores:
                a,b=math.radians(lat),math.radians(s['lat'])
                h=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(s['lon']-lon)/2)**2
                s['distance_km']=round(6371*2*math.asin(math.sqrt(min(1,h))),2)
            stores.sort(key=lambda s:s['distance_km'])
        return stores

class DemoManager:
    """Public credentials grant access only to a separate per-visitor database."""
    def __init__(self, production_path):
        self.directory=Path(production_path).parent / 'demo-sessions'
        self.directory.mkdir(mode=0o700, exist_ok=True)

    def open(self, identifier):
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise PermissionError('Invalid demo session')
        path=self.directory / (identifier+'.db')
        if not path.is_file(): raise PermissionError('Demo expired; start a new demo')
        control=Control(path,demo=True)
        expiry=control.db.execute('SELECT expires FROM demo_metadata').fetchone()[0]
        if expiry<=time.time():
            control.db.close()
            raise PermissionError('Demo expired; start a new demo')
        return control

    def create(self, email, password, role='super_admin'):
        if role not in ('super_admin',)+ROLES:raise ValueError('Unknown demo role')
        if email!=DEMO_EMAIL or password!=DEMO_PASSWORD:
            raise PermissionError('Use the public demo credentials shown on the demo page')
        with DEMO_LOCK:
            for path in self.directory.glob('*.db'):
                db=sqlite3.connect(path)
                try: expired=db.execute('SELECT expires FROM demo_metadata').fetchone()[0]<=time.time()
                finally: db.close()
                if expired: path.unlink()
            if len(list(self.directory.glob('*.db')))>=30:
                raise ValueError('Demo capacity reached; retry later')
            identifier=secrets.token_hex(16)
            control=Control(self.directory / (identifier+'.db'),demo=True)
            try:
                expires=time.time()+3600
                with control.db:
                    control.db.execute('CREATE TABLE demo_metadata(expires REAL NOT NULL)')
                    control.db.execute('INSERT INTO demo_metadata VALUES(?)',(expires,))
                control.bootstrap(DEMO_EMAIL,DEMO_PASSWORD)
                token=control.login(DEMO_EMAIL,DEMO_PASSWORD)
                actor=control.actor(token)
                for feature in ('pos','restaurant','scan_go','returns','marketplace','payments','integrations'):
                    control.mutate(actor,'features',dict(scope='global',feature=feature,enabled=True))
                for name,country,currency,zone,lat,lon,address in (
                    ('Lagos Market Demo','NG','NGN','Africa/Lagos',6.5244,3.3792,'10 Example Market Road, Lagos'),
                    ('Texas Retail Demo','US','USD','America/Chicago',33.1972,-96.6398,'100 Example Street, McKinney, TX')):
                    tenant=control.mutate(actor,'tenants',{'name':name})['id']
                    store=control.mutate(actor,'stores',dict(tenant_id=tenant,name=name+' Store',address=address,country=country,currency=currency,timezone=zone,language='en',lat=lat,lon=lon))['id']
                    for barcode,product,quantity,price in [('DEMO001','Sample Water',24,40000 if currency=='NGN' else 199),('DEMO002','Sample Rice',12,250000 if currency=='NGN' else 899)]:
                        control.mutate(actor,'inventory',dict(store_id=store,barcode=barcode,name=product,quantity=quantity,price_minor=price))
                    control.mutate(actor,'marketplace',dict(store_id=store,enabled=True,category='Groceries',description='Fictional demo store for Global POS marketplace'))
                    control.mutate(actor,'gateways',dict(store_id=store,name='Demo payment simulator',provider='demo',mode='test',enabled=True))
                from demo_operations import seed
                seed(control,actor)
                if role!='super_admin':
                    store=control.db.execute('SELECT * FROM stores ORDER BY name LIMIT 1').fetchone()
                    uid=control.mutate(actor,'users',{'email':'demo-'+role+'@globalpos.example','password':DEMO_PASSWORD})['id']
                    control.mutate(actor,'memberships',{'user_id':uid,'tenant_id':store['tenant_id'],'store_id':'' if role=='tenant_admin' else store['id'],'role':role})
                    token=control.login('demo-'+role+'@globalpos.example',DEMO_PASSWORD)
                return {'demo_id':identifier,'token':token,'expires_at':expires,'demo':True,'role':role}
            finally: control.db.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # Never log credentials or bearer tokens.
    def handle_request(self):
        control=None
        try:
            from urllib.parse import urlsplit, parse_qs
            path=urlsplit(self.path).path; query=parse_qs(urlsplit(self.path).query)
            if self.command=='POST' and path=='/api/demo/login':
                data=self.body()
                result=DemoManager(self.server.db_path).create(data.get('email',''),data.get('password',''),data.get('role','super_admin'))
                self.send_json(200,result)
                return
            if path.startswith('/api/demo/'):
                pieces=path.split('/')
                if len(pieces)<5: raise PermissionError('Invalid demo route')
                control=DemoManager(self.server.db_path).open(pieces[3])
                path='/api/'+'/'.join(pieces[4:])
                if self.command=='POST' and path.startswith('/api/admin/'):
                    if control.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]>=500:
                        raise ValueError('Demo edit limit reached; start a fresh session')
            else:
                control=Control(self.server.db_path)
            engine=Commerce(control,demo=control.demo)
            peer=self.client_address[0]
            origin=str(ipaddress.ip_address(self.headers.get('X-Real-IP',peer) if peer=='127.0.0.1' else peer))
            if path.startswith('/api/integration/'):
                if self.command=='GET':
                    store=query.get('store_id',[''])[0]
                    scope='orders:read' if path=='/api/integration/orders' else 'inventory:read'
                    actor=engine.api_actor(self.headers.get('X-API-Key',''),scope,store)
                    if path=='/api/integration/inventory':result=control.catalog(actor,store)
                    elif path=='/api/integration/orders':result=[dict(x) for x in control.db.execute('SELECT id,store_id,total_minor,currency,status,created FROM orders WHERE store_id=?',(store,))]
                    else:self.send_json(404,{'error':'Not found'});return
                elif self.command=='POST' and path=='/api/integration/inventory':
                    data=self.body();store=data.get('store_id','')
                    actor=engine.api_actor(self.headers.get('X-API-Key',''),'inventory:write',store)
                    result=control.mutate(actor,'inventory',data)
                else:self.send_json(404,{'error':'Not found'});return
                self.send_json(200,result);return
            if self.command=='GET' and path=='/api/federation/config':result=federation.public_config()
            elif self.command=='POST' and path=='/api/federation/provision':
                control.rate_limit('federation-provision:'+origin,60,60);result=federation.exchange(control,self.body(),provision=True)
            elif self.command=='POST' and path=='/api/federation/exchange':
                control.rate_limit('federation:'+origin,60,60);result=federation.exchange(control,self.body())
            elif self.command=='GET' and path=='/api/storefront':result=storefronts.Merchant(control).public(query.get('slug',[''])[0])
            elif self.command=='GET' and path=='/api/marketplace':
                result=engine.marketplace()
            elif self.command=='POST' and path=='/api/checkout':
                control.rate_limit('checkout:'+origin,10,600)
                result=engine.checkout(self.body())
            elif self.command=='POST' and path=='/api/order':
                data=self.body();result=engine.order(data.get('id',''),data.get('checkout_key',''))
            elif self.command=='POST' and path=='/api/order/verify':
                control.rate_limit('verify:'+origin,30,60)
                data=self.body();result=engine.verify_order(data.get('id',''),key=data.get('checkout_key',''),simulate=data.get('simulate'))
            elif self.command=='GET' and path=='/api/stores':
                result=control.discover(query.get('lat',[None])[0],query.get('lon',[None])[0])
            elif self.command=='GET' and path=='/api/catalog':
                result=control.public_catalog(query.get('store_id',[''])[0])
            elif self.command=='POST' and path=='/api/product-search':
                control.rate_limit('product-search:'+origin,12,3600)
                data=self.body()
                result=search_products(data,self.server.db_path if not control.demo else str(control.db.execute('PRAGMA database_list').fetchone()[2]))
                result['findings']=[]
                if not control.demo and data.get('online') is True:
                    control.rate_limit('discovery:global',120,3600)
                    try:result.update(discover({'lat':data['lat'],'lon':data['lon'],'query':data['query']},self.server.db_path))
                    except ValueError as error:result['online_error']=str(error)
            elif self.command=='POST' and path=='/api/discovery':
                if control.demo: raise PermissionError('Online discovery is unavailable in the demo')
                control.rate_limit('discovery:'+origin,6,3600)
                control.rate_limit('discovery:global',120,3600)
                result=discover(self.body(),self.server.db_path)
            elif self.command=='POST' and path.startswith('/api/restaurant/public/'):
                control.rate_limit('restaurant-public:'+origin,20,600)
                result=restaurant.Restaurant(control).public(path.rsplit('/',1)[-1],self.body())
            elif self.command=='POST' and path=='/api/pos/public-receipt':
                control.rate_limit('receipt:'+origin,30,60);data=self.body()
                row=control.db.execute('SELECT * FROM pos_sales WHERE id=?',(data.get('id'),)).fetchone()
                if not row or not secrets.compare_digest(json.loads(row['payload'])['receipt_token'],str(data.get('receipt_token',''))):raise PermissionError('Receipt access denied')
                result=pos_engine.POS(control).sale_public(row)
                for field in ('customer_id','ingredients','contact','delivery_address'):result.pop(field,None)
            elif self.command=='POST' and path=='/api/signup':
                if control.demo: raise PermissionError('Use the live signup page to create an account')
                control.rate_limit('signup:'+origin,5,3600)
                result=control.signup(self.body())
            elif self.command=='POST' and path=='/api/login':
                data=self.body(); result={'token':control.login(data.get('email',''),data.get('password',''))}
            else:
                token=self.headers.get('Authorization','').removeprefix('Bearer ')
                actor=control.actor(token)
                if self.command=='GET' and path=='/api/pos/context':result=pos_engine.POS(control).context(actor)
                elif self.command=='GET' and path=='/api/admin': result=control.snapshot(actor)
                elif self.command=='POST' and path=='/api/logout':
                    with control.db: control.db.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))
                    result={'ok':True}
                elif self.command=='POST' and path=='/api/admin/order-verify':
                    data=self.body();result=engine.verify_order(data.get('id',''),actor=actor)
                elif self.command=='POST' and path.startswith('/api/merchant/'):
                    result=storefronts.Merchant(control).dispatch(actor,path.rsplit('/',1)[-1],self.body())
                elif self.command=='POST' and path.startswith('/api/pos/'):
                    result=pos_engine.POS(control).dispatch(actor,path.rsplit('/',1)[-1],self.body())
                elif self.command=='POST' and path.startswith('/api/restaurant/'):
                    result=restaurant.Restaurant(control).dispatch(actor,path.rsplit('/',1)[-1],self.body())
                elif self.command=='POST' and path.startswith('/api/admin/'):
                    result=control.mutate(actor,path.rsplit('/',1)[1],self.body())
                else: self.send_json(404,{'error':'Not found'}); return
            self.send_json(200,result)
        except PermissionError as e: self.send_json(403,{'error':str(e)})
        except (ValueError,KeyError,TypeError,sqlite3.IntegrityError) as e: self.send_json(400,{'error':str(e)})
        except Exception: self.send_json(500,{'error':'Internal error'})
        finally:
            if control is not None: control.db.close()
    def body(self):
        size=int(self.headers.get('Content-Length','0'))
        if size<=0 or size>16384: raise ValueError('Invalid body size')
        data=json.loads(self.rfile.read(size))
        if not isinstance(data,dict): raise ValueError('JSON object required')
        return data
    def send_json(self,status,data):
        body=json.dumps(data).encode(); self.send_response(status)
        self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    do_GET=handle_request
    do_POST=handle_request

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--db',default='/var/lib/pos-control/control.db')
    parser.add_argument('--bootstrap',metavar='EMAIL'); parser.add_argument('--port',type=int,default=8787)
    args=parser.parse_args(); os.umask(0o077)
    if args.bootstrap:
        Control(args.db).bootstrap(args.bootstrap,getpass.getpass('New super admin password: '))
    else:
        server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler); server.db_path=args.db; server.serve_forever()
