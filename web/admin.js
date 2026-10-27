'use strict';
let token='',state={},demoId='';
const demoMode=new URLSearchParams(location.search).get('demo')==='1';
const el=id=>document.getElementById(id);
const schemas={
  tenants:['id','name','status'],
  stores:['id','tenant_id','name','address','country','currency','timezone','language','lat','lon','status'],
  inventory:['id','store_id','barcode','name','quantity','price_minor'],
  marketplace:['store_id','enabled','category','description'],
  gateways:['id','store_id','name','provider','mode','secret_ref','public_key','enabled'],
  api_keys:['id','store_id','name','scopes','expires_days','action'],
  users:['id','email','password','super','active'],
  memberships:['user_id','tenant_id','store_id','role','remove'],
  features:['scope','feature','enabled'],orders:[]
};
const defaults={status:'active',timezone:'UTC',language:'en',country:'NG',currency:'NGN',lat:0,lon:0,quantity:0,price_minor:0,active:true,super:false,enabled:false,provider:'stripe',mode:'test',scopes:'inventory:read',expires_days:90,action:'issue',category:'General'};
async function api(path,body){
  const url=demoMode?(path==='login'?'/api/demo/login':'/api/demo/'+demoId+'/'+path):'/api/'+path;
  const response=await fetch(url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:body?JSON.stringify(body):undefined,cache:'no-store'});
  const data=await response.json();if(!response.ok)throw Error(data.error||'Request failed');return data;
}
function message(text){el('message').textContent=text;}
function options(key,record){
  if(key==='tenant_id')return (state.tenants||[]).map(x=>[x.id,x.name+' · '+x.id]);
  if(key==='store_id')return [['','Tenant scope (memberships only)'],...(state.stores||[]).map(x=>[x.id,x.name+' · '+x.address])];
  if(key==='user_id')return (state.users||[]).map(x=>[x.id,x.email]);
  const choices={status:['active','suspended'],provider:demoMode?['demo','stripe','paystack','flutterwave']:['stripe','paystack','flutterwave'],mode:demoMode?['test']:['test','live'],role:state.roles,feature:state.feature_names,scopes:['inventory:read','inventory:write','orders:read'],action:['issue','revoke','rotate']};
  return choices[key]?.map(value=>[value,value]);
}
function form(record={}){
  const resource=el('resource').value;el('fields').replaceChildren();el('saveRecord').hidden=resource==='orders';el('new').hidden=resource==='orders';
  el('locate').hidden=resource!=='stores';
  el('resourceHelp').textContent={gateways:'Select a store and provider. Secret reference names identify credentials installed privately on the server; never paste secret keys here. Live payments require explicit server enablement.',api_keys:'Issue a store-scoped key, or select a key and choose revoke/rotate. Supported scopes: inventory:read, inventory:write, orders:read. New key secrets appear once.',marketplace:'Publish or hide this store in the marketplace, and choose its category and description.',orders:'Order status is read-only. Reconcile queries the provider on the server; redirects and screenshots cannot mark orders paid.',inventory:'Stock is available quantity, excluding units held by pending orders. Prices are integer minor units in the selected store currency.'}[resource]||'Select a record to edit, or create a new record.';
  for(const key of schemas[resource]){
    const label=document.createElement('label');label.textContent=key.replaceAll('_',' ')+' ';
    const choices=options(key,record);let input;
    if(choices){input=document.createElement('select');for(const [value,title] of choices){const option=document.createElement('option');option.value=value;option.textContent=title;input.append(option);}}
    else{input=document.createElement('input');if(['super','active','enabled','remove'].includes(key)){input.type='checkbox';input.checked=Boolean(record[key]??defaults[key]);}
      else if(['lat','lon','quantity','price_minor','expires_days'].includes(key)){input.type='number';input.step=['lat','lon'].includes(key)?'any':'1';}
      else if(key==='password'){input.type='password';input.autocomplete='new-password';}
      if(key==='secret_ref')input.placeholder='GLOBALPOS_STRIPE_TEST_LAGOS';
    }
    if(key==='scopes'){input.multiple=true;const selected=(record.scopes||defaults.scopes).split(',');for(const option of input.options)option.selected=selected.includes(option.value);}
    input.name=key;if(input.type!=='checkbox'&&key!=='scopes')input.value=record[key]??defaults[key]??'';
    if(key==='id'&&record.id)input.readOnly=true;
    if(key==='action'&&record.id)input.value='revoke';
    label.append(input);el('fields').append(label);
  }
}
function records(){
  const resource=el('resource').value;el('records').replaceChildren();
  for(const row of state[resource]||[]){
    if(resource==='orders'){
      const card=document.createElement('article');const text=document.createElement('p');text.textContent=`${row.id} · ${row.currency} ${row.total_minor} minor units · ${row.status}`;
      const button=document.createElement('button');button.type='button';button.textContent='Reconcile payment';button.addEventListener('click',async()=>{button.disabled=true;try{const result=await api('admin/order-verify',{id:row.id});await refresh();message('Order status: '+result.status);}catch(e){message(e.message);}finally{button.disabled=false;}});card.append(text,button);el('records').append(card);continue;
    }
    const button=document.createElement('button');button.type='button';
    button.textContent=(row.name||row.email||[row.scope,row.feature,row.user_id,row.role,row.store_id].filter(Boolean).join(' · '))+(resource==='gateways'?` · ${row.mode} · ${row.configured?'server key present':'server key pending'}`:resource==='api_keys'?` · ${row.prefix} · ${row.active?'active':'revoked'}`:'');
    button.addEventListener('click',()=>form(row));el('records').append(button);
  }
  form();
}
async function refresh(){
  state=await api('admin');el('overview').textContent=`${state.tenants.length} tenants · ${state.stores.length} stores · ${state.inventory.length} inventory records · ${state.gateways.length} gateways · ${state.api_keys.length} integration keys`;
  el('audit').replaceChildren();for(const row of state.audit||[]){const p=document.createElement('p');p.textContent=`${row.created} · ${row.actor} · ${row.action} · ${row.target}`;el('audit').append(p);}
  el('login').hidden=true;el('workspace').hidden=false;records();
}
el('loginForm').addEventListener('submit',async event=>{
  event.preventDefault();try{const data=Object.fromEntries(new FormData(event.target));const session=await api('login',data);token=session.token;demoId=session.demo_id||'';
    if(demoMode){el('sampleStore').href='/?demo='+demoId;el('sampleStore').hidden=false;el('sampleMarket').href='/marketplace.html?demo='+demoId;el('sampleMarket').hidden=false;}
    event.target.reset();await refresh();message(demoMode?'Signed in to your isolated demo.':'Signed in.');
  }catch(e){message(e.message);}
});
el('editor').addEventListener('submit',async event=>{
  event.preventDefault();el('saveRecord').disabled=true;try{const data={};for(const input of el('fields').querySelectorAll('input,select')){
    if(input.multiple)data[input.name]=Array.from(input.selectedOptions,x=>x.value).join(',');else if(input.type==='checkbox')data[input.name]=input.checked;else if(input.value!=='')data[input.name]=input.type==='number'?Number(input.value):input.value;
  }
    const result=await api('admin/'+el('resource').value,data);await refresh();
    if(result.secret){el('newKey').textContent=result.secret;el('keyReveal').hidden=false;message('API key issued. Copy its secret now; it will not be shown again.');}
    else message('Saved. Change recorded in the audit trail.');
  }catch(e){message(e.message);}finally{el('saveRecord').disabled=false;}
});
el('hideKey').addEventListener('click',()=>{el('newKey').textContent='';el('keyReveal').hidden=true;});
el('resource').addEventListener('change',records);el('new').addEventListener('click',()=>form());
el('logout').addEventListener('click',async()=>{try{await api('logout',{});}finally{token='';state={};demoId='';el('newKey').textContent='';el('keyReveal').hidden=true;el('sampleStore').hidden=true;el('sampleMarket').hidden=true;el('workspace').hidden=true;el('login').hidden=false;message('Signed out.');}});
el('locate').addEventListener('click',()=>{
  if(el('resource').value!=='stores')return;
  if(!navigator.geolocation){message('Location unavailable; enter coordinates manually.');return;}
  navigator.geolocation.getCurrentPosition(position=>{el('fields').querySelector('[name=lat]').value=position.coords.latitude;el('fields').querySelector('[name=lon]').value=position.coords.longitude;message('Coordinates captured. Confirm the store address and save.');},()=>message('Location unavailable; enter coordinates manually.'),{timeout:10000,maximumAge:0});
});
if(demoMode){el('demoNotice').hidden=false;el('loginForm').elements.email.value='demo@globalpos.example';el('loginForm').elements.password.value='GlobalPOS-Demo!2026';message('Demo login is prefilled. Sign in to create your own isolated sample workspace.');}
