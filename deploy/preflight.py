"""Read-only DNS, web-port ownership and duplicate Nginx hostname checks."""
import argparse
import ipaddress
import json
import pathlib
import re
import shutil
import socket
import subprocess


def conflicting_ports(output):
    return [line for line in output.splitlines()
            if re.search(r':(?:80|443)\s', line) and '"nginx"' not in line]


def duplicate_hosts(output, domain, managed):
    source = ''
    conflicts = set()
    for line in output.splitlines():
        match = re.match(r'# configuration file (.*):$', line)
        if match:
            source = match.group(1)
        line = line.split('#', 1)[0]
        for match in re.finditer(r'\bserver_name\s+([^;]+);', line):
            names = [n.strip('"\'') for n in match.group(1).split()]
            if domain in names and source and pathlib.Path(source).resolve() != pathlib.Path(managed).resolve():
                conflicts.add(source)
    return sorted(conflicts)


def web_listeners(output, port):
    """Share existing web socket address groups; never inherit default_server."""
    values={str(port)}
    for line in output.splitlines():
        line=line.split('#',1)[0]
        for directive in re.findall(r'\blisten\s+([^;]+);',line):
            fields=directive.split();endpoint=fields[0]
            if endpoint in (str(port),'*:'+str(port),'0.0.0.0:'+str(port)):
                if 'proxy_protocol' in fields or (port==80 and 'ssl' in fields):raise ValueError('Existing web listener requires a nonstandard protocol; route ACME explicitly before deployment')
                continue
            match=re.fullmatch(r'\[([^]]+)\]:(\d+)',endpoint)
            if match:address,number=match.groups()
            else:
                address,separator,number=endpoint.rpartition(':')
                if not separator:continue
            if number!=str(port):continue
            try:ip=ipaddress.ip_address(address)
            except ValueError:raise ValueError('Web listener uses a hostname; configure explicit IP listener addresses before automated deployment')
            if 'proxy_protocol' in fields or (port==80 and 'ssl' in fields):
                raise ValueError('Existing web listener requires a nonstandard protocol; route ACME explicitly before deployment')
            values.add('['+str(ip)+']:'+number if ip.version==6 else str(ip)+':'+number)
    return sorted(values)


def check(domain, public_ip, managed, additional=()):
    expected = ipaddress.ip_address(public_ip)
    if not expected.is_global:
        raise ValueError('Configure the real globally routable server public IP')
    addresses = {info[4][0] for info in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    if public_ip not in addresses:
        raise ValueError('DNS does not resolve to pos_public_ip: ' + ', '.join(sorted(addresses)))
    interfaces=json.loads(subprocess.check_output(['ip','-j','address','show'],text=True))
    local={str(ipaddress.ip_address(item['local'])) for interface in interfaces for item in interface.get('addr_info',[]) if 'local' in item}
    allowed={str(expected),*(str(ipaddress.ip_address(x)) for x in additional),*local}
    unexpected={x for x in addresses if x not in allowed or not ipaddress.ip_address(x).is_global}
    if unexpected:raise ValueError('DNS A/AAAA records point away from this server: '+', '.join(sorted(unexpected))+'. Correct stale records or declare actual additional public IPs.')
    http_listeners=['80'];https_listeners=['443']
    # Every published A/AAAA record must route to this server for HTTP-01 validation.
    listeners = subprocess.check_output(['ss', '-H', '-ltnp'], text=True)
    ports = conflicting_ports(listeners)
    if ports:
        raise ValueError('Ports 80/443 have non-Nginx or unidentified owners. Reconfigure explicitly; no services were stopped:\n' + '\n'.join(ports))
    if shutil.which('nginx'):
        result = subprocess.run(['nginx', '-T'], text=True, capture_output=True)
        if result.returncode:
            raise ValueError('Existing Nginx configuration is invalid: ' + result.stderr)
        http_listeners=web_listeners(result.stdout,80)
        https_listeners=web_listeners(result.stdout,443)
        conflicts = duplicate_hosts(result.stdout, domain, managed)
        if conflicts:
            raise ValueError('Hostname already configured outside the managed vhost: ' + ', '.join(conflicts))
    if any(ipaddress.ip_address(x).version==6 for x in addresses):
        if '[::]:80' not in http_listeners:http_listeners.append('[::]:80')
        if '[::]:443' not in https_listeners:https_listeners.append('[::]:443')
    probes={'127.0.0.1'}
    for endpoint in http_listeners:
        if endpoint=='[::]:80':probes.add('[::1]')
        elif endpoint!='80':
            address=endpoint.rsplit(':',1)[0].strip('[]')
            if address in local:probes.add('['+address+']' if ':' in address else address)
    print(json.dumps({'domain': domain, 'addresses': sorted(addresses), 'web_ports': 'available or owned by nginx', 'http_listeners':http_listeners, 'https_listeners':https_listeners, 'probe_addresses':sorted(probes)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('domain')
    parser.add_argument('public_ip')
    parser.add_argument('managed_vhost')
    parser.add_argument('--additional-ip',action='append',default=[])
    args = parser.parse_args()
    try:
        check(args.domain, args.public_ip, args.managed_vhost,args.additional_ip)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + '\n')
