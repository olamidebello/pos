"""Store-scoped register transactions. Integer minor units; no card data or fake gateway verification."""
import csv,hashlib,io,json,secrets,time,uuid
from decimal import Decimal,ROUND_HALF_UP

CAPS={
 'tenant_admin':{'manage','sell','shift','refund','stock','report','verify','menu','tables','orders','kitchen','reserve','dispatch','drive'},
 'store_manager':{'manage','sell','shift','refund','stock','report','verify','menu','tables','orders','kitchen','reserve','dispatch','drive'},
 'cashier':{'sell','shift','verify'},'exit_verifier':{'verify'},'auditor':{'report'},'accountant':{'report'},
 'inventory_manager':{'stock'},'waiter':{'tables','orders'},'host':{'tables','reserve'},
 'chef':{'kitchen'},'kitchen_staff':{'kitchen'},'delivery_dispatcher':{'dispatch'},'delivery_driver':{'drive'}
}


def integer(value,name,low=0,high=10**12):
 if type(value) is not int or not low<=value<=high:raise ValueError(name+' must be an integer between '+str(low)+' and '+str(high))
 return value

def label(value,name,maximum=200,required=True):
 if not isinstance(value,str) or len(value)>maximum or '\x00' in value or (required and not value.strip()):raise ValueError('Invalid '+name)
 return value.strip()

def key(value):
 try:return str(uuid.UUID(str(value)))
 except (ValueError,TypeError):raise ValueError('A UUID request_key is required') from None

