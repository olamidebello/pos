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

LOCK=threading.Lock()
CACHE={}

def coordinates(data):
    lat,lon=float(data['lat']),float(data['lon'])
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
        raise ValueError('Invalid coordinates')
    return round(lat,3),round(lon,3)

def parse_findings(data):
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
            inventory=[],inventory_status='No verified inventory source connected. Stock and prices unavailable.',verified=False))
    return rows[:20]

def fetch(data):
    lat,lon=coordinates(data)
    query=f'[out:json][timeout:15];nwr[shop][name](around:500,{lat},{lon});out center tags 200;'
    request=urllib.request.Request('https://overpass-api.de/api/interpreter',data=urllib.parse.urlencode({'data':query}).encode(),headers={'User-Agent':'GlobalPOS/1.0 StoreDiscovery','Content-Type':'application/x-www-form-urlencoded'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
        raw=response.read(2000001)
    if len(raw)>2000000:raise ValueError('Discovery response too large')
    return {'findings':parse_findings(json.loads(raw)),'attribution':'© OpenStreetMap contributors · ODbL','fetched_at':time.time()}

def discover(data,state_path):
    lat,lon=coordinates(data);key=(lat,lon)
    if not LOCK.acquire(blocking=False):raise ValueError('Discovery is busy; try again shortly')
    try:
        now=time.time()
        for old in list(CACHE):
            if CACHE[old][0]<now:del CACHE[old]
        if key in CACHE:return CACHE[key][1]
        with tempfile.TemporaryDirectory(prefix='discovery-',dir=Path(state_path).parent) as folder:
            output=str(Path(folder)/'result.json');variables=Path(folder)/'vars.json'
            variables.write_text(json.dumps({'discovery_lat':lat,'discovery_lon':lon,'discovery_output':output}))
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
    payload=json.load(sys.stdin)
    result=fetch(payload)
    Path(sys.argv[1]).write_text(json.dumps(result))
