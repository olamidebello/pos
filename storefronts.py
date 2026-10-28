"""Opt-in tenant storefronts: private drafts, explicit publication and scoped merchant management."""
import hashlib,json,math,re,secrets,time,unicodedata
from zoneinfo import ZoneInfo
from agreement_gate import AgreementGate
from commerce import Commerce
from pos_engine import label,dump


def initialize(db):
 db.execute('CREATE TABLE IF NOT EXISTS storefronts(store_id TEXT PRIMARY KEY REFERENCES stores(id),slug TEXT UNIQUE NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL)')


def validate_preferences(data):
 country=label(data.get('country'),'country',2).upper();currency=label(data.get('currency'),'currency',3).upper();zone=label(data.get('timezone'),'timezone',100)
 if not re.fullmatch('[A-Z]{2}',country) or not re.fullmatch('[A-Z]{3}',currency):raise ValueError('Use two-letter country and three-letter currency codes')
 try:ZoneInfo(zone)
 except (ValueError,KeyError):raise ValueError('Choose a valid timezone') from None
 return {'country':country,'currency':currency,'timezone':zone,'category':data.get('category','Retail') if isinstance(data.get('category','Retail'),str) else 'Retail'}

def new_slug(name):
 prefix=re.sub('[^a-z0-9]+','-',unicodedata.normalize('NFKD',name).encode('ascii','ignore').decode().lower()).strip('-')[:45] or 'store'
 return prefix+'-'+secrets.token_hex(4)


def create_draft(db,tenant,name,preferences):
 settings=validate_preferences(preferences);sid=secrets.token_hex(12);slug=new_slug(name)
 db.execute('INSERT INTO stores VALUES(?,?,?,?,?,?,?,?,?,?,?)',(sid,tenant,name,'',settings['country'],settings['currency'],settings['timezone'],'en',0.,0.,'draft'))
 data={'headline':'','description':'','accent':'#17624c','logo_url':'','hours':'','virtual_only':False,'location_confirmed':False,'category':settings['category']}
 db.execute('INSERT INTO storefronts VALUES(?,?,?,?,?,?)',(sid,slug,'draft',dump(data),1,time.time()))
 return {'store_id':sid,'slug':slug,'state':'draft'}


