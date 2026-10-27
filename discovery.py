"""Unprivileged, read-only online business discovery. No production inventory writes."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import ipaddress


LOCK=threading.Lock()
CACHE={}

def coordinates(data):
    lat,lon=float(data['lat']),float(data['lon'])
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
        raise ValueError('Invalid coordinates')
    return round(lat,3),round(lon,3)

def website_url(value):
    if not isinstance(value,str) or len(value)>2000:return ''
    try:
        url=urllib.parse.urlsplit(value)
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.port not in (None,443):return ''
        host=url.hostname.lower().rstrip('.')
        if '.' not in host or host.endswith(('.local','.internal','.localhost','.test','.example')):return ''
        try:
            if not ipaddress.ip_address(host).is_global:return ''
        except ValueError:pass
        return value
    except ValueError:return ''

# Categories identify possible sellers, never prove a specific product is stocked.
CATEGORIES={
    'supermarket':{'food','rice','water','milk','bread','grocery','groceries','drink','drinks','sugar','oil','eggs','flour'},
    'convenience':{'food','water','milk','bread','drink','drinks','snack','snacks'},
    'pharmacy':{'medicine','pharmacy','toothpaste','vitamin','vitamins'},
    'electronics':{'phone','charger','cable','laptop','computer','electronics'},
    'clothes':{'shirt','shoes','dress','clothes','clothing'},
    'hardware':{'tools','screw','paint','hardware','hammer'},
}

def parse_findings(data, limit=20):
    rows=[]
    for item in data.get('elements',[])[:200]:
        tags=item.get('tags',{});center=item.get('center',item)
        if not tags.get('name') or not tags.get('shop'):continue
        try:lat,lon=coordinates(center)
        except (KeyError,TypeError,ValueError):continue
        kind=item.get('type');identifier=item.get('id')
        if kind not in ('node','way','relation') or type(identifier) is not int:continue
        rows.append(dict(source_id=f'{kind}/{identifier}',name=str(tags['name'])[:200],
            address=', '.join(str(tags[k])[:200] for k in ('addr:housenumber','addr:street','addr:city','addr:postcode','addr:country') if tags.get(k)) or 'Address unavailable; confirm with store',
            lat=lat,lon=lon,category=str(tags['shop'])[:100],source_url=f'https://www.openstreetmap.org/{kind}/{identifier}',
            website_url=website_url(tags.get('website',tags.get('contact:website',''))),inventory=[],inventory_status='No verified inventory source connected. Stock and prices unavailable.',verified=False))
    return rows[:limit]

def fetch(data):
    lat,lon=coordinates(data)
    radius=10000 if data.get('query') else 500
    query=f'[out:json][timeout:15];nwr[shop][name](around:{radius},{lat},{lon});out center tags 200;'
    request=urllib.request.Request('https://overpass-api.de/api/interpreter',data=urllib.parse.urlencode({'data':query}).encode(),headers={'User-Agent':'GlobalPOS/1.0 StoreDiscovery','Content-Type':'application/x-www-form-urlencoded'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
        raw=response.read(2000001)
    if len(raw)>2000000:raise ValueError('Discovery response too large')
    findings=parse_findings(json.loads(raw),limit=200)
    if data.get('query'):
        words=set(data['query'].casefold().split())
        categories={kind for kind,items in CATEGORIES.items() if words&items}
        findings=[item for item in findings if item['category'] in categories or words&set((item['name']+' '+item['category']).casefold().split())]
        for item in findings:item['match_note']='Possible seller based on listing category/name; product availability unconfirmed.'
    for item in findings:
        a,b=math.radians(lat),math.radians(item['lat'])
        h=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(item['lon']-lon)/2)**2
        item['distance_km']=round(6371*2*math.asin(math.sqrt(min(1,h))),2)
    findings.sort(key=lambda item:item['distance_km'])
    return {'findings':findings[:20],'attribution':'© OpenStreetMap contributors · ODbL','fetched_at':time.time()}

def discover(data,state_path):
    lat,lon=coordinates(data)
    product_mode='query' in data
    if product_mode:
        query=data['query']
        if not isinstance(query,str) or not 2<=len(query.strip())<=200 or len(query.split())>15:raise ValueError('Enter a product description of 2–200 characters, up to 15 words')
        query=query.strip()
    else:query=''
    # Catalog results are read fresh by the API; this cache contains external listing findings only.
    key=(lat,lon,query.casefold())
    if not LOCK.acquire(blocking=False):raise ValueError('Discovery is busy; try again shortly')
    try:
        now=time.time()
        for old in list(CACHE):
            if CACHE[old][0]<now:del CACHE[old]
        if key in CACHE:return CACHE[key][1]
        with tempfile.TemporaryDirectory(prefix='discovery-',dir=Path(state_path).parent) as folder:
            output=str(Path(folder)/'result.json');variables=Path(folder)/'vars.json'
            payload_path=Path(folder)/'input.json'
            payload_path.write_text(json.dumps({'lat':lat,'lon':lon,'query':query}))
            variables.write_text(json.dumps({'discovery_input':str(payload_path),'discovery_output':output}))
            process=subprocess.run(['/usr/bin/ansible-playbook','-i','localhost,','-c','local',str(Path(__file__).with_name('discover-store.yml')),'--extra-vars','@'+str(variables)],capture_output=True,timeout=32,env={**os.environ,'ANSIBLE_LOCAL_TEMP':folder+'/ansible','ANSIBLE_REMOTE_TEMP':folder+'/remote','ANSIBLE_NOCOLOR':'1'})
            if process.returncode:raise ValueError('Online discovery unavailable. Try again later or enter an address.')
            result=json.loads(Path(output).read_text())
        if len(CACHE)>=100:CACHE.pop(next(iter(CACHE)))
        CACHE[key]=(now+900,result)
        return result
    except (subprocess.TimeoutExpired,OSError,json.JSONDecodeError) as error:
        raise ValueError('Online discovery unavailable. Try again later or enter an address.') from error
    finally:LOCK.release()

if __name__=='__main__':
    payload=json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv)>2 else json.load(sys.stdin)
    result=fetch(payload)
    Path(sys.argv[1]).write_text(json.dumps(result))
