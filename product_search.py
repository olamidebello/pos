"""Read-only nearest-store product search for the Ansible discovery worker."""
import math
import sqlite3
from pathlib import Path

def search(data, db_path):
    query=data.get('query','')
    if not isinstance(query,str) or not 2<=len(query.strip())<=200:raise ValueError('Enter a product description of 2–200 characters')
    lat,lon=float(data['lat']),float(data['lon'])
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:raise ValueError('Invalid location')
    radius=float(data.get('radius_km',10))
    if not math.isfinite(radius) or not 0.5<=radius<=50:raise ValueError('Search radius must be 0.5–50 kilometres')
    terms=query.casefold().split()
    if len(terms)>15:raise ValueError('Use a shorter description')
    connection=sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    try:
        # Bound processing even for large catalogs; input is never SQL or a shell argument.
        connection.set_progress_handler(lambda: 1 if __import__('time').monotonic()>deadline else 0,10000)
        deadline=__import__('time').monotonic()+5
        stores=connection.execute("SELECT s.* FROM stores s JOIN tenants t ON s.tenant_id=t.id WHERE s.status='active' AND t.status='active'")
        ranked=[]
        for row in stores:
            store=dict(row);a,b=math.radians(lat),math.radians(store['lat'])
            h=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(store['lon']-lon)/2)**2
            distance=6371*2*math.asin(math.sqrt(min(1,h)))
            if distance<=radius:ranked.append((distance,store))
        ranked.sort(key=lambda value:value[0]);results=[]
        for distance,store in ranked[:200]:
            products=[]
            # LIKE parameters escape wildcard characters; exact Python token filtering handles casefold.
            first=terms[0].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            rows=connection.execute("SELECT id,barcode,name,quantity,price_minor FROM inventory WHERE store_id=? AND (name LIKE ? ESCAPE '\\' OR barcode LIKE ? ESCAPE '\\') LIMIT 1000",(store['id'],'%'+first+'%','%'+first+'%'))
            for item in rows:
                if all(term in (item['name']+' '+item['barcode']).casefold() for term in terms):products.append(dict(item))
                if len(products)>=10:break
            if products:
                results.append({k:store[k] for k in ('id','name','address','country','currency','lat','lon')}|{'distance_km':round(distance,2),'products':products,'source':'merchant_catalog'})
            if len(results)>=20:break
        return {'stores':results,'query':query.strip(),'radius_km':radius,'catalog_note':'Merchant-published catalog. Recheck availability and totals at checkout.'}
    finally:connection.close()