class Merchant:
 def __init__(self,control):self.c=control;self.db=control.db
 def context(self,actor):
  tenants=[]
  for tenant in self.db.execute('SELECT * FROM tenants ORDER BY name'):
   try:self.c.authorize(actor,tenant['id'],write=True)
   except PermissionError:continue
   tenants.append(dict(tenant))
  stores=[]
  for row in self.db.execute('SELECT * FROM stores ORDER BY name'):
   try:self.c.authorize(actor,row['tenant_id'],row['id'],write=True)
   except PermissionError:continue
   front=self.db.execute('SELECT * FROM storefronts WHERE store_id=?',(row['id'],)).fetchone();settings=json.loads(front['payload']) if front else {'headline':'','description':'','accent':'#17624c','logo_url':'','hours':'','category':'Retail','virtual_only':False,'location_confirmed':False}
   stores.append(dict(row,storefront=dict(settings,slug=front['slug'] if front else '',state=front['state'] if front else 'draft',revision=front['revision'] if front else 0),features=self.c.effective_features(row),products=self.c.catalog(actor,row['id'])))
  terms,terms_digest=AgreementGate(self.db).document('store-v1')
  return {'me':{k:actor[k] for k in ('id','email','super')},'tenants':tenants,'stores':stores,'terms_digest':terms_digest,'terms_version':'store-v1','notice':'Draft storefronts are private. Publishing exposes the selected name, address, coordinates, description, branding, hours and product catalog to shoppers. Marketplace and hosted payment availability follow platform and store configuration.'}
 def dispatch(self,actor,action,body):
  if action=='context':return self.context(actor)
  if action=='inventory':
   item=dict(body);item.pop('action',None);return self.c.mutate(actor,'inventory',item)
  if action not in ('save','publish','unpublish'):raise ValueError('Unknown merchant operation')
  sid=body.get('store_id')
  existing=self.db.execute('SELECT * FROM stores WHERE id=?',(sid,)).fetchone() if sid else None
  if existing:self.c.authorize(actor,existing['tenant_id'],existing['id'],write=True)
  elif action!='save':raise ValueError('Create the store first')
  if action=='save':
   tenant=body.get('tenant_id')
   if existing and tenant!=existing['tenant_id']:raise PermissionError('Cannot move a store between companies')
   self.c.authorize(actor,tenant,existing['id'] if existing else '',write=True)
   settings=validate_preferences(body);name=label(body.get('name'),'store name',150);address=label(body.get('address',''),'store address',500,False)
   state=body.get('status','draft')
   if state not in ('draft','active'):raise ValueError('Choose private draft or active store')
   virtual=body.get('virtual_only') is True;confirmed=body.get('location_confirmed') is True
   lat=float(body.get('lat') or 0);lon=float(body.get('lon') or 0)
   if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:raise ValueError('Invalid store coordinates')
   if state=='active' and (not address or not virtual and not confirmed):raise ValueError('An active store requires an address and confirmed location, or an explicitly online-only storefront')
   accent=label(body.get('accent','#17624c'),'accent',7)
   if not re.fullmatch('#[0-9a-fA-F]{6}',accent):raise ValueError('Choose a valid brand color')
   logo=label(body.get('logo_url',''),'logo URL',1000,False)
   if logo and not re.fullmatch(r'https://[^\s@]+',logo):raise ValueError('Logo must use a public HTTPS URL without credentials')
   data={'headline':label(body.get('headline',''),'headline',150,False),'description':label(body.get('description',''),'description',2000,False),'hours':label(body.get('hours',''),'hours',1000,False),'category':label(body.get('category','Retail'),'category',100),'accent':accent,'logo_url':logo,'virtual_only':virtual,'location_confirmed':confirmed}
   with self.db:
    self.db.execute('BEGIN IMMEDIATE')
    old=self.db.execute('SELECT * FROM storefronts WHERE store_id=?',(sid,)).fetchone() if sid else None;revision=old['revision'] if old else 0
    if revision!=body.get('revision',0):raise ValueError('Storefront changed; reload before saving')
    if existing and existing['currency']!=settings['currency'] and self.db.execute('SELECT 1 FROM inventory WHERE store_id=?',(sid,)).fetchone():raise ValueError('Cannot change currency while inventory exists')
    sid=sid or secrets.token_hex(12);slug=old['slug'] if old else new_slug(name)
    self.db.execute('INSERT INTO stores VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,address=excluded.address,country=excluded.country,currency=excluded.currency,timezone=excluded.timezone,language=excluded.language,lat=excluded.lat,lon=excluded.lon,status=excluded.status',(sid,tenant,name,address,settings['country'],settings['currency'],settings['timezone'],label(body.get('language','en'),'language',30),lat,lon,state))
    # Any branding/location/catalog-policy edit returns publication to draft for explicit approval.
    self.db.execute('INSERT OR REPLACE INTO storefronts VALUES(?,?,?,?,?,?)',(sid,slug,'draft',dump(data),revision+1,time.time()));self.db.execute('UPDATE marketplace SET enabled=0 WHERE store_id=?',(sid,));self.c.log(actor,'storefront_saved',sid,{'state':state,'revision':revision+1})
   return {'store_id':sid,'slug':slug,'revision':revision+1,'state':'draft'}
  front=self.db.execute('SELECT * FROM storefronts WHERE store_id=?',(sid,)).fetchone()
  if not front:raise ValueError('Save storefront branding first')
  data=json.loads(front['payload'])
  if action=='publish':
   if existing['status']!='active':raise ValueError('Finish store details and activate the store before publication')
   if not self.c.effective_features(existing)['marketplace']:raise PermissionError('Platform marketplace feature must be enabled; your private draft is retained')
   if not self.db.execute('SELECT 1 FROM inventory WHERE store_id=?',(sid,)).fetchone():raise ValueError('Add at least one product before publishing')
   gate=AgreementGate(self.db);current=gate.document('store-v1')[1]
   if body.get('publish_confirmed') is not True or body.get('terms_digest')!=current:raise ValueError('Review current store terms and explicitly authorize public listing')
  with self.db:
   self.db.execute('BEGIN IMMEDIATE')
   if self.db.execute('UPDATE storefronts SET state=?,revision=revision+1,updated=? WHERE store_id=? AND revision=?',('published' if action=='publish' else 'draft',time.time(),sid,body.get('revision'))).rowcount!=1:raise ValueError('Storefront changed; reload')
   self.db.execute('INSERT INTO marketplace VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET enabled=excluded.enabled,category=excluded.category,description=excluded.description',(sid,int(action=='publish'),data['category'],data['description'] or existing['name']))
   if action=='publish':self.db.execute('INSERT INTO agreement_events(actor,store,version,digest,action) VALUES(?,?,?,?,?)',(actor['id'],sid,'store-v1',current,'accept'))
   self.c.log(actor,'storefront_'+action,sid,{'slug':front['slug']})
  return {'ok':True,'slug':front['slug'],'state':'published' if action=='publish' else 'draft'}
 def public(self,slug):
  if not isinstance(slug,str) or not re.fullmatch('[a-z0-9-]{1,80}',slug):raise ValueError('Invalid storefront')
  row=self.db.execute("SELECT f.*,s.status,t.status AS tenant_status FROM storefronts f JOIN stores s ON s.id=f.store_id JOIN tenants t ON t.id=s.tenant_id WHERE f.slug=? AND f.state='published' AND s.status='active' AND t.status='active'",(slug,)).fetchone()
  if not row:raise ValueError('Storefront is not published')
  market=Commerce(self.c,demo=self.c.demo).marketplace();store=next((s for s in market['stores'] if s['id']==row['store_id']),None)
  if not store:raise ValueError('Storefront marketplace is unavailable')
  return {'store':store,'branding':json.loads(row['payload']),'slug':slug,'terms_version':market['terms_version'],'terms_digest':market['terms_digest'],'demo':self.c.demo}
