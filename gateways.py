"""Hosted payment adapters. Credentials are environment references, never browser input."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

PROVIDERS = ('stripe', 'paystack', 'flutterwave')
EXPONENTS = {'USD':2,'NGN':2,'GBP':2,'EUR':2,'GHS':2,'KES':2,'ZAR':2,'CAD':2,'AUD':2,'JPY':0}

class GatewayError(ValueError): pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def secret(config):
    ref=config['secret_ref'];provider=config['provider'];mode=config['mode']
    if not re.fullmatch('GLOBALPOS_'+provider.upper()+'_'+mode.upper()+r'_[A-Z0-9_]{1,80}',ref):
        raise GatewayError('Use a provider/mode-specific server secret reference')
    value=os.environ.get(ref,'')
    if not value: raise GatewayError('Gateway secret is not configured on the server')
    if provider in ('stripe','paystack'):
        if not value.startswith('sk_'+mode+'_'):raise GatewayError('Gateway key does not match the configured mode')
    elif provider=='flutterwave':
        is_test=value.startswith('FLWSECK_TEST-')
        if not value.startswith('FLWSECK') or is_test!=(mode=='test'):raise GatewayError('Flutterwave key does not match the configured mode')
    else:raise GatewayError('Unsupported provider')
    return value


def request(provider, path, key, data=None, form=False, idempotency=None):
    origins={'stripe':'https://api.stripe.com/v1','paystack':'https://api.paystack.co','flutterwave':'https://api.flutterwave.com/v3'}
    body=(urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()) if data is not None else None
    headers={'Authorization':'Bearer '+key,'Accept':'application/json'}
    if body is not None:headers['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
    if idempotency:headers['Idempotency-Key']=idempotency
    req=urllib.request.Request(origins[provider]+path,data=body,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(req,timeout=20) as response:
            value=json.loads(response.read(1024*1024))
            if not isinstance(value,dict):raise ValueError('Invalid response')
            return value
    except (urllib.error.URLError,ValueError,TimeoutError,OSError):
        raise GatewayError('Provider request failed; the order remains pending reconciliation') from None


def checkout(config, order, email, return_url):
    key=secret(config);provider=config['provider'];currency=order['currency'];reference=order['reference']
    if currency not in EXPONENTS:raise GatewayError('Currency requires an explicit minor-unit configuration')
    if provider=='stripe':
        payload={'mode':'payment','success_url':return_url,'cancel_url':return_url,'customer_email':email,'client_reference_id':reference,'metadata[order_id]':order['id'],'line_items[0][quantity]':1,'line_items[0][price_data][currency]':currency.lower(),'line_items[0][price_data][unit_amount]':order['total_minor'],'line_items[0][price_data][product_data][name]':'Global POS store order '+order['id']}
        result=request(provider,'/checkout/sessions',key,payload,form=True,idempotency=reference)
        identifier=result.get('id');url=result.get('url')
    elif provider=='paystack':
        payload={'reference':reference,'amount':order['total_minor'],'currency':currency,'email':email,'callback_url':return_url,'metadata':{'order_id':order['id']}}
        result=request(provider,'/transaction/initialize',key,payload)
        if result.get('status') is not True:raise GatewayError('Paystack checkout was not initialized')
        identifier=reference;url=result.get('data',{}).get('authorization_url')
    else:
        major=str(Decimal(order['total_minor'])/(10**EXPONENTS[currency]))
        payload={'tx_ref':reference,'amount':major,'currency':currency,'redirect_url':return_url,'customer':{'email':email},'meta':{'order_id':order['id']}}
        result=request(provider,'/payments',key,payload)
        if result.get('status')!='success':raise GatewayError('Flutterwave checkout was not initialized')
        identifier=reference;url=result.get('data',{}).get('link')
    allowed={'stripe':'checkout.stripe.com','paystack':'checkout.paystack.com','flutterwave':'checkout.flutterwave.com'}
    parsed=urllib.parse.urlsplit(url or '')
    if not identifier or parsed.scheme!='https' or parsed.hostname!=allowed[provider] or parsed.username or parsed.password or parsed.port not in (None,443):
        raise GatewayError('Provider did not return a supported secure hosted checkout URL')
    return {'provider_id':identifier,'checkout_url':url}


def verify(config, order):
    key=secret(config);provider=config['provider'];reference=order['reference'];mode=config['mode']
    if provider=='stripe':
        if not re.fullmatch(r'cs_[A-Za-z0-9_]+',order['provider_id']):return False
        result=request(provider,'/checkout/sessions/'+order['provider_id'],key)
        return result.get('payment_status')=='paid' and result.get('client_reference_id')==reference and result.get('currency','').upper()==order['currency'] and result.get('amount_total')==order['total_minor'] and result.get('livemode')==(mode=='live')
    if provider=='paystack':
        response=request(provider,'/transaction/verify/'+urllib.parse.quote(reference,safe=''),key)
        data=response.get('data') or {}
        return response.get('status') is True and data.get('status')=='success' and data.get('reference')==reference and data.get('currency')==order['currency'] and data.get('amount')==order['total_minor'] and data.get('domain')==mode
    response=request(provider,'/transactions/verify_by_reference?tx_ref='+urllib.parse.quote(reference,safe=''),key)
    data=response.get('data') or {}
    try:minor=Decimal(str(data.get('amount')))*(10**EXPONENTS[order['currency']])
    except Exception:return False
    return response.get('status')=='success' and data.get('status')=='successful' and data.get('tx_ref')==reference and data.get('currency')==order['currency'] and minor==order['total_minor']
