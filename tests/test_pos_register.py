import json,secrets,tempfile,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from control import Control
from pos_engine import POS
from restaurant import Restaurant

class RegisterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'pos.db');self.c=Control(self.path);self.c.bootstrap('root@example.test','register-root-password');self.actor=self.c.actor(self.c.login('root@example.test','register-root-password'))
  self.tenant=self.c.mutate(self.actor,'tenants',{'name':'Retail'})['id'];self.store=self.c.mutate(self.actor,'stores',{'tenant_id':self.tenant,'name':'Store','address':'Store address','country':'US','currency':'USD','timezone':'America/Chicago','language':'en','lat':33.,'lon':-96.})['id']
  for feature in ('pos','restaurant','scan_go','returns'):self.c.mutate(self.actor,'features',{'scope':'global','feature':feature,'enabled':True})
  self.pos=POS(self.c);self.rest=Restaurant(self.c);self.item=self.c.mutate(self.actor,'inventory',{'store_id':self.store,'name':'Rice meal','barcode':'123','quantity':10,'price_minor':1000})['id'];self.shift=self.call('shift-open',terminal='Register A',opening_minor=10000)['id']
 def tearDown(self):self.c.db.close();self.tmp.cleanup()
 def call(self,action,**body):return self.pos.dispatch(self.actor,action,dict(body,store_id=self.store))
 def checkout(self,quantity=1,**extra):
  data={'store_id':self.store,'items':[{'id':self.item,'quantity':quantity}],**extra};quote=self.pos.quote(self.actor,self.store,data);data.update(quote_digest=quote['quote_digest'],request_key=str(uuid.uuid4()),shift_id=self.shift,payments=[{'method':'cash','amount_minor':quote['total_minor']}]);return self.pos.checkout(self.actor,data)
 def test_server_prices_stock_and_idempotency(self):
  body={'store_id':self.store,'items':[{'id':self.item,'quantity':2,'price_minor':1}],'total_minor':1};quote=self.pos.quote(self.actor,self.store,body);body.update(quote_digest=quote['quote_digest'],request_key=str(uuid.uuid4()),shift_id=self.shift,payments=[{'method':'cash','amount_minor':2000}]);one=self.pos.checkout(self.actor,body);two=self.pos.checkout(self.actor,body);self.assertEqual(one['id'],two['id']);self.assertEqual(one['total_minor'],2000);self.assertEqual(self.c.db.execute('SELECT quantity FROM inventory WHERE id=?',(self.item,)).fetchone()[0],8)
  body['items'][0]['quantity']=1
  with self.assertRaises(ValueError):self.pos.checkout(self.actor,body)
 def test_changed_quote_and_cash_change(self):
  data={'store_id':self.store,'items':[{'id':self.item,'quantity':1}]};quote=self.pos.quote(self.actor,self.store,data)
  with self.c.db:self.c.db.execute('UPDATE inventory SET price_minor=2000 WHERE id=?',(self.item,))
  data.update(quote_digest=quote['quote_digest'],request_key=str(uuid.uuid4()),shift_id=self.shift,payments=[{'method':'cash','amount_minor':1000}])
  with self.assertRaises(ValueError):self.pos.checkout(self.actor,data)
  quote=self.pos.quote(self.actor,self.store,data);data.update(quote_digest=quote['quote_digest'],payments=[{'method':'cash','amount_minor':2000,'received_minor':5000}]);sale=self.pos.checkout(self.actor,data);self.assertEqual(sale['payments'][0]['received_minor'],5000);closed=self.call('shift-close',id=self.shift,actual_minor=12000);self.assertEqual(closed['difference_minor'],0)
 def test_partial_split_payments_and_replay_binding(self):
  self.call('settings',revision=0,allow_partial=True);customer=self.call('customer',name='Customer')['id'];data={'store_id':self.store,'customer_id':customer,'items':[{'id':self.item,'quantity':1}]};quote=self.pos.quote(self.actor,self.store,data);data.update(quote_digest=quote['quote_digest'],request_key=str(uuid.uuid4()),shift_id=self.shift,payments=[{'method':'cash','amount_minor':200},{'method':'cash','amount_minor':300}]);sale=self.pos.checkout(self.actor,data);self.assertEqual(sale['state'],'partial')
  key=str(uuid.uuid4());self.call('payment',id=sale['id'],shift_id=self.shift,request_key=key,payments=[{'method':'cash','amount_minor':500}]);self.call('payment',id=sale['id'],shift_id=self.shift,request_key=key,payments=[{'method':'cash','amount_minor':500}]);self.assertEqual(self.call('receipt',id=sale['id'])['paid_minor'],1000)
  with self.assertRaises(ValueError):self.call('payment',id=sale['id'],shift_id=self.shift,request_key=key,payments=[{'method':'cash','amount_minor':400}])
 def test_tax_discount_customer_and_bulk_prices(self):
  self.call('settings',revision=0,tax_bps=1000);self.call('price',inventory_id=self.item,minimum=2,price_minor=800);customer=self.call('customer',name='Wholesale')['id'];self.call('price',inventory_id=self.item,customer_id=customer,minimum=1,price_minor=700);self.call('coupon',code='SAVE',kind='percent',value=1000,active=True)
  quote=self.pos.quote(self.actor,self.store,{'items':[{'id':self.item,'quantity':2}],'customer_id':customer,'coupon':'SAVE'});self.assertEqual(quote['subtotal_minor'],1400);self.assertEqual(quote['discount_minor'],140);self.assertEqual(quote['tax_minor'],126);self.assertEqual(quote['total_minor'],1386)
 def test_refund_is_bounded_and_stock_return_is_once(self):
  sale=self.checkout(2);key=str(uuid.uuid4());body=dict(id=sale['id'],shift_id=self.shift,request_key=key,items=[{'id':self.item,'quantity':1}],restock=True,reason='Customer return');first=self.call('refund',**body);self.assertEqual(first['amount_minor'],1000);self.assertEqual(self.call('refund',**body)['id'],first['id']);self.assertEqual(self.c.db.execute('SELECT quantity FROM inventory WHERE id=?',(self.item,)).fetchone()[0],9)
  with self.assertRaises(ValueError):self.call('refund',**dict(body,request_key=str(uuid.uuid4()),items=[{'id':self.item,'quantity':2}]))
 def test_exit_receipt_cannot_be_reused(self):
  sale=self.checkout();self.call('verify-exit',id=sale['id'],receipt_token=sale['receipt_token'])
  with self.assertRaises(ValueError):self.call('verify-exit',id=sale['id'],receipt_token=sale['receipt_token'])
  with self.assertRaises(PermissionError):self.call('verify-exit',id=sale['id'],receipt_token='wrong')
 def test_feature_disable_and_cross_store_shift(self):
  self.c.mutate(self.actor,'features',{'scope':'store:'+self.store,'feature':'pos','enabled':False})
  with self.assertRaises(PermissionError):self.checkout()
 def test_parallel_last_unit_cannot_oversell(self):
  with self.c.db:self.c.db.execute('UPDATE inventory SET quantity=1 WHERE id=?',(self.item,))
  base={'store_id':self.store,'items':[{'id':self.item,'quantity':1}]};quote=self.pos.quote(self.actor,self.store,base)
  def attempt(_):
   c=Control(self.path)
   try:
    try:return POS(c).checkout(self.actor,dict(base,quote_digest=quote['quote_digest'],request_key=str(uuid.uuid4()),shift_id=self.shift,payments=[{'method':'cash','amount_minor':1000}]))['id']
    except ValueError:return None
   finally:c.db.close()
  with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(attempt,range(2)))
  self.assertEqual(sum(x is not None for x in result),1)
 def test_cashier_cannot_refund_or_attest_external_payment(self):
  uid=self.c.mutate(self.actor,'users',{'email':'cashier@example.test','password':'cashier-long-password'})['id'];self.c.mutate(self.actor,'memberships',{'user_id':uid,'tenant_id':self.tenant,'store_id':self.store,'role':'cashier'});cashier=self.c.actor(self.c.login('cashier@example.test','cashier-long-password'));pos=POS(self.c);sid=pos.dispatch(cashier,'shift-open',{'store_id':self.store,'terminal':'Cashier'})['id'];data={'store_id':self.store,'items':[{'id':self.item,'quantity':1}]};q=pos.quote(cashier,self.store,data)
  with self.assertRaises(PermissionError):pos.checkout(cashier,dict(data,quote_digest=q['quote_digest'],request_key=str(uuid.uuid4()),shift_id=sid,payments=[{'method':'bank_transfer','amount_minor':1000,'reference':'ref','confirmed_external':True}]))
  sale=self.checkout()
  with self.assertRaises(PermissionError):pos.dispatch(cashier,'refund',{'store_id':self.store,'id':sale['id']})
 def test_report_exports_real_xlsx_and_safe_csv(self):
  self.checkout();xlsx=self.call('report-export',format='xlsx');csv=self.call('report-export',format='csv');import base64,zipfile,io
  with zipfile.ZipFile(io.BytesIO(base64.b64decode(xlsx['base64']))) as z:self.assertIn('xl/worksheets/sheet1.xml',z.namelist())
  self.assertIn(b'Total minor',base64.b64decode(csv['base64']))

