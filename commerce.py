"""Store marketplace, scoped integrations and hosted-checkout order records."""
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode
from agreement_gate import AgreementGate
import gateways

KEY_SCOPES=('inventory:read','inventory:write','orders:read')
RESOURCES=('marketplace','gateways','api_keys')


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS marketplace(store_id TEXT PRIMARY KEY REFERENCES stores(id),enabled INTEGER NOT NULL DEFAULT 0,category TEXT NOT NULL,description TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS gateways(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),name TEXT NOT NULL,provider TEXT NOT NULL,mode TEXT NOT NULL,secret_ref TEXT NOT NULL,public_key TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS api_keys(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),name TEXT NOT NULL,digest TEXT UNIQUE NOT NULL,prefix TEXT NOT NULL,scopes TEXT NOT NULL,expires REAL NOT NULL,active INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),gateway_id TEXT NOT NULL REFERENCES gateways(id),gateway_snapshot TEXT NOT NULL,seller_snapshot TEXT NOT NULL,request_hash TEXT NOT NULL,access_hash TEXT UNIQUE NOT NULL,email TEXT NOT NULL,total_minor INTEGER NOT NULL,currency TEXT NOT NULL,reference TEXT UNIQUE NOT NULL,status TEXT NOT NULL,provider_id TEXT NOT NULL DEFAULT '',checkout_url TEXT NOT NULL DEFAULT '',created REAL NOT NULL,verified REAL);
    CREATE TABLE IF NOT EXISTS order_items(order_id TEXT NOT NULL REFERENCES orders(id),inventory_id TEXT NOT NULL REFERENCES inventory(id),name TEXT NOT NULL,quantity INTEGER NOT NULL,price_minor INTEGER NOT NULL,PRIMARY KEY(order_id,inventory_id));
    ''')


class Commerce:
    def __init__(self, control, demo=False):
        self.c=control;self.db=control.db;self.demo=demo

    def store(self, identifier, actor=None, write=False):
        row=self.db.execute('SELECT * FROM stores WHERE id=?',(identifier,)).fetchone()
        if not row:raise ValueError('Unknown store')
        if actor:self.c.authorize(actor,row['tenant_id'],identifier,write=write)
        return dict(row)

    def mutate(self, actor, resource, data):
        req=self.c.required
        if resource in ('gateways','api_keys') and not actor['super']:raise PermissionError('Super admin required')
        store=self.store(req(data,'store_id'),actor,write=True)
        result={}
        with self.db:
            if resource=='marketplace':
                if type(data.get('enabled')) is not bool:raise ValueError('Boolean enabled required')
                identifier=store['id']
                self.db.execute('INSERT INTO marketplace VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET enabled=excluded.enabled,category=excluded.category,description=excluded.description',(identifier,data['enabled'],req(data,'category'),req(data,'description')))
            elif resource=='gateways':
                identifier=data.get('id') or secrets.token_hex(12)
                existing=self.db.execute('SELECT store_id,provider,mode,secret_ref FROM gateways WHERE id=?',(identifier,)).fetchone()
                provider=req(data,'provider');mode=req(data,'mode')
                if provider not in (*gateways.PROVIDERS,'demo') or mode not in ('test','live'):raise ValueError('Unsupported provider or mode')
                if type(data.get('enabled')) is not bool:raise ValueError('Boolean enabled required')
                if provider=='demo' and (not self.demo or mode!='test'):raise PermissionError('Simulator is available only inside an isolated demo')
                if self.demo and mode=='live':raise PermissionError('Live gateway mode is unavailable in demo')
                if existing and existing['store_id']!=store['id']:raise PermissionError('Cannot move gateway between stores')
                if existing and self.db.execute('SELECT 1 FROM orders WHERE gateway_id=?',(identifier,)).fetchone() and (provider,mode,data.get('secret_ref',''))!=(existing['provider'],existing['mode'],existing['secret_ref']):raise ValueError('Create a new gateway for a different provider or credential reference; retain the existing one for reconciliation')
                ref=data.get('secret_ref','')
                if provider!='demo' and not re.fullmatch('GLOBALPOS_'+provider.upper()+'_'+mode.upper()+r'_[A-Z0-9_]{1,80}',ref):raise ValueError('Use a server secret reference, not a raw gateway secret')
                public=data.get('public_key','')
                if not isinstance(public,str) or len(public)>500 or public.startswith(('sk_','FLWSECK')):raise ValueError('Do not paste secret keys into public key fields')
                self.db.execute('INSERT INTO gateways VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,provider=excluded.provider,mode=excluded.mode,secret_ref=excluded.secret_ref,public_key=excluded.public_key,enabled=excluded.enabled',(identifier,store['id'],req(data,'name'),provider,mode,ref,public,data['enabled']))
            elif resource=='api_keys':
                action=data.get('action','issue')
                if action not in ('issue','revoke','rotate'):raise ValueError('Unknown API key action')
                old=None
                if action in ('revoke','rotate'):
                    old=self.db.execute('SELECT * FROM api_keys WHERE id=? AND store_id=?',(req(data,'id'),store['id'])).fetchone()
                    if not old:raise ValueError('Unknown API key')
                    self.db.execute('UPDATE api_keys SET active=0 WHERE id=?',(old['id'],))
                if action=='revoke':identifier=old['id']
                else:
                    scopes=data.get('scopes',','.join(json.loads(old['scopes'])) if old else '')
                    if not isinstance(scopes,str):raise ValueError('Comma-separated scopes required')
                    scopes=sorted(set(x.strip() for x in scopes.split(',') if x.strip()))
                    if not scopes or any(x not in KEY_SCOPES for x in scopes):raise ValueError('Use inventory:read, inventory:write or orders:read')
                    days=data.get('expires_days',90)
                    if type(days) is not int or not 1<=days<=365:raise ValueError('Key expiry must be 1–365 days')
                    raw='gpos_'+secrets.token_urlsafe(32);identifier=secrets.token_hex(12)
                    name=data.get('name') or (old['name'] if old else '')
                    if not isinstance(name,str) or not name.strip() or len(name)>100:raise ValueError('Key name required')
                    self.db.execute('INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?)',(identifier,store['id'],name,self.digest(raw),raw[:12]+'…'+raw[-4:],json.dumps(scopes),time.time()+days*86400,1,time.time()))
                    result['secret']=raw
            else:raise ValueError('Unknown commerce resource')
            self.c.log(actor,resource+'.'+data.get('action','save'),identifier,{'store_id':store['id'],'name':data.get('name','')})
        return dict(id=identifier,**result)

    @staticmethod
    def digest(value):return hashlib.sha256(value.encode()).hexdigest()

    def api_actor(self, key, required, store):
        row=self.db.execute('SELECT * FROM api_keys WHERE digest=? AND active=1 AND expires>?',(self.digest(key),time.time())).fetchone()
        if not row or row['store_id']!=store or required not in json.loads(row['scopes']):raise PermissionError('API key scope or store access denied')
        self.c.rate_limit('integration:'+row['id'],120,60)
        record=self.store(store)
        active=self.db.execute("SELECT 1 FROM tenants WHERE id=? AND status='active'",(record['tenant_id'],)).fetchone()
        if not self.c.effective_features(record)['integrations']:raise PermissionError('Integrations are disabled for this store')
        if record['status']!='active' or not active:raise PermissionError('Store or tenant is suspended')
        return {'id':'api:'+row['id'],'super':False,'api_store':store,'api_scopes':json.loads(row['scopes'])}

    def snapshot(self, actor, stores):
        ids={s['id'] for s in stores}
        out={'marketplace':[],'gateways':[],'api_keys':[],'orders':[]}
        for resource in ('marketplace','gateways'):
            for row in self.db.execute('SELECT * FROM '+resource):
                if row['store_id'] not in ids:continue
                item=dict(row)
                if resource=='gateways':
                    item['configured']=item['provider']=='demo' if self.demo else False
                    if not self.demo and item['provider']!='demo':
                        try:gateways.secret(item);item['configured']=True
                        except ValueError:pass
                out[resource].append(item)
        if actor['super']:
            out['api_keys']=[dict(x,scopes=','.join(json.loads(x['scopes']))) for x in self.db.execute('SELECT id,store_id,name,prefix,scopes,expires,active,created FROM api_keys')]
        for row in self.db.execute('SELECT id,store_id,gateway_id,total_minor,currency,status,created,verified FROM orders ORDER BY created DESC LIMIT 500'):
            if row['store_id'] in ids:out['orders'].append(dict(row))
        return out

    def marketplace(self):
        rows=[]
        for store in self.c.discover():
            flags=self.c.effective_features(store)
            if not flags['marketplace']:continue
            listing=self.db.execute('SELECT * FROM marketplace WHERE store_id=? AND enabled=1',(store['id'],)).fetchone()
            if not listing:continue
            item=dict(store,category=listing['category'],description=listing['description'])
            item['products']=self.c.public_catalog(store['id'])
            item['gateways']=[]
            for gateway in self.db.execute('SELECT * FROM gateways WHERE store_id=? AND enabled=1',(store['id'],)):
                if not flags['payments']:continue
                if self.demo and gateway['provider']!='demo':continue
                if not self.demo and gateway['provider']=='demo':continue
                if gateway['mode']=='live' and os.environ.get('GLOBALPOS_ENABLE_LIVE_PAYMENTS')!='1':continue
                item['gateways'].append({k:gateway[k] for k in ('id','name','provider','mode')})
            rows.append(item)
        text,digest=AgreementGate(self.db).document('shopper-v1')
        return dict(stores=rows,terms_version='shopper-v1',terms_digest=digest,demo=self.demo)

    def checkout(self, data):
        req=self.c.required;key=req(data,'checkout_key')
        if not re.fullmatch(r'[a-f0-9]{64}',key):raise ValueError('A fresh 256-bit checkout key is required')
        if data.get('accepted') is not True:raise ValueError('Explicit shopper agreement acceptance required')
        gate=AgreementGate(self.db)
        if data.get('terms_version')!='shopper-v1' or data.get('terms_digest')!=gate.document('shopper-v1')[1]:raise ValueError('Read and accept the current shopper agreement')
        store=req(data,'store_id');gateway_id=req(data,'gateway_id');email=req(data,'email')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):raise ValueError('Valid customer email required')
        items=data.get('items')
        if not isinstance(items,list) or not 1<=len(items)<=50:raise ValueError('Use 1–50 basket lines from one store')
        quantities={}
        for line in items:
            if not isinstance(line,dict) or not isinstance(line.get('id'),str) or type(line.get('quantity')) is not int or not 1<=line['quantity']<=100:raise ValueError('Invalid basket line')
            if line['id'] in quantities:raise ValueError('Duplicate basket item')
            quantities[line['id']]=line['quantity']
        request_hash=self.digest(json.dumps([store,gateway_id,email,sorted(quantities.items())]))
        existing=self.db.execute('SELECT * FROM orders WHERE access_hash=?',(self.digest(key),)).fetchone()
        if existing:
            if existing['request_hash']!=request_hash:raise ValueError('Checkout key was already used with a different basket')
            return self.order(existing['id'],key)
        current=next((s for s in self.marketplace()['stores'] if s['id']==store),None)
        if not current:raise ValueError('Store is not published in the marketplace')
        if not self.c.effective_features(current)['payments']:raise PermissionError('Payments are disabled for this store')
        config=self.db.execute('SELECT * FROM gateways WHERE id=? AND store_id=? AND enabled=1',(gateway_id,store)).fetchone()
        if not config:raise ValueError('Gateway unavailable for this store')
        config=dict(config)
        if self.demo and config['provider']!='demo':raise PermissionError('Demo cannot contact real payment providers')
        if not self.demo and config['provider']=='demo':raise PermissionError('Demo gateway cannot be used in production')
        if config['mode']=='live' and (self.demo or os.environ.get('GLOBALPOS_ENABLE_LIVE_PAYMENTS')!='1'):raise PermissionError('Live payments are disabled on this server')
        if current['currency'] not in gateways.EXPONENTS:raise ValueError('Currency is not configured for checkout')
        if config['provider']!='demo':gateways.secret(config)
        identifier=secrets.token_hex(12);reference='gpos_'+identifier
        try:
            self.db.execute('BEGIN IMMEDIATE')
            # Recheck publication and gateway state under the same write lock.
            if not self.db.execute("SELECT 1 FROM stores s JOIN tenants t ON s.tenant_id=t.id JOIN marketplace m ON m.store_id=s.id JOIN gateways g ON g.store_id=s.id WHERE s.id=? AND s.status='active' AND t.status='active' AND m.enabled=1 AND g.id=? AND g.enabled=1",(store,gateway_id)).fetchone():raise ValueError('Store or gateway is no longer available')
            if not self.c.effective_features(self.store(store))['payments'] or not self.c.effective_features(self.store(store))['marketplace']:raise PermissionError('Checkout was disabled')
            if self.demo and self.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]>=50:raise ValueError('Demo order limit reached')
            lines=[];total=0
            for item,quantity in quantities.items():
                row=self.db.execute('SELECT * FROM inventory WHERE id=? AND store_id=?',(item,store)).fetchone()
                if not row or row['quantity']<quantity:raise ValueError('An item is unavailable or belongs to another store')
                total+=row['price_minor']*quantity
                lines.append((identifier,item,row['name'],quantity,row['price_minor']))
            if not 0<total<=100000000:raise ValueError('Order total is outside supported limits')
            self.db.execute('INSERT INTO orders(id,store_id,gateway_id,gateway_snapshot,seller_snapshot,request_hash,access_hash,email,total_minor,currency,reference,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,store,gateway_id,json.dumps(config),json.dumps({k:current[k] for k in ('id','name','address','country','currency','lat','lon')}),request_hash,self.digest(key),email,total,current['currency'],reference,'initializing',time.time()))
            self.db.executemany('INSERT INTO order_items VALUES(?,?,?,?,?)',lines)
            for _,item,_,quantity,_ in lines:self.db.execute('UPDATE inventory SET quantity=quantity-? WHERE id=?',(quantity,item))
            self.db.execute('INSERT INTO agreement_events(actor,store,version,digest,action) VALUES(?,?,?,?,?)',('guest:'+self.digest(key),store,'shopper-v1',data['terms_digest'],'accept'))
            self.db.commit()
        except Exception:
            self.db.rollback()
            # A concurrent retry with this same key should return the first order.
            row=self.db.execute('SELECT id,request_hash FROM orders WHERE access_hash=?',(self.digest(key),)).fetchone()
            if row and row['request_hash']==request_hash:return self.order(row['id'],key)
            raise
        if config['provider']=='demo':
            with self.db:self.db.execute("UPDATE orders SET status='awaiting_demo',provider_id=? WHERE id=?",(reference,identifier))
        else:
            order=dict(self.db.execute('SELECT * FROM orders WHERE id=?',(identifier,)).fetchone())
            query={'order':identifier}
            public=os.environ.get('GLOBALPOS_PUBLIC_URL','https://pos.africandroid.world').rstrip('/')
            if not re.fullmatch(r'https://[a-z0-9.-]+(?::[0-9]+)?',public):raise ValueError('Configure a secure public return origin')
            try:
                result=gateways.checkout(config,order,email,public+'/marketplace.html?'+urlencode(query))
                with self.db:self.db.execute("UPDATE orders SET status='pending',provider_id=?,checkout_url=? WHERE id=?",(result['provider_id'],result['checkout_url'],identifier))
            except gateways.GatewayError:
                with self.db:self.db.execute("UPDATE orders SET status='initialization_uncertain' WHERE id=?",(identifier,))
        return self.order(identifier,key)

    def order(self, identifier, key):
        row=self.db.execute('SELECT * FROM orders WHERE id=? AND access_hash=?',(identifier,self.digest(key))).fetchone()
        if not row:raise PermissionError('Order access denied')
        result={k:row[k] for k in ('id','store_id','total_minor','currency','status','checkout_url','created','verified')}
        result['seller']=json.loads(row['seller_snapshot'])
        result['items']=[dict(x) for x in self.db.execute('SELECT inventory_id,name,quantity,price_minor FROM order_items WHERE order_id=?',(identifier,))]
        result['test_mode']=json.loads(row['gateway_snapshot'])['mode']=='test';result['demo']=self.demo
        return result

    def verify_order(self, identifier, key=None, actor=None, simulate=None):
        row=self.db.execute('SELECT * FROM orders WHERE id=?',(identifier,)).fetchone()
        if not row:raise ValueError('Unknown order')
        if actor:self.store(row['store_id'],actor)
        elif not key or row['access_hash']!=self.digest(key):raise PermissionError('Order access denied')
        if row['status'] in ('paid','demo_paid','demo_cancelled'):
            return {'id':identifier,'status':row['status']}
        config=json.loads(row['gateway_snapshot'])
        if config['provider']=='demo':
            if not self.demo:raise PermissionError('Simulator is not available in production')
            if simulate not in ('pay','cancel'):return {'id':identifier,'status':row['status']}
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                fresh=self.db.execute('SELECT status FROM orders WHERE id=?',(identifier,)).fetchone()[0]
                if fresh=='awaiting_demo':
                    if simulate=='cancel':
                        for item in self.db.execute('SELECT inventory_id,quantity FROM order_items WHERE order_id=?',(identifier,)):
                            self.db.execute('UPDATE inventory SET quantity=quantity+? WHERE id=?',(item['quantity'],item['inventory_id']))
                    self.db.execute('UPDATE orders SET status=?,verified=? WHERE id=?',('demo_paid' if simulate=='pay' else 'demo_cancelled',time.time(),identifier))
        else:
            if self.demo:raise PermissionError('Demo cannot verify real provider transactions')
            # A browser return or a claimed status never proves payment.
            if gateways.verify(config,dict(row)):
                with self.db:self.db.execute("UPDATE orders SET status='paid',verified=? WHERE id=? AND status!='paid'",(time.time(),identifier))
        current=self.db.execute('SELECT status FROM orders WHERE id=?',(identifier,)).fetchone()[0]
        return {'id':identifier,'status':current}
