"""Restaurant operations integrated with the shared store, role, inventory and POS ledger."""
import hashlib,json,secrets,time,uuid
from decimal import Decimal,InvalidOperation
from pos_engine import POS,integer,label,key,dump,digest,rounded

MODES=('dine_in','takeout','delivery','drive_through','curbside','phone','online','catering')
TRANSITIONS={'requested':{'open','cancelled'},'open':{'queued','cancelled'},'queued':{'preparing','cancelled'},'preparing':{'ready','cancelled'},'ready':{'served','cancelled'},'served':set(),'cancelled':set()}


def milli(value):
 try:n=Decimal(str(value))*1000
 except (InvalidOperation,ValueError):raise ValueError('Use a valid ingredient quantity') from None
 if not n.is_finite() or n!=n.to_integral_value() or not 0<=n<=10**12:raise ValueError('Ingredient quantities require up to three decimal places and must be nonnegative')
 return int(n)


def initialize(db):
 db.executescript('''
 CREATE TABLE IF NOT EXISTS restaurant_tables(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),name TEXT NOT NULL,capacity INTEGER NOT NULL,room TEXT NOT NULL,x INTEGER NOT NULL,y INTEGER NOT NULL,state TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS restaurant_menu(id TEXT PRIMARY KEY REFERENCES inventory(id),store_id TEXT NOT NULL REFERENCES stores(id),payload TEXT NOT NULL,revision INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS restaurant_ingredients(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),name TEXT NOT NULL,unit TEXT NOT NULL,quantity_milli INTEGER NOT NULL,cost_minor_per_1000 INTEGER NOT NULL,low_milli INTEGER NOT NULL,payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS restaurant_ingredient_moves(id TEXT PRIMARY KEY,store_id TEXT NOT NULL,ingredient_id TEXT NOT NULL,delta_milli INTEGER NOT NULL,reason TEXT NOT NULL,reference TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS restaurant_orders(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),table_id TEXT NOT NULL DEFAULT '',owner TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL,sale_id TEXT UNIQUE,request_key TEXT NOT NULL,request_digest TEXT NOT NULL,guest_hash TEXT UNIQUE,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(store_id,request_key));
 CREATE TABLE IF NOT EXISTS restaurant_reservations(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),table_id TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL,guests INTEGER NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,owner TEXT NOT NULL,revision INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS restaurant_waitlist(id TEXT PRIMARY KEY,store_id TEXT NOT NULL,name TEXT NOT NULL,guests INTEGER NOT NULL,state TEXT NOT NULL,estimate_minutes INTEGER NOT NULL,created REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS restaurant_delivery(order_id TEXT PRIMARY KEY REFERENCES restaurant_orders(id),store_id TEXT NOT NULL,driver TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS restaurant_safety(id TEXT PRIMARY KEY,store_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,owner TEXT NOT NULL,created REAL NOT NULL);
 CREATE INDEX IF NOT EXISTS restaurant_order_queue ON restaurant_orders(store_id,state,updated);
 CREATE INDEX IF NOT EXISTS restaurant_reservation_slots ON restaurant_reservations(table_id,start,end);
 ''')


