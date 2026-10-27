'use strict';
let productSearchRevision=0;
function productMoney(amount,currency){const format=new Intl.NumberFormat(undefined,{style:'currency',currency});return format.format(amount/10**format.resolvedOptions().maximumFractionDigits);}
function productText(tag,text){const node=document.createElement(tag);node.textContent=text;return node;}
function productLink(text,href,external=false){const a=productText('a',text);a.href=href;if(external){a.target='_blank';a.rel='noopener noreferrer';}return a;}
function drawProductResults(result){
  const panel=document.getElementById('productResults');panel.replaceChildren();
  panel.append(productText('h3','Merchant inventory matches'));
  for(const store of result.stores){
    const article=document.createElement('article');article.append(productText('h3',store.name),productText('p',`${store.address} · ${store.distance_km} km from your location`));
    for(const item of store.products)article.append(productText('p',`${item.name} · ${productMoney(item.price_minor,store.currency)} · ${item.quantity>0?item.quantity+' available':'Out of stock'}`));
    const parameters=new URLSearchParams({store:store.id});if(demoPrefix)parameters.set('demo',demoId);
    article.append(productLink('Enter store and shop →','/marketplace.html?'+parameters));panel.append(article);
  }
  if(!result.stores.length)panel.append(productText('p','No matching merchant inventory was found within 10 km.'));
  panel.append(productText('p',result.catalog_note));
  if(result.online_error)panel.append(productText('p',result.online_error));
  if(result.findings.length){panel.append(productText('h3','Possible online sellers — inventory unconfirmed'));
    for(const store of result.findings){const article=document.createElement('article');article.append(productText('h3',store.name),productText('p',`${store.address} · ${store.distance_km} km`),productText('p',store.match_note),productText('p',store.inventory_status));
      article.append(productLink('View listing source',store.source_url,true));
      const accept=productText('button','Select this store'),decline=productText('button','Decline');accept.type=decline.type='button';
      decline.addEventListener('click',()=>article.remove());
      accept.addEventListener('click',()=>{const view=document.getElementById('externalStoreView');view.replaceChildren(productText('h3',store.name),productText('p',store.address),productText('p','Online listing selected. Review the source-listed website before entering personal information. Products, current prices and purchases are handled by that store. Global POS has no connected inventory or order integration for this listing.'));
        if(store.website_url)view.append(productLink('Open store website to browse and shop ↗',store.website_url,true));else view.append(productText('p','No secure store website is listed. Check the source or contact the store.'));
        view.hidden=false;view.scrollIntoView({behavior:'smooth',block:'start'});});
      article.append(accept,decline);panel.append(article);
    }
    panel.append(productLink('© OpenStreetMap contributors · ODbL','https://www.openstreetmap.org/copyright',true));
    panel.append(productText('p','Listings are limited by source coverage. Suggested categories do not confirm this product is sold.'));
  }
}
document.getElementById('productSearchForm').addEventListener('submit',async event=>{
  event.preventDefault();const revision=++productSearchRevision,form=event.currentTarget,button=form.querySelector('button'),status=document.getElementById('productSearchStatus');button.disabled=true;status.textContent='Finding your location and nearby sellers…';document.getElementById('externalStoreView').hidden=true;
  try{
    if(!navigator.geolocation)throw Error('Device location is unavailable. Browse stores by address below.');
    const position=await new Promise((resolve,reject)=>navigator.geolocation.getCurrentPosition(resolve,()=>reject(Error('Location permission is needed for nearby product search. You can still find stores by address below.')),{timeout:10000,maximumAge:60000}));
    const response=await fetch('/api/'+demoPrefix+'product-search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:form.elements.query.value,lat:position.coords.latitude,lon:position.coords.longitude,online:form.elements.online.checked}),cache:'no-store'});
    const result=await response.json();if(!response.ok)throw Error(result.error||'Search failed');if(revision!==productSearchRevision)return;drawProductResults(result);status.textContent=`${result.stores.length} stores with matching inventory, ${result.findings.length} possible online sellers. Search area: 10 km.`;
  }catch(error){if(revision===productSearchRevision)status.textContent=error.message;}finally{button.disabled=false;}
});