class RestaurantTests(RegisterTests):
 # Inherit register tests to exercise the same initialized restaurant schema.
 def setup_menu(self):
  ingredient=self.rest.dispatch(self.actor,'ingredient',{'store_id':self.store,'name':'Rice','unit':'g','quantity':'1000','cost_minor_per_1000':500,'low_stock':'200'})['id'];self.ingredient=ingredient
  self.rest.dispatch(self.actor,'menu',{'store_id':self.store,'id':self.item,'active':True,'allergens':'Merchant supplied: sesame','recipe':[{'id':ingredient,'quantity':'100'}],'modifier_groups':[{'name':'Portion','minimum':1,'maximum':1,'choices':[{'id':'regular','name':'Regular','price_minor':0},{'id':'large','name':'Large','price_minor':200}]}]})
  self.table=self.rest.dispatch(self.actor,'table',{'store_id':self.store,'name':'Table 1','capacity':4})['id']
 def order_body(self):return {'store_id':self.store,'table_id':self.table,'mode':'dine_in','items':[{'id':self.item,'quantity':2,'modifiers':['large'],'notes':'No extra sauce'}],'guests':2}
 def create_order(self):
  body=self.order_body();quote=self.rest.dispatch(self.actor,'quote',body);return self.rest.dispatch(self.actor,'order-create',dict(body,quote_digest=quote['quote_digest'],request_key=str(uuid.uuid4())))
 def advance(self,order,state,**extra):return self.rest.dispatch(self.actor,'order-status',{'store_id':self.store,'id':order['id'],'revision':order['revision'],'state':state,**extra})
 def test_recipe_consumption_payment_and_table_release(self):
  self.setup_menu();order=self.create_order();self.advance(order,'queued');row=self.rest.order(self.store,order['id']);self.assertEqual(self.c.db.execute('SELECT quantity_milli FROM restaurant_ingredients WHERE id=?',(self.ingredient,)).fetchone()[0],800000)
  with self.assertRaises(ValueError):self.advance(order,'queued')
  for state in ('preparing','ready','served'):
   self.advance(self.rest.order_data(row),state);row=self.rest.order(self.store,order['id'])
  quote=self.rest.payment_quote(self.actor,self.store,order['id']);sale=self.pos.checkout(self.actor,{'store_id':self.store,'restaurant_order_id':order['id'],'quote_digest':quote['quote_digest'],'request_key':str(uuid.uuid4()),'shift_id':self.shift,'payments':[{'method':'cash','amount_minor':2400}]});self.assertEqual(sale['state'],'paid');self.assertEqual(self.c.db.execute('SELECT quantity FROM inventory WHERE id=?',(self.item,)).fetchone()[0],8);self.assertEqual(self.rest.table(self.store,self.table)['state'],'cleaning')
  with self.assertRaises(ValueError):self.rest.payment_quote(self.actor,self.store,order['id'])
 def test_queue_cancellation_restores_unprepared_ingredients_once(self):
  self.setup_menu();order=self.create_order();self.advance(order,'queued');current=self.rest.order_data(self.rest.order(self.store,order['id']));self.advance(current,'cancelled',reason='Guest changed plans');self.assertEqual(self.c.db.execute('SELECT quantity_milli FROM restaurant_ingredients WHERE id=?',(self.ingredient,)).fetchone()[0],1000000)
  with self.assertRaises(ValueError):self.advance(current,'cancelled',reason='Again')
 def test_reservations_reject_overlap_and_capacity(self):
  self.setup_menu();body={'store_id':self.store,'table_id':self.table,'name':'Guest','start':2000000000,'end':2000003600,'guests':2};self.rest.dispatch(self.actor,'reservation',body)
  with self.assertRaises(ValueError):self.rest.dispatch(self.actor,'reservation',body)
  with self.assertRaises(ValueError):self.rest.dispatch(self.actor,'reservation',dict(body,start=2000010000,end=2000013600,guests=6))
 def test_qr_order_is_pending_and_guest_token_is_scoped(self):
  self.setup_menu();token=json.loads(self.rest.table(self.store,self.table)['payload'])['qr_token'];body=dict(self.order_body(),table_token=token,accepted=True,request_key=str(uuid.uuid4()));order=self.rest.public('order',body);self.assertEqual(order['state'],'requested');self.assertEqual(self.rest.public('order',body)['id'],order['id']);self.assertEqual(self.c.db.execute('SELECT quantity FROM inventory WHERE id=?',(self.item,)).fetchone()[0],10)
  with self.assertRaises(PermissionError):self.rest.public('track',{'id':order['id'],'guest_token':'wrong'})
  tracked=self.rest.public('track',{'id':order['id'],'guest_token':order['guest_token']});self.assertNotIn('guest_token',tracked);self.assertNotIn('contact',tracked)
 def test_modifier_constraints_and_ingredient_shortage(self):
  self.setup_menu();body=self.order_body();body['items'][0]['modifiers']=[]
  with self.assertRaises(ValueError):self.rest.dispatch(self.actor,'quote',body)
  with self.c.db:self.c.db.execute('UPDATE restaurant_ingredients SET quantity_milli=1000')
  with self.assertRaises(ValueError):self.rest.dispatch(self.actor,'quote',self.order_body())
