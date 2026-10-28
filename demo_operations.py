"""Fictional register and restaurant data; called only for isolated demo databases."""
import time
from pos_engine import POS
from restaurant import Restaurant

def seed(control,actor):
 if not control.demo:raise PermissionError('Demo seed requires an isolated demo database')
 stores=control.db.execute('SELECT * FROM stores ORDER BY name').fetchall()
 if not stores:return
 store=stores[0];pos=POS(control);restaurant=Restaurant(control)
 pos.dispatch(actor,'settings',{'store_id':store['id'],'revision':0,'category':'restaurant','tax_bps':0,'service_bps':0,'allow_partial':False,'receipt_note':'SIMULATED DEMO — not proof of payment'})
 ingredient=restaurant.dispatch(actor,'ingredient',{'store_id':store['id'],'name':'Sample rice ingredient','unit':'g','quantity':'10000','cost_minor_per_1000':100000,'low_stock':'500','reason':'Demo seed'})['id']
 for item in control.db.execute('SELECT * FROM inventory WHERE store_id=?',(store['id'],)).fetchall():
  restaurant.dispatch(actor,'menu',{'store_id':store['id'],'id':item['id'],'active':True,'category':'Demo menu','station':'Beverages' if 'Water' in item['name'] else 'Kitchen','allergens':'Fictional data — ask staff; no allergen-free assurance','recipe':[] if 'Water' in item['name'] else [{'id':ingredient,'quantity':'100'}],'modifier_groups':[{'name':'Serving','minimum':1,'maximum':1,'choices':[{'id':'standard','name':'Standard','price_minor':0},{'id':'extra','name':'Larger serving','price_minor':10000}]}]})
 table=restaurant.dispatch(actor,'table',{'store_id':store['id'],'name':'Table 1','capacity':4,'x':130,'y':180})['id'];restaurant.dispatch(actor,'table',{'store_id':store['id'],'name':'Table 2','capacity':6,'x':420,'y':180})
 now=int(time.time());restaurant.dispatch(actor,'reservation',{'store_id':store['id'],'table_id':table,'start':now+3600,'end':now+7200,'guests':2,'name':'Sample Guest','notes':'Fictional reservation'})