def dump(value):return json.dumps(value,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(dump(value).encode()).hexdigest()
def rounded(numerator,denominator=10000):return (numerator+denominator//2)//denominator


def initialize(db):
 db.executescript('''
 CREATE TABLE IF NOT EXISTS pos_settings(store_id TEXT PRIMARY KEY REFERENCES stores(id),revision INTEGER NOT NULL,payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS pos_products(id TEXT PRIMARY KEY REFERENCES inventory(id),store_id TEXT NOT NULL REFERENCES stores(id),payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS pos_customers(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS pos_prices(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),inventory_id TEXT NOT NULL REFERENCES inventory(id),customer_id TEXT NOT NULL DEFAULT '',minimum INTEGER NOT NULL,price_minor INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS pos_coupons(store_id TEXT NOT NULL,code TEXT NOT NULL,payload TEXT NOT NULL,uses INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(store_id,code));
 CREATE TABLE IF NOT EXISTS pos_shifts(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),terminal TEXT NOT NULL,owner TEXT NOT NULL REFERENCES users(id),state TEXT NOT NULL,opening INTEGER NOT NULL,payload TEXT NOT NULL,opened REAL NOT NULL,closed REAL);
 CREATE UNIQUE INDEX IF NOT EXISTS pos_one_open_terminal ON pos_shifts(store_id,terminal) WHERE state='open';
 CREATE TABLE IF NOT EXISTS pos_carts(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),owner TEXT NOT NULL REFERENCES users(id),state TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS pos_sales(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),owner TEXT NOT NULL REFERENCES users(id),cart_id TEXT UNIQUE,request_key TEXT NOT NULL,request_digest TEXT NOT NULL,payload TEXT NOT NULL,total_minor INTEGER NOT NULL,paid_minor INTEGER NOT NULL DEFAULT 0,refunded_minor INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,created REAL NOT NULL,exit_verified REAL,UNIQUE(store_id,request_key));
 CREATE TABLE IF NOT EXISTS pos_tenders(id TEXT PRIMARY KEY,sale_id TEXT NOT NULL REFERENCES pos_sales(id),shift_id TEXT NOT NULL REFERENCES pos_shifts(id),request_key TEXT NOT NULL,method TEXT NOT NULL,amount_minor INTEGER NOT NULL,received_minor INTEGER NOT NULL,reference TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL,UNIQUE(sale_id,request_key));
 CREATE TABLE IF NOT EXISTS pos_refunds(id TEXT PRIMARY KEY,sale_id TEXT NOT NULL REFERENCES pos_sales(id),shift_id TEXT NOT NULL REFERENCES pos_shifts(id),request_key TEXT NOT NULL,request_digest TEXT NOT NULL,amount_minor INTEGER NOT NULL,method TEXT NOT NULL,payload TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL,UNIQUE(sale_id,request_key));
 CREATE TABLE IF NOT EXISTS pos_movements(id TEXT PRIMARY KEY,store_id TEXT NOT NULL,inventory_id TEXT NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,reference TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL);
 CREATE INDEX IF NOT EXISTS pos_sales_store_time ON pos_sales(store_id,created);
 CREATE INDEX IF NOT EXISTS pos_movement_store_time ON pos_movements(store_id,created);
 ''')


class POS:
 def __init__(self,control):self.c=control;self.db=control.db
 def context(self,actor):
  stores=[]
  for row in self.db.execute("SELECT s.* FROM stores s JOIN tenants t ON t.id=s.tenant_id WHERE s.status='active' AND t.status='active' ORDER BY s.name"):
   try:self.c.authorize(actor,row['tenant_id'],row['id'])
   except PermissionError:continue
   stores.append(dict(row,effective_features=self.c.effective_features(row)))
  return {'me':{k:actor[k] for k in ('id','email','super')},'stores':stores}
 def settings(self,store):
  row=self.db.execute('SELECT * FROM pos_settings WHERE store_id=?',(store,)).fetchone()
  from gateways import EXPONENTS
  currency=self.db.execute('SELECT currency FROM stores WHERE id=?',(store,)).fetchone()
  defaults={'tax_bps':0,'service_bps':0,'service_taxable':False,'allow_partial':False,'minor_exponent':EXPONENTS.get(currency[0] if currency else 'USD',2),'public_ordering':False,'receipt_note':'Keep this receipt for returns.','category':'retail'}
  return dict(defaults,**(json.loads(row['payload']) if row else {})),row['revision'] if row else 0
 def check(self,actor,store,cap=None):
  row=self.db.execute('SELECT s.*,t.status AS tenant_status FROM stores s JOIN tenants t ON t.id=s.tenant_id WHERE s.id=?',(store,)).fetchone()
  if not row or row['status']!='active' or row['tenant_status']!='active':raise PermissionError('Active store and tenant required')
  self.c.authorize(actor,row['tenant_id'],store)
  if actor.get('api_store'):raise PermissionError('Register access requires a staff login')
  if not self.c.effective_features(row).get('pos'):raise PermissionError('POS must be enabled by the super administrator')
  memberships=self.db.execute('SELECT role FROM memberships WHERE user_id=? AND tenant_id=? AND (store_id=? OR store_id=\'\')',(actor['id'],row['tenant_id'],store)).fetchall()
  permissions=set().union(*(CAPS.get(r['role'],set()) for r in memberships))
  if actor['super']:permissions=set(CAPS['tenant_admin'])
  if cap and cap not in permissions:raise PermissionError('Permission required: '+cap)
  return dict(row),permissions
 def shift(self,actor,store,sid):
  row=self.db.execute("SELECT * FROM pos_shifts WHERE id=? AND store_id=? AND state='open'",(sid,store)).fetchone()
  if not row or row['owner']!=actor['id']:raise PermissionError('Open your own register shift before recording a payment')
  return row
 def movement(self,actor,store,item,delta,reason,reference):
  self.db.execute('INSERT INTO pos_movements VALUES(?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),store,item,delta,reason,reference,actor['id'],time.time()))
 def products(self,store):
  rows=self.db.execute('SELECT i.*,p.payload FROM inventory i LEFT JOIN pos_products p ON p.id=i.id WHERE i.store_id=? ORDER BY i.name',(store,)).fetchall()
  return [dict({k:r[k] for k in r.keys() if k!='payload'},**(json.loads(r['payload']) if r['payload'] else {})) for r in rows]
 def quote(self,actor,store,body):
  record,permissions=self.check(actor,store,'sell');settings,revision=self.settings(store)
  from gateways import EXPONENTS
  if record['currency'] not in EXPONENTS and revision==0:raise ValueError('Configure the currency decimal places explicitly before checkout in this currency')
  customer=body.get('customer_id','')
  if customer and not self.db.execute('SELECT 1 FROM pos_customers WHERE id=? AND store_id=?',(customer,store)).fetchone():raise ValueError('Customer belongs to another store or is unavailable')
  items=body.get('items');lines=[];seen=set()
  if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('Use 1–100 product lines')
  for item in items:
   if not isinstance(item,dict):raise ValueError('Invalid cart line')
   product=self.db.execute('SELECT i.*,p.payload FROM inventory i LEFT JOIN pos_products p ON p.id=i.id WHERE i.id=? AND i.store_id=?',(item.get('id'),store)).fetchone()
   if not product or product['id'] in seen:raise ValueError('Unknown, duplicate or cross-store product')
   seen.add(product['id']);quantity=integer(item.get('quantity'),'quantity',1,10000);meta=json.loads(product['payload']) if product['payload'] else {}
   if meta.get('active') is False:raise ValueError('Product is unavailable')
   if quantity>product['quantity']:raise ValueError('Insufficient stock: '+product['name'])
   price=product['price_minor']
   rule=self.db.execute("SELECT * FROM pos_prices WHERE store_id=? AND inventory_id=? AND minimum<=? AND customer_id IN ('',?) ORDER BY CASE WHEN customer_id='' THEN 1 ELSE 0 END,minimum DESC LIMIT 1",(store,product['id'],quantity,customer)).fetchone()
   if rule:price=rule['price_minor']
   lines.append({'line':len(lines),'id':product['id'],'barcode':product['barcode'],'name':product['name'],'quantity':quantity,'price_minor':price,'gross_minor':quantity*price,'tax_bps':meta.get('tax_bps',settings['tax_bps']),'cost_minor':meta.get('cost_minor',0),'returned':0})
  subtotal=sum(x['gross_minor'] for x in lines);discount=0;coupon=str(body.get('coupon','')).strip().upper()
  if coupon:
   row=self.db.execute('SELECT * FROM pos_coupons WHERE store_id=? AND code=?',(store,coupon)).fetchone()
   if not row:raise ValueError('Unknown coupon')
   policy=json.loads(row['payload'])
   if not policy['active'] or not policy['starts']<=time.time()<=policy['ends'] or row['uses']>=policy['maximum_uses']:raise ValueError('Coupon is inactive, expired or exhausted')
   discount=min(subtotal,rounded(subtotal*policy['value']) if policy['kind']=='percent' else policy['value'])
  manual=body.get('discount_minor',0)
  if manual:
   if 'manage' not in permissions:raise PermissionError('Manager required for a manual discount')
   discount+=integer(manual,'discount_minor',0,subtotal-discount)
  allocated=0;cumulative=0
  for index,line in enumerate(lines):
   cumulative+=line['gross_minor'];line['discount_minor']=(discount*cumulative//subtotal if subtotal else 0)-allocated
   allocated+=line['discount_minor'];line['net_minor']=line['gross_minor']-line['discount_minor'];line['tax_minor']=rounded(line['net_minor']*line['tax_bps']);line['total_minor']=line['net_minor']+line['tax_minor']
  service=rounded((subtotal-discount)*settings['service_bps']);service_tax=rounded(service*settings['tax_bps']) if settings['service_taxable'] else 0
  tip=integer(body.get('tip_minor',0),'tip_minor',0,10**9)
  if tip and body.get('tip_confirmed') is not True:raise ValueError('Customer authorization is required before recording a tip')
  result={'store_id':store,'currency':record['currency'],'minor_exponent':settings['minor_exponent'],'settings_revision':revision,'customer_id':customer,'lines':lines,'subtotal_minor':subtotal,'discount_minor':discount,'tax_minor':sum(l['tax_minor'] for l in lines)+service_tax,'service_minor':service,'service_tax_minor':service_tax,'tip_minor':tip,'total_minor':sum(l['total_minor'] for l in lines)+service+service_tax+tip,'coupon':coupon,'is_demo':self.c.demo}
  if result['total_minor']>10**12:raise ValueError('Sale exceeds the amount limit')
  result['quote_digest']=digest(result);return result
 def sale(self,actor,sale_id,cap='sell'):
  row=self.db.execute('SELECT * FROM pos_sales WHERE id=?',(sale_id,)).fetchone()
  if not row:raise ValueError('Sale not found')
  self.check(actor,row['store_id'],cap);return row
 def sale_public(self,row,include_token=False):
  payload=json.loads(row['payload']);payload.pop('receipt_token',None)
  result=dict(payload,**{k:row[k] for k in ('id','store_id','state','total_minor','paid_minor','refunded_minor','created','exit_verified')})
  result['balance_minor']=max(0,row['total_minor']-row['paid_minor']);result['payments']=[dict(r) for r in self.db.execute('SELECT method,amount_minor,received_minor,reference,created FROM pos_tenders WHERE sale_id=? ORDER BY created',(row['id'],))]
  if include_token:result['receipt_token']=json.loads(row['payload'])['receipt_token']
  return result
 def add_tenders(self,actor,row,shift_id,payments,request_key):
  self.shift(actor,row['store_id'],shift_id)
  if not isinstance(payments,list) or not 1<=len(payments)<=8:raise ValueError('Use 1–8 payment parts')
  remaining=row['total_minor']-row['paid_minor'];paid=0
  _,permissions=self.check(actor,row['store_id'],'sell')
  for index,payment in enumerate(payments):
   method=payment.get('method');amount=integer(payment.get('amount_minor'),'payment amount',1,remaining-paid)
   if method not in ('cash','external_terminal','bank_transfer','demo'):raise ValueError('Unsupported register tender')
   if method=='demo' and not self.c.demo:raise PermissionError('Simulated payments are available only inside isolated demos')
   reference=label(payment.get('reference',''),'reference',200,False)
   if method in ('external_terminal','bank_transfer'):
    if 'manage' not in permissions or payment.get('confirmed_external') is not True or not reference:raise PermissionError('Manager confirmation and external payment reference required; this records an external settlement, not gateway verification')
   received=integer(payment.get('received_minor',amount),'received_minor',amount,10**12) if method=='cash' else amount
   self.db.execute('INSERT INTO pos_tenders VALUES(?,?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),row['id'],shift_id,request_key+':'+str(index),method,amount,received,reference,actor['id'],time.time()));paid+=amount
  self.db.execute('UPDATE pos_sales SET paid_minor=paid_minor+?,state=CASE WHEN paid_minor+?>=total_minor THEN ? ELSE ? END WHERE id=?',(paid,paid,'demo_paid' if self.c.demo else 'paid','partial',row['id']))
 def checkout(self,actor,body):
  store=body.get('store_id');self.check(actor,store,'sell');request_key=key(body.get('request_key'));fingerprint=digest(body)
  with self.db:
   self.db.execute('BEGIN IMMEDIATE')
   prior=self.db.execute('SELECT * FROM pos_sales WHERE store_id=? AND request_key=?',(store,request_key)).fetchone()
   if prior:
    if prior['request_digest']!=fingerprint:raise ValueError('Request key belongs to a different checkout')
    return self.sale_public(prior,True)
   restaurant_order=body.get('restaurant_order_id')
   if restaurant_order:
    from restaurant import Restaurant
    quote=Restaurant(self.c).payment_quote(actor,store,restaurant_order)
   else:quote=self.quote(actor,store,body)
   if body.get('quote_digest')!=quote['quote_digest']:raise ValueError('Cart prices or settings changed; review a fresh quote')
   self.shift(actor,store,body.get('shift_id'));settings,_=self.settings(store);payments=body.get('payments',[])
   payment_total=sum(integer(p.get('amount_minor'),'payment amount',1) for p in payments)
   if payment_total<quote['total_minor'] and (not settings['allow_partial'] or not quote.get('customer_id')):raise ValueError('Partial payment requires store permission and an identified customer')
   if payment_total>quote['total_minor']:raise ValueError('Applied payments cannot exceed the bill; use received_minor for cash tendered')
   cart=None
   if body.get('cart_id'):
    cart=self.db.execute('SELECT * FROM pos_carts WHERE id=? AND store_id=? AND owner=?',(body['cart_id'],store,actor['id'])).fetchone()
    if not cart or cart['state'] not in ('active','held') or cart['revision']!=body.get('cart_revision'):raise ValueError('Saved cart changed or was already checked out')
   sid=str(uuid.uuid4());snapshot=dict(quote,receipt_token=secrets.token_urlsafe(32),seller={k:self.check(actor,store)[0][k] for k in ('name','address','country')},receipt_note=settings['receipt_note'],restaurant_order_id=restaurant_order or '')
   self.db.execute('INSERT INTO pos_sales(id,store_id,owner,cart_id,request_key,request_digest,payload,total_minor,state,created) VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,store,actor['id'],cart['id'] if cart else None,request_key,fingerprint,dump(snapshot),quote['total_minor'],'unpaid',time.time()))
   if restaurant_order:
    from restaurant import Restaurant
    Restaurant(self.c).bind_sale(restaurant_order,sid)
   else:
    for line in quote['lines']:
     if self.db.execute('UPDATE inventory SET quantity=quantity-? WHERE id=? AND store_id=? AND quantity>=?',(line['quantity'],line['id'],store,line['quantity'])).rowcount!=1:raise ValueError('Stock changed; refresh the cart')
     self.movement(actor,store,line['id'],-line['quantity'],'sale',sid)
   if quote.get('coupon'):
    row=self.db.execute('SELECT payload FROM pos_coupons WHERE store_id=? AND code=?',(store,quote['coupon'])).fetchone();maximum=json.loads(row[0])['maximum_uses']
    if self.db.execute('UPDATE pos_coupons SET uses=uses+1 WHERE store_id=? AND code=? AND uses<?',(store,quote['coupon'],maximum)).rowcount!=1:raise ValueError('Coupon was exhausted')
   row=self.db.execute('SELECT * FROM pos_sales WHERE id=?',(sid,)).fetchone()
   if quote['total_minor']==0:self.db.execute('UPDATE pos_sales SET state=? WHERE id=?',('demo_paid' if self.c.demo else 'paid',sid))
   else:self.add_tenders(actor,row,body['shift_id'],payments,request_key)
   if cart:self.db.execute("UPDATE pos_carts SET state='checked_out',revision=revision+1,updated=? WHERE id=?",(time.time(),cart['id']))
   if restaurant_order:
    from restaurant import Restaurant
    Restaurant(self.c).release_if_paid(Restaurant(self.c).order(store,restaurant_order))
   self.c.log(actor,'pos_checkout',sid,{'store':store,'total_minor':quote['total_minor'],'demo':self.c.demo})
   return self.sale_public(self.db.execute('SELECT * FROM pos_sales WHERE id=?',(sid,)).fetchone(),True)
 def dispatch(self,actor,action,body):
  store=body.get('store_id');record,permissions=self.check(actor,store)
  if action=='state':
   settings,revision=self.settings(store)
   return {'store':record,'permissions':sorted(permissions),'settings':settings,'settings_revision':revision,'products':self.products(store),'customers':[dict(id=r['id'],**json.loads(r['payload'])) for r in self.db.execute('SELECT * FROM pos_customers WHERE store_id=? ORDER BY id LIMIT 500',(store,))] if permissions&{'manage','sell'} else [],'shifts':[dict(r) for r in self.db.execute("SELECT id,terminal,owner,opening,opened,state FROM pos_shifts WHERE store_id=? AND state='open' AND (owner=? OR ?)",(store,actor['id'],int('manage' in permissions)))],'carts':[dict(id=r['id'],state=r['state'],revision=r['revision'],**json.loads(r['payload'])) for r in self.db.execute("SELECT * FROM pos_carts WHERE store_id=? AND owner=? AND state IN ('active','held') ORDER BY updated DESC LIMIT 50",(store,actor['id']))],'sales':[self.sale_public(r) for r in self.db.execute('SELECT * FROM pos_sales WHERE store_id=? AND (owner=? OR ?) ORDER BY created DESC LIMIT 100',(store,actor['id'],int(bool(permissions&{'manage','report'}))))] if permissions&{'sell','manage','report'} else [],'pricing_rules':[dict(r) for r in self.db.execute('SELECT * FROM pos_prices WHERE store_id=?',(store,))] if 'manage' in permissions else [],'coupons':[dict(code=r['code'],uses=r['uses'],**json.loads(r['payload'])) for r in self.db.execute('SELECT * FROM pos_coupons WHERE store_id=?',(store,))] if 'manage' in permissions else [],'demo':self.c.demo,'notice':'Tax and service-charge rates are merchant configured, not jurisdictional tax advice. External tenders are manager-attested records, not automatic gateway verification. No payment card data is accepted.'}
  if action=='quote':return self.quote(actor,store,body)
  if action=='checkout':return self.checkout(actor,body)
  if action=='settings':
   self.check(actor,store,'manage');current,revision=self.settings(store)
   if body.get('revision')!=revision:raise ValueError('Settings changed; reload')
   config={'tax_bps':integer(body.get('tax_bps',0),'tax_bps',0,10000),'service_bps':integer(body.get('service_bps',0),'service_bps',0,10000),'service_taxable':body.get('service_taxable') is True,'allow_partial':body.get('allow_partial') is True,'minor_exponent':integer(body.get('minor_exponent',2),'minor_exponent',0,4),'receipt_note':label(body.get('receipt_note',''),'receipt note',500,False),'category':body.get('category','retail'),'public_ordering':body.get('public_ordering') is True}
   from gateways import EXPONENTS
   if record['currency'] in EXPONENTS and config['minor_exponent']!=EXPONENTS[record['currency']]:raise ValueError('Currency decimal places must match the supported currency definition')
   if config['category'] not in ('retail','restaurant'):raise ValueError('Choose retail or restaurant')
   if self.db.execute('SELECT 1 FROM pos_sales WHERE store_id=?',(store,)).fetchone() and config['minor_exponent']!=current['minor_exponent']:raise ValueError('Currency exponent cannot change after a sale')
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');latest=self.settings(store)[1]
    if latest!=revision:raise ValueError('Settings changed; reload')
    self.db.execute('INSERT OR REPLACE INTO pos_settings VALUES(?,?,?)',(store,revision+1,dump(config)));self.c.log(actor,'pos_settings',store,config)
   return {'ok':True}
  if action=='shift-open':
   self.check(actor,store,'shift');terminal=label(body.get('terminal'),'terminal',100);opening=integer(body.get('opening_minor',0),'opening_minor');sid=str(uuid.uuid4())
   with self.db:self.db.execute('INSERT INTO pos_shifts VALUES(?,?,?,?,?,?,?,?,?)',(sid,store,terminal,actor['id'],'open',opening,'{}',time.time(),None));self.c.log(actor,'shift_open',sid,{'opening_minor':opening,'terminal':terminal})
   return {'id':sid}
  if action=='shift-close':
   self.check(actor,store,'shift');sid=body.get('id')
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.shift(actor,store,sid);actual=integer(body.get('actual_minor'),'actual_minor');cash=self.db.execute("SELECT coalesce(sum(amount_minor),0) FROM pos_tenders WHERE shift_id=? AND method='cash'",(sid,)).fetchone()[0];refund=self.db.execute("SELECT coalesce(sum(amount_minor),0) FROM pos_refunds WHERE shift_id=? AND method='cash'",(sid,)).fetchone()[0];expected=row['opening']+cash-refund;result={'expected_minor':expected,'actual_minor':actual,'difference_minor':actual-expected,'note':label(body.get('note',''),'note',500,False)};self.db.execute("UPDATE pos_shifts SET state='closed',payload=?,closed=? WHERE id=?",(dump(result),time.time(),sid));self.c.log(actor,'shift_close',sid,result)
   return result
  if action=='cart-save':
   self.check(actor,store,'sell');self.quote(actor,store,body);cid=body.get('id') or str(uuid.uuid4());state=body.get('state','held')
   if state not in ('active','held'):raise ValueError('Choose active or held cart')
   payload={k:body.get(k) for k in ('items','customer_id','coupon','discount_minor','tip_minor','tip_confirmed') if k in body};payload['name']=label(body.get('name','Saved cart'),'cart name',100)
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');old=self.db.execute('SELECT * FROM pos_carts WHERE id=?',(cid,)).fetchone()
    if old and (old['store_id']!=store or old['owner']!=actor['id'] or old['state']=='checked_out' or old['revision']!=body.get('revision')):raise PermissionError('Cart changed or belongs to another cashier')
    revision=old['revision']+1 if old else 1;self.db.execute('INSERT OR REPLACE INTO pos_carts VALUES(?,?,?,?,?,?,?)',(cid,store,actor['id'],state,dump(payload),revision,time.time()))
   return {'id':cid,'revision':revision}
  if action=='customer':
   self.check(actor,store,'sell');cid=body.get('id') or str(uuid.uuid4());old=self.db.execute('SELECT store_id FROM pos_customers WHERE id=?',(cid,)).fetchone()
   if old and old['store_id']!=store:raise PermissionError('Cross-store customer edit denied')
   data={k:label(body.get(k,''),k,200,k=='name') for k in ('name','email','phone')}
   with self.db:self.db.execute('INSERT OR REPLACE INTO pos_customers VALUES(?,?,?)',(cid,store,dump(data)));self.c.log(actor,'pos_customer',cid,{'store_id':store})
   return {'id':cid}
  if action=='product-create':
   self.check(actor,store,'stock');iid=str(uuid.uuid4());quantity=integer(body.get('quantity',0),'quantity',0,10**9)
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');self.db.execute('INSERT INTO inventory VALUES(?,?,?,?,?,?)',(iid,store,label(body.get('barcode'),'barcode',200),label(body.get('name'),'product name',200),quantity,integer(body.get('price_minor'),'price_minor',0,10**10)));self.movement(actor,store,iid,quantity,'initial_stock',iid);self.c.log(actor,'pos_inventory_create',iid,{'store_id':store,'quantity':quantity})
   return {'id':iid}
  if action in ('product','price','coupon','stock-adjust','stock-transfer'):
   self.check(actor,store,'stock' if action in ('product','stock-adjust','stock-transfer') else 'manage')
   if action=='product':
    row=self.db.execute('SELECT id FROM inventory WHERE id=? AND store_id=?',(body.get('id'),store)).fetchone()
    if not row:raise ValueError('Create inventory product in administration first')
    data={k:label(body.get(k,''),k,2000,False) for k in ('sku','description','category','variant','unit','batch','expires','serial','image_url')};data.update(active=body.get('active',True) is True,tax_bps=integer(body.get('tax_bps',self.settings(store)[0]['tax_bps']),'tax_bps',0,10000),cost_minor=integer(body.get('cost_minor',0),'cost_minor'),low_stock=integer(body.get('low_stock',5),'low_stock',0,10**9))
    if data['image_url'] and not data['image_url'].startswith('https://'):raise ValueError('Product image must use HTTPS')
    with self.db:
     self.db.execute('BEGIN IMMEDIATE')
     if 'price_minor' in body:
      price=integer(body['price_minor'],'price_minor',0,10**10)
      if self.db.execute('UPDATE inventory SET price_minor=? WHERE id=? AND store_id=? AND price_minor=?',(price,row['id'],store,body.get('previous_price_minor'))).rowcount!=1:raise ValueError('Retail price changed; reload before editing')
     self.db.execute('INSERT OR REPLACE INTO pos_products VALUES(?,?,?)',(row['id'],store,dump(data)));self.c.log(actor,'pos_product',row['id'],data)
    return {'ok':True}
   if action=='price':
    if not self.db.execute('SELECT 1 FROM inventory WHERE id=? AND store_id=?',(body.get('inventory_id'),store)).fetchone():raise ValueError('Product not found in store')
    customer=body.get('customer_id','')
    if customer and not self.db.execute('SELECT 1 FROM pos_customers WHERE id=? AND store_id=?',(customer,store)).fetchone():raise ValueError('Customer not found in store')
    pid=body.get('id') or str(uuid.uuid4());old=self.db.execute('SELECT store_id FROM pos_prices WHERE id=?',(pid,)).fetchone()
    if old and old[0]!=store:raise PermissionError('Price belongs to another store')
    with self.db:
     if body.get('remove') is True:self.db.execute('DELETE FROM pos_prices WHERE id=? AND store_id=?',(pid,store))
     else:self.db.execute('INSERT OR REPLACE INTO pos_prices VALUES(?,?,?,?,?,?)',(pid,store,body['inventory_id'],customer,integer(body.get('minimum',1),'minimum',1,10000),integer(body.get('price_minor'),'price_minor')))
     self.c.log(actor,'pos_price_rule',pid,{'store_id':store,'removed':body.get('remove') is True})
    return {'id':pid}
   if action=='coupon':
    code=label(body.get('code'),'coupon',40).upper();kind=body.get('kind')
    if kind not in ('percent','fixed'):raise ValueError('Choose percent or fixed discount')
    value=integer(body.get('value'),'value',0,10000 if kind=='percent' else 10**12);policy={'kind':kind,'value':value,'active':body.get('active') is True,'starts':integer(body.get('starts',0),'starts'),'ends':integer(body.get('ends',4102444800),'ends'),'maximum_uses':integer(body.get('maximum_uses',1000),'maximum_uses',1,10**9)}
    with self.db:self.db.execute('INSERT INTO pos_coupons VALUES(?,?,?,0) ON CONFLICT(store_id,code) DO UPDATE SET payload=excluded.payload',(store,code,dump(policy)));self.c.log(actor,'pos_coupon',code,policy)
    return {'ok':True}
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');item=self.db.execute('SELECT * FROM inventory WHERE id=? AND store_id=?',(body.get('id'),store)).fetchone()
    if not item:raise ValueError('Product not found')
    reason=label(body.get('reason'),'stock reason',300);quantity=integer(body.get('quantity'),'quantity',1,10**9)
    if action=='stock-transfer':
     target=body.get('target_store');target_record,_=self.check(actor,target,'stock')
     if target==store or target_record['tenant_id']!=record['tenant_id'] or target_record['currency']!=record['currency']:raise PermissionError('Transfers require different authorized stores in the same tenant and currency')
     destination=self.db.execute('SELECT * FROM inventory WHERE store_id=? AND barcode=?',(target,item['barcode'])).fetchone()
     if not destination:raise ValueError('Create the matching barcode at the destination store first')
     if self.db.execute('UPDATE inventory SET quantity=quantity-? WHERE id=? AND quantity>=?',(quantity,item['id'],quantity)).rowcount!=1:raise ValueError('Insufficient stock')
     self.db.execute('UPDATE inventory SET quantity=quantity+? WHERE id=?',(quantity,destination['id']));ref=str(uuid.uuid4());self.movement(actor,store,item['id'],-quantity,'transfer_out: '+reason,ref);self.movement(actor,target,destination['id'],quantity,'transfer_in: '+reason,ref)
    else:
     direction=body.get('direction')
     if direction not in ('add','remove'):raise ValueError('Choose add or remove stock')
     delta=quantity if direction=='add' else -quantity
     if self.db.execute('UPDATE inventory SET quantity=quantity+? WHERE id=? AND quantity+?>=0',(delta,item['id'],delta)).rowcount!=1:raise ValueError('Insufficient stock')
     self.movement(actor,store,item['id'],delta,reason,str(uuid.uuid4()))
    self.c.log(actor,action,item['id'],{'quantity':quantity,'reason':reason})
   return {'ok':True}
  if action=='payment':
   self.check(actor,store,'sell');request_key=key(body.get('request_key'))
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.sale(actor,body.get('id'))
    if row['store_id']!=store:raise PermissionError('Cross-store payment denied')
    prior=self.db.execute('SELECT * FROM pos_tenders WHERE sale_id=? AND request_key LIKE ?',(row['id'],request_key+':%')).fetchall()
    if prior:
     expected=[(p.get('method'),p.get('amount_minor'),p.get('received_minor',p.get('amount_minor')) if p.get('method')=='cash' else p.get('amount_minor'),p.get('reference','').strip()) for p in body.get('payments',[])]
     actual=[(p['method'],p['amount_minor'],p['received_minor'],p['reference']) for p in sorted(prior,key=lambda x:x['request_key'])]
     if expected!=actual:raise ValueError('Payment request key belongs to different payment parts')
     return self.sale_public(row,True)
    if row['state'] not in ('partial','unpaid'):raise ValueError('Sale has no payable balance')
    self.add_tenders(actor,row,body.get('shift_id'),body.get('payments'),request_key)
    source=json.loads(row['payload']).get('restaurant_order_id')
    if source:
     from restaurant import Restaurant
     Restaurant(self.c).release_if_paid(Restaurant(self.c).order(store,source))
   return self.sale_public(self.sale(actor,row['id']),True)
  if action=='cancel':
   self.check(actor,store,'refund')
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.sale(actor,body.get('id'),'refund')
    if row['store_id']!=store:raise PermissionError('Cross-store cancellation denied')
    if row['state']=='cancelled':return {'ok':True}
    if row['paid_minor']!=row['refunded_minor']:raise ValueError('Refund collected payments before cancelling the outstanding sale')
    reason=label(body.get('reason'),'cancellation reason',500);snapshot=json.loads(row['payload'])
    if snapshot.get('restaurant_order_id'):raise ValueError('Use restaurant cancellation and food-waste controls for prepared orders')
    for line in snapshot['lines']:
     qty=line['quantity']-line['returned']
     if qty:self.db.execute('UPDATE inventory SET quantity=quantity+? WHERE id=?',(qty,line['id']));self.movement(actor,store,line['id'],qty,'cancel: '+reason,row['id'])
    self.db.execute("UPDATE pos_sales SET state='cancelled' WHERE id=?",(row['id'],));self.c.log(actor,'pos_cancel',row['id'],{'reason':reason})
   return {'ok':True}
  if action=='refund':
   self.check(actor,store,'refund');request_key=key(body.get('request_key'));fingerprint=digest(body)
   with self.db:
    self.db.execute('BEGIN IMMEDIATE');row=self.sale(actor,body.get('id'),'refund')
    if row['store_id']!=store:raise PermissionError('Cross-store refund denied')
    prior=self.db.execute('SELECT * FROM pos_refunds WHERE sale_id=? AND request_key=?',(row['id'],request_key)).fetchone()
    if prior:
     if prior['request_digest']!=fingerprint:raise ValueError('Refund key belongs to another request')
     return {'id':prior['id'],'amount_minor':prior['amount_minor']}
    self.shift(actor,store,body.get('shift_id'));snapshot=json.loads(row['payload']);requested=body.get('items');amount=0;seen=set();restock=body.get('restock') is True
    if snapshot.get('restaurant_order_id') and restock:raise ValueError('Prepared food cannot be automatically returned to sellable stock')
    if not isinstance(requested,list) or not requested:raise ValueError('Choose returned product quantities')
    for request in requested:
     matches=[l for l in snapshot['lines'] if l['id']==request.get('id') and ('line' not in request or l.get('line')==request['line'])]
     if len(matches)>1:raise ValueError('Specify the receipt line number when a product occurs with different modifiers')
     line=matches[0] if matches else None
     if not line or line.get('line',line['id']) in seen:raise ValueError('Unknown or duplicate returned item')
     seen.add(line.get('line',line['id']));qty=integer(request.get('quantity'),'return quantity',1,line['quantity']-line['returned']);before=line['total_minor']*line['returned']//line['quantity'];line['returned']+=qty;amount+=line['total_minor']*line['returned']//line['quantity']-before
     if restock:self.db.execute('UPDATE inventory SET quantity=quantity+? WHERE id=?',(qty,line['id']));self.movement(actor,store,line['id'],qty,'refund',row['id'])
    extras=integer(body.get('extra_minor',0),'extra_minor',0,snapshot.get('service_minor',0)+snapshot.get('service_tax_minor',0)+snapshot.get('tip_minor',0)-snapshot.get('extras_refunded',0));snapshot['extras_refunded']=snapshot.get('extras_refunded',0)+extras;amount+=extras
    if not 0<amount<=row['paid_minor']-row['refunded_minor']:raise ValueError('Refund exceeds collected, unrefunded payments')
    method=body.get('method','cash')
    if method not in ('cash','external_terminal','bank_transfer','demo') or method=='demo' and not self.c.demo:raise ValueError('Unsupported refund method')
    if method in ('external_terminal','bank_transfer') and (body.get('confirmed_external') is not True or not body.get('reference')):raise ValueError('Confirm the external refund and provide its reference; no gateway refund is sent')
    reason=label(body.get('reason'),'return reason',500);rid=str(uuid.uuid4());self.db.execute('INSERT INTO pos_refunds VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,row['id'],body['shift_id'],request_key,fingerprint,amount,method,dump({'items':requested,'restock':restock,'reason':reason,'reference':body.get('reference',''),'extra_minor':extras}),actor['id'],time.time()));self.db.execute('UPDATE pos_sales SET payload=?,refunded_minor=refunded_minor+?,state=CASE WHEN refunded_minor+?=total_minor THEN ? ELSE state END WHERE id=?',(dump(snapshot),amount,amount,'refunded',row['id']));self.c.log(actor,'pos_refund',rid,{'sale_id':row['id'],'amount_minor':amount,'method':method})
   return {'id':rid,'amount_minor':amount,'external_transfer_performed':False}
  if action in ('receipt','verify-exit'):
   cap='verify' if action=='verify-exit' else 'sell';self.check(actor,store,cap)
   with self.db:
    if action=='verify-exit':self.db.execute('BEGIN IMMEDIATE')
    row=self.sale(actor,body.get('id'),cap)
    if row['store_id']!=store:raise PermissionError('Receipt belongs to another store')
    if action=='verify-exit':
     if not secrets.compare_digest(str(body.get('receipt_token','')),json.loads(row['payload'])['receipt_token']):raise PermissionError('Invalid receipt verification token')
     if row['state'] not in ('paid','demo_paid') or row['refunded_minor'] or row['exit_verified']:raise ValueError('Receipt unpaid, refunded or already used for exit verification')
     self.db.execute('UPDATE pos_sales SET exit_verified=? WHERE id=? AND exit_verified IS NULL',(time.time(),row['id']));self.c.log(actor,'exit_verified',row['id'],{'demo':self.c.demo})
   return self.sale_public(self.sale(actor,row['id'],cap),True)
  if action=='product-qr':
   self.check(actor,store,'stock');item=self.db.execute('SELECT barcode FROM inventory WHERE id=? AND store_id=?',(body.get('id'),store)).fetchone()
   if not item:raise ValueError('Product not found')
   from pos_exports import qr_svg
   return qr_svg(item['barcode'])
  if action=='receipt-qr':
   row=self.sale(actor,body.get('id'))
   if row['store_id']!=store:raise PermissionError('Receipt belongs to another store')
   from pos_exports import qr_url,qr_svg
   return qr_svg(qr_url(self.c,'/receipt.html',{'id':row['id'],'token':json.loads(row['payload'])['receipt_token']}))
  if action=='report-export':
   from pos_exports import report_export
   return report_export(self.dispatch(actor,'report',body),body.get('format'))
  if action=='report':
   self.check(actor,store,'report');days=integer(body.get('days',30),'days',1,366);cutoff=time.time()-days*86400
   rows=self.db.execute('SELECT * FROM pos_sales WHERE store_id=? AND created>=? ORDER BY created DESC LIMIT 10000',(store,cutoff)).fetchall();sales=[]
   for r in rows:
    data=json.loads(r['payload']);sales.append({k:r[k] for k in ('id','state','total_minor','paid_minor','refunded_minor','created')}|{k:data[k] for k in ('currency','tax_minor','tip_minor')})
   stock=self.products(store)
   total={'sales_count':len(rows),'billed_minor':sum(r['total_minor'] for r in rows),'collected_minor':sum(r['paid_minor'] for r in rows),'refunded_minor':sum(r['refunded_minor'] for r in rows),'outstanding_minor':sum(max(0,r['total_minor']-r['paid_minor']) for r in rows),'tax_billed_minor':sum(s['tax_minor'] for s in sales),'tips_billed_minor':sum(s['tip_minor'] for s in sales),'stock_cost_minor':sum(x['quantity']*x.get('cost_minor',0) for x in stock)}
   if any(abs(v)>9007199254740991 for v in total.values()):raise ValueError('Report exceeds safe numeric display precision; use a narrower reporting period')
   movements=[dict(r) for r in self.db.execute('SELECT * FROM pos_movements WHERE store_id=? AND created>=? ORDER BY created DESC LIMIT 2000',(store,cutoff))]
   low=[{'id':p['id'],'name':p['name'],'quantity':p['quantity'],'reorder_suggestion':max(0,p.get('low_stock',5)*2-p['quantity'])} for p in stock if p['quantity']<=p.get('low_stock',5)]
   return {'summary':total,'sales':sales,'low_stock':low,'movements':movements,'currency':record['currency'],'minor_exponent':self.settings(store)[0]['minor_exponent'],'notice':'Last '+str(days)+' rolling days, maximum 10,000 sales and 2,000 stock movements. Billed tax/tips are not a remittance calculation. Inventory valuation uses merchant-entered unit costs; no certified profit or predictive forecast.'}
  raise ValueError('Unknown register operation')