class Restaurant:
 def __init__(self,control):self.c=control;self.db=control.db;self.pos=POS(control)
 def check(self,actor,store,cap=None):
  record,permissions=self.pos.check(actor,store,cap)
  if not self.c.effective_features(record).get('restaurant'):raise PermissionError('Restaurant module must be enabled by the super administrator')
  return record,permissions
 def ingredient_move(self,actor,store,iid,delta,reason,reference):
  self.db.execute('INSERT INTO restaurant_ingredient_moves VALUES(?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),store,iid,delta,reason,reference,actor['id'] if actor else 'guest',time.time()))
 def table(self,store,tid):
  if not tid:return None
  row=self.db.execute('SELECT * FROM restaurant_tables WHERE id=? AND store_id=?',(tid,store)).fetchone()
  if not row:raise ValueError('Table not found in this restaurant')
  return row
 def menu(self,store):
  return [dict(id=r['id'],revision=r['revision'],price_minor=r['price_minor'],quantity=r['quantity'],**json.loads(r['payload'])) for r in self.db.execute('SELECT m.*,i.price_minor,i.quantity FROM restaurant_menu m JOIN inventory i ON i.id=m.id WHERE m.store_id=? ORDER BY i.name',(store,))]
 def order(self,store,oid):
  row=self.db.execute('SELECT * FROM restaurant_orders WHERE id=? AND store_id=?',(oid,store)).fetchone()
  if not row:raise ValueError('Order not found in this restaurant')
  return row
 def order_data(self,row,private=False):
  data=json.loads(row['payload']);data.pop('guest_token',None)
  if not private:
   for field in ('customer_id','contact','delivery_address'):data.pop(field,None)
  return dict(data,**{k:row[k] for k in ('id','table_id','owner','mode','state','revision','sale_id','created','updated')})
 def price_order(self,store,body):
  record=self.db.execute('SELECT * FROM stores WHERE id=?',(store,)).fetchone();settings,revision=self.pos.settings(store);lines=[];ingredients={};items=body.get('items')
  if not isinstance(items,list) or not 1<=len(items)<=50:raise ValueError('Use 1–50 menu lines')
  for index,item in enumerate(items):
   row=self.db.execute('SELECT m.*,i.price_minor,i.quantity FROM restaurant_menu m JOIN inventory i ON i.id=m.id WHERE m.id=? AND m.store_id=?',(item.get('id'),store)).fetchone()
   if not row:raise ValueError('Menu item not found')
   menu=json.loads(row['payload']);qty=integer(item.get('quantity'),'quantity',1,100)
   if not menu['active']:raise ValueError('Menu item is unavailable')
   modifiers=item.get('modifiers',[])
   if not isinstance(modifiers,list) or len(modifiers)!=len(set(modifiers)) or len(modifiers)>20:raise ValueError('Invalid or duplicate modifiers')
   selected=[];price=row['price_minor'];recipe=list(menu['recipe'])
   for group in menu['modifier_groups']:
    choices=[choice for choice in group['choices'] if choice['id'] in modifiers]
    if not group['minimum']<=len(choices)<=group['maximum']:raise ValueError('Check modifier group '+group['name'])
    selected.extend(choices)
   if set(modifiers)!={m['id'] for m in selected}:raise ValueError('Unknown modifier choice')
   price+=sum(m['price_minor'] for m in selected)
   for modifier in selected:recipe.extend(modifier.get('recipe',[]))
   recipe_cost=0
   for ingredient in recipe:
    ingredients[ingredient['id']]=ingredients.get(ingredient['id'],0)+ingredient['quantity_milli']*qty
    cost=self.db.execute('SELECT cost_minor_per_1000 FROM restaurant_ingredients WHERE id=? AND store_id=?',(ingredient['id'],store)).fetchone()
    if not cost:raise ValueError('Recipe ingredient unavailable')
    recipe_cost+=rounded(ingredient['quantity_milli']*cost[0],1000000)
   notes=label(item.get('notes',''),'cooking instructions',500,False)
   lines.append({'line':index,'id':row['id'],'name':menu['name'],'quantity':qty,'price_minor':price,'modifiers':[m['name'] for m in selected],'notes':notes,'allergens':'; '.join([menu['allergens']]+[m.get('allergens','') for m in selected if m.get('allergens')]),'station':menu['station'],'net_minor':qty*price,'tax_minor':rounded(qty*price*menu['tax_bps']),'returned':0,'cost_minor':recipe_cost})
  totals={}
  for line in lines:totals[line['id']]=totals.get(line['id'],0)+line['quantity']
  for iid,qty in totals.items():
   if self.db.execute('SELECT quantity FROM inventory WHERE id=?',(iid,)).fetchone()[0]<qty:raise ValueError('Insufficient menu availability')
  for iid,qty in ingredients.items():
   row=self.db.execute('SELECT * FROM restaurant_ingredients WHERE id=? AND store_id=?',(iid,store)).fetchone()
   if not row or row['quantity_milli']<qty:raise ValueError('Insufficient ingredient availability')
  for line in lines:line.update(gross_minor=line['net_minor'],discount_minor=0,total_minor=line['net_minor']+line['tax_minor'],tax_bps=next(m['tax_bps'] for m in self.menu(store) if m['id']==line['id']))
  subtotal=sum(l['net_minor'] for l in lines);service=rounded(subtotal*settings['service_bps']);service_tax=rounded(service*settings['tax_bps']) if settings['service_taxable'] else 0;tip=integer(body.get('tip_minor',0),'tip_minor',0,10**9)
  if tip and body.get('tip_confirmed') is not True:raise ValueError('Explicit customer authorization required for a tip')
  mode=body.get('mode','dine_in')
  if mode not in MODES:raise ValueError('Unknown order type')
  table=self.table(store,body.get('table_id',''))
  if mode=='dine_in' and not table:raise ValueError('Select a table for dine-in')
  if table and table['state']=='cleaning':raise ValueError('Table is awaiting cleaning')
  result={'store_id':store,'currency':record['currency'],'minor_exponent':settings['minor_exponent'],'settings_revision':revision,'customer_id':body.get('customer_id',''),'lines':lines,'ingredients':ingredients,'subtotal_minor':subtotal,'discount_minor':0,'coupon':'','tax_minor':sum(l['tax_minor'] for l in lines)+service_tax,'service_minor':service,'service_tax_minor':service_tax,'tip_minor':tip,'total_minor':sum(l['total_minor'] for l in lines)+service+service_tax+tip,'is_demo':self.c.demo,'guests':integer(body.get('guests',1),'guests',1,1000),'contact':label(body.get('contact',''),'contact',200,False),'delivery_address':label(body.get('delivery_address',''),'delivery address',500,False),'priority':integer(body.get('priority',0),'priority',0,3),'expected_minutes':integer(body.get('expected_minutes',20),'expected_minutes',1,240)}
  if result['customer_id'] and not self.db.execute('SELECT 1 FROM pos_customers WHERE id=? AND store_id=?',(result['customer_id'],store)).fetchone():raise ValueError('Customer not found')
  if table and result['guests']>table['capacity']:raise ValueError('Guest count exceeds table capacity')
  result['quote_digest']=digest(result);return result
 def payment_quote(self,actor,store,oid):
  self.check(actor,store,'sell');row=self.order(store,oid)
  if row['state'] not in ('queued','preparing','ready','served') or row['sale_id']:raise ValueError('Send this order to the kitchen before collecting its bill; it must not have another sale')
  data=json.loads(row['payload']);data.pop('guest_token',None);data['restaurant_order_id']=oid;data['order_revision']=row['revision'];data['quote_digest']=digest({k:v for k,v in data.items() if k!='quote_digest'});return data
 def bind_sale(self,oid,sid):
  if self.db.execute('UPDATE restaurant_orders SET sale_id=?,updated=? WHERE id=? AND sale_id IS NULL',(sid,time.time(),oid)).rowcount!=1:raise ValueError('Order already has a bill')
 def release_if_paid(self,row):
  if row['table_id'] and row['state']=='served' and row['sale_id']:
   paid=self.db.execute('SELECT state FROM pos_sales WHERE id=?',(row['sale_id'],)).fetchone()
   if paid and paid['state'] in ('paid','demo_paid'):
    others=self.db.execute("SELECT 1 FROM restaurant_orders o LEFT JOIN pos_sales s ON s.id=o.sale_id WHERE o.table_id=? AND o.id<>? AND o.state!='cancelled' AND (o.state!='served' OR s.state IS NULL OR s.state NOT IN ('paid','demo_paid'))",(row['table_id'],row['id'])).fetchone()
    if not others:self.db.execute("UPDATE restaurant_tables SET state='cleaning',revision=revision+1 WHERE id=?",(row['table_id'],))
 def public_table(self,token):
  table=self.db.execute('SELECT * FROM restaurant_tables WHERE token_hash=?',(hashlib.sha256(str(token).encode()).hexdigest(),)).fetchone()
  if not table:raise PermissionError('Unknown QR table link')
  store=self.db.execute("SELECT s.*,t.status AS tenant_status FROM stores s JOIN tenants t ON t.id=s.tenant_id WHERE s.id=?",(table['store_id'],)).fetchone()
  if not store or store['status']!='active' or store['tenant_status']!='active':raise PermissionError('Restaurant unavailable')
  flags=self.c.effective_features(store)
  if not all(flags.get(f) for f in ('pos','restaurant','scan_go')):raise PermissionError('QR ordering is disabled')
  return table,store
 def public_context(self,body):
  if body.get('table_token'):return self.public_table(body['table_token'])
  store=self.db.execute("SELECT s.*,t.status AS tenant_status FROM stores s JOIN tenants t ON t.id=s.tenant_id WHERE s.id=?",(body.get('store_id'),)).fetchone()
  if not store or store['status']!='active' or store['tenant_status']!='active':raise PermissionError('Restaurant unavailable')
  if not self.pos.settings(store['id'])[0].get('public_ordering'):raise PermissionError('Restaurant online requests are disabled')
  if not all(self.c.effective_features(store).get(flag) for flag in ('pos','restaurant','scan_go','marketplace')):raise PermissionError('Public restaurant ordering is disabled')
  published=self.db.execute('SELECT enabled FROM marketplace WHERE store_id=?',(store['id'],)).fetchone()
  if not published or not published[0]:raise PermissionError('Restaurant is not published')
  return None,store
 def public_menu(self,store):
  out=[]
  for item in self.menu(store):
   if not item['active']:continue
   public={k:v for k,v in item.items() if k!='recipe'}
   public['modifier_groups']=[dict(group,choices=[{k:v for k,v in choice.items() if k!='recipe'} for choice in group['choices']]) for group in item['modifier_groups']]
   out.append(public)
  return out
 def public(self,action,body):
  if action=='menu':
   table,store=self.public_context(body);return {'store':{k:store[k] for k in ('id','name','address','currency','language','timezone')},'table':{'id':table['id'],'name':table['name'],'capacity':table['capacity']} if table else None,'menu':self.public_menu(store['id']),'settings':{k:self.pos.settings(store['id'])[0][k] for k in ('tax_bps','service_bps','minor_exponent','service_taxable')},'demo':self.c.demo,'notice':'Submit an order request for staff acceptance. No payment is collected. Allergen and dietary information is merchant supplied; discuss allergies with restaurant staff.'}
  if action=='order':
   table,store=self.public_context(body)
   if body.get('accepted') is not True:raise ValueError('Confirm the order request and restaurant terms')
   request_key=key(body.get('request_key'));fingerprint=digest(body)
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');prior=self.db.execute('SELECT * FROM restaurant_orders WHERE store_id=? AND request_key=?',(store['id'],request_key)).fetchone()
    if prior:
     if prior['request_digest']!=fingerprint:raise ValueError('Request key reused for a different order')
     return {'id':prior['id'],'guest_token':json.loads(prior['payload'])['guest_token'],'state':prior['state']}
    if self.db.execute("SELECT count(*) FROM restaurant_orders WHERE store_id=? AND table_id=? AND state='requested'",(store['id'],table['id'] if table else '')).fetchone()[0]>=(5 if table else 50):raise ValueError('Table has too many requests awaiting staff acceptance')
    mode='dine_in' if table else body.get('mode','takeout')
    if not table and mode not in ('takeout','delivery'):raise ValueError('Choose takeout or delivery')
    quote=self.price_order(store['id'],dict(body,table_id=table['id'] if table else '',mode=mode,priority=0));oid=str(uuid.uuid4());token=secrets.token_urlsafe(32);quote['guest_token']=token
    self.db.execute('INSERT INTO restaurant_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,store['id'],table['id'] if table else '','guest',mode,'requested',dump(quote),1,None,request_key,fingerprint,hashlib.sha256(token.encode()).hexdigest(),time.time(),time.time()))
   return {'id':oid,'guest_token':token,'state':'requested'}
  if action in ('track','cancel'):
   row=self.db.execute('SELECT * FROM restaurant_orders WHERE id=? AND guest_hash=?',(body.get('id'),hashlib.sha256(str(body.get('guest_token','')).encode()).hexdigest())).fetchone()
   if not row:raise PermissionError('Order access denied')
   if action=='cancel':
    with self.db:
     if self.db.execute("UPDATE restaurant_orders SET state='cancelled',revision=revision+1,updated=? WHERE id=? AND state='requested'",(time.time(),row['id'])).rowcount!=1:raise ValueError('Staff has accepted this order; contact the restaurant to cancel')
    row=self.order(row['store_id'],row['id'])
   data=self.order_data(row);data.pop('ingredients',None);return data
  raise ValueError('Unknown public restaurant operation')
 def dispatch(self,actor,action,body):
  store=body.get('store_id');record,permissions=self.check(actor,store)
  if action=='state':
   tables=[dict({k:r[k] for k in ('id','name','capacity','room','x','y','state','revision')},**(json.loads(r['payload']) if permissions&{'manage','tables','reserve'} else {})) for r in self.db.execute('SELECT * FROM restaurant_tables WHERE store_id=? ORDER BY room,name',(store,))]
   orders=[self.order_data(r,private=bool(permissions&{'manage','orders','dispatch','drive'})) for r in self.db.execute("SELECT o.* FROM restaurant_orders o LEFT JOIN pos_sales s ON s.id=o.sale_id WHERE o.store_id=? AND (o.state NOT IN ('cancelled','served') OR o.updated>? OR (o.state='served' AND (s.state IS NULL OR s.state NOT IN ('paid','demo_paid','refunded','cancelled')))) ORDER BY o.created DESC LIMIT 200",(store,time.time()-86400))]
   orders.sort(key=lambda o:(-o.get('priority',0),o['created']))
   if 'drive' in permissions and not permissions&{'manage','dispatch'}:
    assigned={r[0] for r in self.db.execute('SELECT order_id FROM restaurant_delivery WHERE store_id=? AND driver=?',(store,actor['id']))};orders=[o for o in orders if o['id'] in assigned]
   elif 'manage' not in permissions and not permissions&{'kitchen','sell','dispatch'}:orders=[o for o in orders if o['owner']==actor['id'] or 'tables' in permissions]
   reservations=[dict({k:r[k] for k in ('id','table_id','start','end','guests','state','revision')},**json.loads(r['payload'])) for r in self.db.execute('SELECT * FROM restaurant_reservations WHERE store_id=? AND end>? ORDER BY start LIMIT 200',(store,time.time()-86400))] if permissions&{'manage','reserve'} else []
   deliveries=[dict(r) for r in self.db.execute('SELECT * FROM restaurant_delivery WHERE store_id=? AND (driver=? OR ?) ORDER BY updated DESC LIMIT 100',(store,actor['id'],int(bool(permissions&{'dispatch','manage'}))))] if permissions&{'dispatch','drive','manage'} else []
   for delivery in deliveries:delivery.update(json.loads(delivery.pop('payload')))
   return {'store':record,'permissions':sorted(permissions),'tables':tables,'orders':orders,'menu':self.menu(store),'ingredients':[dict(r) for r in self.db.execute('SELECT * FROM restaurant_ingredients WHERE store_id=?',(store,))] if permissions&{'stock','menu','manage'} else [],'reservations':reservations,'waitlist':[dict(r) for r in self.db.execute("SELECT * FROM restaurant_waitlist WHERE store_id=? AND state IN ('waiting','notified') ORDER BY created",(store,))] if permissions&{'reserve','manage'} else [],'deliveries':deliveries,'drivers':[dict(r) for r in self.db.execute("SELECT DISTINCT u.id,u.email FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.store_id=? AND m.tenant_id=? AND m.role='delivery_driver' AND u.active=1",(store,record['tenant_id']))] if permissions&{'manage','dispatch'} else [],'demo':self.c.demo,'notice':'QR orders await staff acceptance. Ingredient units are merchant configured. No allergen-free, dietary certification, delivery-provider, food-safety compliance or predictive timing guarantee is made.'}
  if action=='menu':
   self.check(actor,store,'menu');iid=body.get('id');product=self.db.execute('SELECT * FROM inventory WHERE id=? AND store_id=?',(iid,store)).fetchone()
   if not product:raise ValueError('Create the menu product in store inventory first')
   groups=body.get('modifier_groups',[]);recipe=body.get('recipe',[])
   if not isinstance(groups,list) or len(groups)>10 or not isinstance(recipe,list) or len(recipe)>50:raise ValueError('Modifier or recipe limit exceeded')
   normalized=[];choice_ids=set();recipe_ids=set()
   for group in groups:
    choices=[]
    for choice in group.get('choices',[]):
     cid=label(choice.get('id'),'modifier id',50)
     if cid in choice_ids:raise ValueError('Duplicate modifier id')
     extra_recipe=choice.get('recipe',[])
     if not isinstance(extra_recipe,list) or len(extra_recipe)>20:raise ValueError('Invalid modifier recipe')
     for ingredient in extra_recipe:
      if not self.db.execute('SELECT 1 FROM restaurant_ingredients WHERE id=? AND store_id=?',(ingredient.get('id'),store)).fetchone():raise ValueError('Modifier ingredient belongs to another store')
     choice_ids.add(cid);choices.append({'id':cid,'name':label(choice.get('name'),'modifier name',100),'price_minor':integer(choice.get('price_minor',0),'modifier price',0,10**9),'allergens':label(choice.get('allergens',''),'modifier allergens',500,False),'recipe':[{'id':r['id'],'quantity_milli':milli(r['quantity'])} for r in extra_recipe]})
    minimum=integer(group.get('minimum',0),'minimum',0,len(choices));maximum=integer(group.get('maximum',1),'maximum',minimum,len(choices))
    normalized.append({'name':label(group.get('name'),'group name',100),'minimum':minimum,'maximum':maximum,'choices':choices})
   for ingredient in recipe:
    if ingredient.get('id') in recipe_ids:raise ValueError('Duplicate recipe ingredient')
    recipe_ids.add(ingredient.get('id'))
    if not self.db.execute('SELECT 1 FROM restaurant_ingredients WHERE id=? AND store_id=?',(ingredient.get('id'),store)).fetchone():raise ValueError('Ingredient belongs to another store')
   data={'name':product['name'],'category':label(body.get('category','Main menu'),'category',100),'description':label(body.get('description',''),'description',2000,False),'allergens':label(body.get('allergens',''),'allergen information',1000,False),'dietary':label(body.get('dietary',''),'dietary information',500,False),'station':label(body.get('station','Kitchen'),'station',80),'active':body.get('active') is True,'tax_bps':integer(body.get('tax_bps',self.pos.settings(store)[0]['tax_bps']),'tax_bps',0,10000),'modifier_groups':normalized,'recipe':[{'id':r['id'],'quantity_milli':milli(r['quantity'])} for r in recipe],'prep_minutes':integer(body.get('prep_minutes',20),'prep_minutes',1,240)}
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.db.execute('SELECT revision FROM restaurant_menu WHERE id=?',(iid,)).fetchone();revision=row[0] if row else 0
    if revision!=body.get('revision',0):raise ValueError('Menu item changed; reload')
    self.db.execute('INSERT OR REPLACE INTO restaurant_menu VALUES(?,?,?,?)',(iid,store,dump(data),revision+1));self.c.log(actor,'restaurant_menu',iid,{'revision':revision+1})
   return {'id':iid,'revision':revision+1}
  if action=='ingredient':
   self.check(actor,store,'stock');iid=body.get('id') or str(uuid.uuid4());old=self.db.execute('SELECT * FROM restaurant_ingredients WHERE id=?',(iid,)).fetchone()
   if old and old['store_id']!=store:raise PermissionError('Cross-store ingredient edit denied')
   unit=body.get('unit','g')
   if unit not in ('g','ml','piece'):raise ValueError('Choose g, ml or piece; convert supplier quantities into this base unit')
   if old and old['unit']!=unit:raise ValueError('Unit cannot change; create a new ingredient')
   qty=milli(body.get('quantity'));cost=integer(body.get('cost_minor_per_1000',0),'cost_minor_per_1000');low=milli(body.get('low_stock',0));payload={'batch':label(body.get('batch',''),'batch',100,False),'expires':label(body.get('expires',''),'expiry',50,False)}
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');current=self.db.execute('SELECT quantity_milli FROM restaurant_ingredients WHERE id=?',(iid,)).fetchone()
    if current and body.get('previous_quantity_milli')!=current[0]:raise ValueError('Ingredient stock changed; reload before adjustment')
    self.db.execute('INSERT OR REPLACE INTO restaurant_ingredients VALUES(?,?,?,?,?,?,?,?)',(iid,store,label(body.get('name'),'ingredient',150),unit,qty,cost,low,dump(payload)));self.ingredient_move(actor,store,iid,qty-(current[0] if current else 0),'stock_adjustment',label(body.get('reason','Initial stock'),'reason',300));self.c.log(actor,'restaurant_ingredient',iid,{'quantity_milli':qty})
   return {'id':iid}
  if action=='table':
   self.check(actor,store,'tables');tid=body.get('id') or str(uuid.uuid4());old=self.db.execute('SELECT * FROM restaurant_tables WHERE id=?',(tid,)).fetchone()
   if old and old['store_id']!=store:raise PermissionError('Cross-store table edit denied')
   state=body.get('state','available')
   if state not in ('available','reserved','occupied','ordering','preparing','served','payment_pending','cleaning'):raise ValueError('Unknown table status')
   token=json.loads(old['payload'])['qr_token'] if old else secrets.token_urlsafe(32)
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.db.execute('SELECT revision FROM restaurant_tables WHERE id=?',(tid,)).fetchone();revision=row[0] if row else 0
    if revision!=body.get('revision',0):raise ValueError('Table changed; reload')
    self.db.execute('INSERT OR REPLACE INTO restaurant_tables VALUES(?,?,?,?,?,?,?,?,?,?,?)',(tid,store,label(body.get('name'),'table name',80),integer(body.get('capacity',4),'capacity',1,1000),label(body.get('room','Dining room'),'room',100),integer(body.get('x',100),'x',0,950),integer(body.get('y',100),'y',0,950),state,hashlib.sha256(token.encode()).hexdigest(),dump({'qr_token':token}),revision+1))
   return {'id':tid,'revision':revision+1}
  if action in ('quote','order-create'):
   self.check(actor,store,'orders')
   if action=='quote':return self.price_order(store,body)
   request_key=key(body.get('request_key'));fingerprint=digest(body)
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');prior=self.db.execute('SELECT * FROM restaurant_orders WHERE store_id=? AND request_key=?',(store,request_key)).fetchone()
    if prior:
     if prior['request_digest']!=fingerprint:raise ValueError('Order request key changed')
     return self.order_data(prior,True)
    quote=self.price_order(store,body)
    if body.get('quote_digest')!=quote['quote_digest']:raise ValueError('Order quote changed; review current pricing')
    oid=str(uuid.uuid4());self.db.execute('INSERT INTO restaurant_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,store,body.get('table_id',''),actor['id'],body.get('mode','dine_in'),'open',dump(quote),1,None,request_key,fingerprint,None,time.time(),time.time()));self.c.log(actor,'restaurant_order',oid,{'mode':body.get('mode','dine_in')})
    if body.get('table_id'):self.db.execute("UPDATE restaurant_tables SET state='ordering',revision=revision+1 WHERE id=?",(body['table_id'],))
   return self.order_data(self.order(store,oid),True)
  if action=='order-update':
   self.check(actor,store,'orders')
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.order(store,body.get('id'))
    if row['state']!='open' or row['revision']!=body.get('revision') or row['sale_id']:raise ValueError('Only unchanged, pre-kitchen orders can be edited')
    if row['owner']!=actor['id'] and 'manage' not in permissions:raise PermissionError('Only the order owner or a manager can revise it')
    quote=self.price_order(store,dict(body,table_id=row['table_id'],mode=row['mode']))
    if body.get('quote_digest')!=quote['quote_digest']:raise ValueError('Review the new order quote')
    self.db.execute('UPDATE restaurant_orders SET payload=?,revision=revision+1,updated=? WHERE id=?',(dump(quote),time.time(),row['id']));self.c.log(actor,'restaurant_order_revision',row['id'],{'revision':row['revision']+1})
   return {'ok':True}
  if action=='payment-quote':return self.payment_quote(actor,store,body.get('id'))
  if action=='order-status':
   target=body.get('state');cap='kitchen' if target in ('preparing','ready') else 'orders';self.check(actor,store,cap)
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.order(store,body.get('id'));data=json.loads(row['payload'])
    if row['revision']!=body.get('revision'):raise ValueError('Order changed; refresh the queue')
    if target not in TRANSITIONS[row['state']]:raise ValueError('Invalid order transition')
    if target=='cancelled':
     self.check(actor,store,'manage')
     if row['sale_id'] and self.db.execute('SELECT paid_minor-refunded_minor FROM pos_sales WHERE id=?',(row['sale_id'],)).fetchone()[0]:raise ValueError('Refund collected payments before cancellation')
     data['cancellation_reason']=label(body.get('reason'),'cancellation reason',500)
     if row['state'] in ('open','requested','queued'):
      if row['state']=='queued':
       for iid,qty in data['ingredients'].items():self.db.execute('UPDATE restaurant_ingredients SET quantity_milli=quantity_milli+? WHERE id=?',(qty,iid));self.ingredient_move(actor,store,iid,qty,'cancel_before_preparation',row['id'])
       for line in data['lines']:self.db.execute('UPDATE inventory SET quantity=quantity+? WHERE id=?',(line['quantity'],line['id']));self.pos.movement(actor,store,line['id'],line['quantity'],'restaurant_cancel',row['id'])
     else:data['food_waste_recorded']=True
     if row['sale_id']:self.db.execute("UPDATE pos_sales SET state='cancelled' WHERE id=?",(row['sale_id'],))
    if target=='queued':
     for iid,qty in data['ingredients'].items():
      if self.db.execute('UPDATE restaurant_ingredients SET quantity_milli=quantity_milli-? WHERE id=? AND quantity_milli>=?',(qty,iid,qty)).rowcount!=1:raise ValueError('Ingredient stock changed; manager must revise the order')
      self.ingredient_move(actor,store,iid,-qty,'recipe_consumption',row['id'])
     for line in data['lines']:
      if self.db.execute('UPDATE inventory SET quantity=quantity-? WHERE id=? AND quantity>=?',(line['quantity'],line['id'],line['quantity'])).rowcount!=1:raise ValueError('Menu availability changed')
      self.pos.movement(actor,store,line['id'],-line['quantity'],'restaurant_kitchen',row['id'])
    data.setdefault('timeline',{})[target]=time.time();self.db.execute('UPDATE restaurant_orders SET state=?,payload=?,revision=revision+1,updated=? WHERE id=?',(target,dump(data),time.time(),row['id']));self.c.log(actor,'restaurant_order_'+target,row['id'],{'store_id':store})
    if row['table_id'] and target!='cancelled':self.db.execute('UPDATE restaurant_tables SET state=?,revision=revision+1 WHERE id=?',({'queued':'preparing','preparing':'preparing','ready':'preparing','served':'payment_pending','open':'ordering'}.get(target,'occupied'),row['table_id']))
    self.release_if_paid(self.order(store,row['id']))
   return {'ok':True}
  if action=='table-qr':
   self.check(actor,store,'tables');table=self.table(store,body.get('id'))
   if not table:raise ValueError('Table not found')
   from pos_exports import qr_url,qr_svg
   return qr_svg(qr_url(self.c,'/menu.html',{'table':json.loads(table['payload'])['qr_token']}))
  if action=='table-transfer':
   self.check(actor,store,'tables')
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.order(store,body.get('id'));target=self.table(store,body.get('table_id'));data=json.loads(row['payload'])
    if row['revision']!=body.get('revision') or row['state']=='cancelled' or not target or target['capacity']<data['guests'] or target['state'] not in ('available','reserved'):raise ValueError('Table transfer unavailable or order changed')
    self.db.execute('UPDATE restaurant_orders SET table_id=?,revision=revision+1,updated=? WHERE id=?',(target['id'],time.time(),row['id']));self.db.execute("UPDATE restaurant_tables SET state='occupied',revision=revision+1 WHERE id=?",(target['id'],))
    if row['table_id'] and not self.db.execute("SELECT 1 FROM restaurant_orders WHERE table_id=? AND id<>? AND state NOT IN ('cancelled','served')",(row['table_id'],row['id'])).fetchone():self.db.execute("UPDATE restaurant_tables SET state='cleaning',revision=revision+1 WHERE id=?",(row['table_id'],))
   return {'ok':True}
  if action=='reservation':
   self.check(actor,store,'reserve');rid=body.get('id') or str(uuid.uuid4());table=self.table(store,body.get('table_id'));start=integer(body.get('start'),'start');end=integer(body.get('end'),'end');guests=integer(body.get('guests'),'guests',1,1000);state=body.get('state','confirmed')
   if not table or guests>table['capacity'] or not 0<end-start<=86400 or state not in ('confirmed','seated','cancelled','no_show','completed'):raise ValueError('Invalid reservation time, table, capacity or status')
   payload={'name':label(body.get('name'),'reservation name',150),'contact':label(body.get('contact',''),'contact',200,False),'notes':label(body.get('notes',''),'notes',500,False)}
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');old=self.db.execute('SELECT * FROM restaurant_reservations WHERE id=?',(rid,)).fetchone()
    if old and (old['store_id']!=store or old['revision']!=body.get('revision')):raise PermissionError('Reservation changed or belongs to another store')
    if state in ('confirmed','seated') and self.db.execute("SELECT 1 FROM restaurant_reservations WHERE table_id=? AND id<>? AND state IN ('confirmed','seated') AND start<? AND end>?",(table['id'],rid,end,start)).fetchone():raise ValueError('Table already reserved in this time slot')
    self.db.execute('INSERT OR REPLACE INTO restaurant_reservations VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,store,table['id'],start,end,guests,state,dump(payload),actor['id'],old['revision']+1 if old else 1))
   return {'id':rid}
  if action=='waitlist':
   self.check(actor,store,'reserve');wid=body.get('id') or str(uuid.uuid4());old=self.db.execute('SELECT store_id FROM restaurant_waitlist WHERE id=?',(wid,)).fetchone();state=body.get('state','waiting')
   if old and old[0]!=store:raise PermissionError('Waitlist belongs to another store')
   if state not in ('waiting','notified','seated','left'):raise ValueError('Unknown waitlist state')
   with self.db:self.db.execute('INSERT OR REPLACE INTO restaurant_waitlist VALUES(?,?,?,?,?,?,?)',(wid,store,label(body.get('name'),'guest name',150),integer(body.get('guests',1),'guests',1,1000),state,integer(body.get('estimate_minutes',0),'estimate_minutes',0,300),time.time()))
   return {'id':wid,'notice':'Status changes are in-platform only; no SMS/email notification is sent'}
  if action in ('delivery-assign','delivery-status'):
   self.check(actor,store,'dispatch' if action=='delivery-assign' else 'drive')
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');order=self.order(store,body.get('id'))
    if order['mode']!='delivery' or order['state']=='cancelled':raise ValueError('Order is not an active delivery')
    existing=self.db.execute('SELECT * FROM restaurant_delivery WHERE order_id=?',(order['id'],)).fetchone()
    if action=='delivery-assign':
     driver=self.db.execute("SELECT u.id FROM users u JOIN memberships m ON m.user_id=u.id WHERE u.id=? AND u.active=1 AND m.tenant_id=? AND m.store_id=? AND m.role='delivery_driver'",(body.get('driver'),record['tenant_id'],store)).fetchone()
     if not driver:raise ValueError('Choose an active driver assigned to this restaurant')
     if existing and existing['state'] in ('picked_up','delivered'):raise ValueError('Delivery cannot be reassigned after pickup')
     data={'assignment_note':label(body.get('note',''),'assignment note',500,False)};self.db.execute('INSERT OR REPLACE INTO restaurant_delivery VALUES(?,?,?,?,?,?)',(order['id'],store,driver['id'],'assigned',dump(data),time.time()))
    else:
     if not existing or existing['driver']!=actor['id']:raise PermissionError('Only the assigned driver can update this delivery')
     allowed={'assigned':('picked_up','failed'),'picked_up':('delivered','failed')};state=body.get('state')
     if state not in allowed.get(existing['state'],()):raise ValueError('Invalid delivery transition')
     if state=='picked_up' and order['state']!='ready':raise ValueError('Kitchen must mark this order ready before pickup')
     data=json.loads(existing['payload']);data['proof_note']=label(body.get('proof_note'),'delivery proof / failure note',1000)
     self.db.execute('UPDATE restaurant_delivery SET state=?,payload=?,updated=? WHERE order_id=?',(state,dump(data),time.time(),order['id']))
     if state=='delivered':self.db.execute("UPDATE restaurant_orders SET state='served',revision=revision+1,updated=? WHERE id=?",(time.time(),order['id']))
    self.c.log(actor,action,order['id'],{'store':store})
   return {'ok':True}
  if action=='safety-log':
   self.check(actor,store,'kitchen');kind=body.get('kind')
   if kind not in ('temperature','cleaning','maintenance','incident','waste','inspection'):raise ValueError('Unknown operational log type')
   data={'note':label(body.get('note'),'log note',2000),'reading':label(str(body.get('reading','')),'reading',100,False),'unit':label(body.get('unit',''),'unit',50,False)}
   with self.db:self.db.execute('INSERT INTO restaurant_safety VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),store,kind,dump(data),actor['id'],time.time()));self.c.log(actor,'restaurant_safety',store,{'kind':kind})
   return {'ok':True,'notice':'Manual operational record, not automated monitoring or compliance certification'}
  raise ValueError('Unknown restaurant operation')
