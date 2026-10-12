#!/usr/bin/env python3
"""Add HTTPS on 18443 to the existing preview, preserving its HTTP entry.

Uses the existing Caddy CA/storage so previously trusted devices keep working.
Only the public root certificate is exported to the download directory.
"""
import ipaddress
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def environment(container):
    return dict(v.split('=', 1) for v in container['Config']['Env'])


def main():
    host = str(ipaddress.ip_address(sys.argv[1] if len(sys.argv) > 1 else run('ipconfig', 'getifaddr', 'en0')))
    names = ['wms-preview', 'wms-preview-worker', 'wms-preview-proxy']
    sources = {name: inspect(name) for name in names}
    production = inspect('inventree-proxy')
    ca_parent = next(m['Source'] for m in production['Mounts'] if m['Destination'] == '/data')
    ca_storage = Path(ca_parent) / 'caddy'
    root = ca_storage / 'pki/authorities/local/root.crt'
    assert root.is_file(), 'Existing Caddy CA is required'
    public = DEPLOY / 'certificates/public'
    public.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root, public / 'wms-ca.crt')
    site = f'https://{host}:18443'
    origins = {f'{scheme}://{name}:{port}' for name in [host, 'localhost', '127.0.0.1']
               for scheme, port in [('http', 18000), ('https', 18443)]}

    desired_env = {}
    for name in names[:2]:
        env = environment(sources[name])
        assert env.get('INVENTREE_DB_NAME') == 'wms_inventory_acceptance'
        assert env.get('INVENTREE_CACHE_DB') == '1'
        env['INVENTREE_SITE_URL'] = site
        if env.get('INVENTREE_LOCAL_PRINT_DOWNLOAD_URL', '').endswith('/local-print-setup/'):
            env['INVENTREE_LOCAL_PRINT_DOWNLOAD_URL'] = site + '/local-print-setup/'
        env['INVENTREE_SITE_HOST'] = host
        env['INVENTREE_TRUSTED_ORIGINS'] = ','.join(sorted(origins | set(env.get('INVENTREE_TRUSTED_ORIGINS', '').split(',')) - {''}))
        env['INVENTREE_USE_X_FORWARDED_PROTO'] = 'True'
        desired_env[name] = env
    desired_env[names[2]] = {**environment(sources[names[2]]), 'WMS_PREVIEW_HOST': host}

    proxy_binds = [b for b in sources[names[2]]['HostConfig']['Binds']
                   if b.split(':')[1] not in ['/data/caddy', '/var/www/ca']]
    proxy_binds += [f'{ca_storage}:/data/caddy', f'{public}:/var/www/ca:ro']
    proxy = sources[names[2]]
    # Validate before interrupting either web or worker.
    validation = ['docker', 'run', '--rm', '--network', proxy['HostConfig']['NetworkMode'],
                  '-e', f'WMS_PREVIEW_HOST={host}']
    for bind in proxy_binds:
        validation += ['-v', bind]
    validation += [proxy['Image'], 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile']
    run(*validation)

    replaced = []
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    try:
        for name in names:
            source = sources[name]
            is_proxy = name == names[2]
            binds = proxy_binds if is_proxy else source['HostConfig']['Binds']
            ports = {'8000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '18000'}],
                     '8443/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '18443'}]} if is_proxy else {}
            if (environment(source) == desired_env[name]
                    and set(source['HostConfig']['Binds']) == set(binds)
                    and (source['HostConfig']['PortBindings'] or {}) == ports):
                continue
            backup = f'{name}-before-https-{stamp}'
            run('docker', 'stop', '--time', '30', name)
            run('docker', 'rename', name, backup)
            replaced.append((name, backup))
            args = ['docker', 'create', '--name', name, '--restart', 'unless-stopped',
                    '--network', source['HostConfig']['NetworkMode']]
            if source['Config']['WorkingDir']:
                args += ['-w', source['Config']['WorkingDir']]
            if source['Config']['User']:
                args += ['--user', source['Config']['User']]
            for bind in binds:
                args += ['-v', bind]
            for key, value in desired_env[name].items():
                args += ['-e', key + '=' + value]
            for port, bindings in ports.items():
                for binding in bindings:
                    args += ['-p', f"{binding['HostIp']}:{binding['HostPort']}:{port}"]
            for extra in source['HostConfig'].get('ExtraHosts') or []:
                args += ['--add-host', extra]
            entrypoint = source['Config']['Entrypoint'] or []
            if entrypoint:
                assert len(entrypoint) == 1
                args += ['--entrypoint', entrypoint[0]]
            args += [source['Image'], *(source['Config']['Cmd'] or [])]
            run(*args)
            run('docker', 'start', name)
            print('Updated:', name, '; previous container retained:', backup)
        if not replaced:
            run('docker', 'exec', names[2], 'caddy', 'reload', '--config', '/etc/caddy/Caddyfile')
    except Exception:
        for name, backup in reversed(replaced):
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
            run('docker', 'rename', backup, name)
            run('docker', 'start', name)
        raise
    print('HTTPS:', site)
    print('CA certificate:', f'http://{host}:18000/wms-ca.crt')


if __name__ == '__main__':
    main()
