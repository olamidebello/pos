"""Read-only SSL checks for a dedicated, existing Nginx hostname."""
import argparse
import ipaddress
import json
import re
import socket
import ssl
import subprocess
from pathlib import Path


def check_dns(domain, public_ip, additional, resolve=socket.getaddrinfo):
    expected={str(ipaddress.ip_address(x)) for x in [public_ip,*additional]}
    if not all(ipaddress.ip_address(x).is_global for x in expected):
        raise ValueError('Real globally routable public IP addresses are required')
    addresses={str(ipaddress.ip_address(x[4][0])) for x in resolve(domain,80,type=socket.SOCK_STREAM)}
    if not addresses or not addresses<=expected:
        raise ValueError('DNS A/AAAA records must all route to this server; unexpected: '+', '.join(sorted(addresses-expected)))
    return sorted(addresses)


def vhost(output, domain):
    source='';files=set();matches=0
    for line in output.splitlines():
        match=re.match(r'# configuration file (.*):$',line)
        if match:source=match[1]
        for names in re.findall(r'\bserver_name\s+([^;]+);',line.split('#',1)[0]):
            names={n.strip('"\'') for n in names.split()}
            if domain in names:
                if names!={domain}:raise ValueError('Hostname shares a server block with other names; split the vhost before automated certificate changes')
                files.add(str(Path(source).resolve()));matches+=1
    if not files or len(files)!=1 or matches>2:
        raise ValueError('Expected one dedicated existing vhost file with HTTP/HTTPS blocks for '+domain)
    return next(iter(files))


def main():
    p=argparse.ArgumentParser();p.add_argument('domain');p.add_argument('public_ip');p.add_argument('--additional-ip',action='append',default=[]);a=p.parse_args()
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}',a.domain):raise ValueError('Invalid hostname')
    addresses=check_dns(a.domain,a.public_ip,a.additional_ip)
    listeners=subprocess.check_output(['ss','-H','-ltnp'],text=True)
    for line in listeners.splitlines():
        if re.search(r':(?:80|443)\s',line) and '"nginx"' not in line:raise ValueError('Non-Nginx or unknown owner on a web port; no service was stopped')
    if not re.search(r':80\s',listeners):raise ValueError('Nginx must already serve port 80 for HTTP-01')
    dump=subprocess.run(['nginx','-T'],text=True,capture_output=True,check=True)
    config=vhost(dump.stdout,a.domain)
    cert=Path('/etc/letsencrypt/live')/a.domain/'fullchain.pem'
    if cert.exists():
        decoded=ssl._ssl._test_decode_cert(str(cert))
        names={value for kind,value in decoded.get('subjectAltName',[]) if kind=='DNS'}
        if names!={a.domain}:raise ValueError('Existing certificate covers additional names; refusing to shrink its coverage')
    print(json.dumps(dict(domain=a.domain,addresses=addresses,vhost=config)))


if __name__=='__main__':main()
