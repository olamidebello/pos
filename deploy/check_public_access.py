"""Read-only public-release checks under the configured Nginx worker identity."""
import argparse
import json
import os
import pathlib
import pwd
import re
import subprocess

REQUIRED=('index.html','healthz.json','style.css','admin.html','demo.html','install.html','marketplace.html','assets/icon-192.png','agreements/shopper-v1.md')


def worker_from_config(text):
    for line in text.splitlines():
        line=line.split('#',1)[0]
        match=re.match(r'^\s*user\s+([A-Za-z_][A-Za-z0-9_-]*)(?:\s|;)',line)
        if match:return match[1]
    raise ValueError('No explicit Nginx worker user was found; configure the worker account before deploying')


def check(release,worker):
    root=pathlib.Path(release)
    account=pwd.getpwnam(worker)
    for name in REQUIRED:
        path=root/name
        if not path.is_file():raise ValueError('Missing public release asset: '+str(path))
        if os.geteuid()==account.pw_uid:
            readable=os.access(path,os.R_OK) and all(os.access(p,os.X_OK) for p in path.parents)
        elif os.geteuid()==0:
            result=subprocess.run(['runuser','-u',worker,'--','test','-r',str(path)],capture_output=True)
            readable=result.returncode==0
        else:raise ValueError('Worker access checks must run as root or the configured worker user')
        if not readable:
            modes=[str(p)+'='+oct(p.stat().st_mode & 0o777) for p in [path,*path.parents] if p.exists()]
            raise ValueError('Nginx worker '+worker+' cannot read '+str(path)+'. Check directory traversal, file permissions and ACLs: '+', '.join(modes))
    return {'worker':worker,'release':str(root),'readable_assets':len(REQUIRED)}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('release');args=parser.parse_args()
    config=subprocess.run(['nginx','-T'],capture_output=True,text=True,check=True).stdout
    print(json.dumps(check(args.release,worker_from_config(config))))
