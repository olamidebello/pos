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


def check(domain, public_ip, managed):
    expected = ipaddress.ip_address(public_ip)
    if not expected.is_global:
        raise ValueError('Configure the real globally routable server public IP')
    addresses = {info[4][0] for info in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    if public_ip not in addresses:
        raise ValueError('DNS does not resolve to pos_public_ip: ' + ', '.join(sorted(addresses)))
    # Every published A/AAAA record must route to this server for HTTP-01 validation.
    listeners = subprocess.check_output(['ss', '-H', '-ltnp'], text=True)
    ports = conflicting_ports(listeners)
    if ports:
        raise ValueError('Ports 80/443 have non-Nginx or unidentified owners. Reconfigure explicitly; no services were stopped:\n' + '\n'.join(ports))
    if shutil.which('nginx'):
        result = subprocess.run(['nginx', '-T'], text=True, capture_output=True)
        if result.returncode:
            raise ValueError('Existing Nginx configuration is invalid: ' + result.stderr)
        conflicts = duplicate_hosts(result.stdout, domain, managed)
        if conflicts:
            raise ValueError('Hostname already configured outside the managed vhost: ' + ', '.join(conflicts))
    print(json.dumps({'domain': domain, 'addresses': sorted(addresses), 'web_ports': 'available or owned by nginx'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('domain')
    parser.add_argument('public_ip')
    parser.add_argument('managed_vhost')
    args = parser.parse_args()
    try:
        check(args.domain, args.public_ip, args.managed_vhost)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + '\n')
