#!/usr/bin/env python3
"""Give the existing port-18000 preview its own Redis namespace and worker.

Reuses the preview's image, source/data mounts, database and LAN settings.
Never reads credentials into output or changes the production containers.
"""
import json
import subprocess
from datetime import datetime
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def environment(container):
    return dict(value.split('=', 1) for value in container['Config']['Env'])


def create(name, source, env, command, web=False):
    args = ['docker', 'create', '--name', name, '--restart', 'unless-stopped',
            '--network', source['HostConfig']['NetworkMode'],
            '-w', source['Config']['WorkingDir'], '--entrypoint', '/bin/sh']
    if web:
        for port, bindings in source['HostConfig']['PortBindings'].items():
            for binding in bindings or []:
                args.extend(['-p', f"{binding['HostIp'] or '0.0.0.0'}:{binding['HostPort']}:{port}"])
    for key, value in env.items():
        args.extend(['-e', key + '=' + value])
    for bind in source['HostConfig']['Binds'] or []:
        args.extend(['-v', bind])
    for host in source['HostConfig'].get('ExtraHosts') or []:
        args.extend(['--add-host', host])
    if source['Config']['User']:
        args.extend(['--user', source['Config']['User']])
    args.extend([source['Image'], *command])
    run(*args)


def main():
    source = inspect('wms-preview')
    env = environment(source)
    assert env.get('INVENTREE_DB_NAME') == 'wms_inventory_acceptance'
    bindings = source['HostConfig']['PortBindings'] or {}
    if bindings:
        assert bindings['8000/tcp'][0]['HostPort'] == '18000'
    else:
        proxy = inspect('wms-preview-proxy')
        assert proxy['HostConfig']['PortBindings']['8000/tcp'][0]['HostPort'] == '18000'
    env['INVENTREE_CACHE_DB'] = '1'
    names = run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines()
    if 'wms-preview-worker' in names:
        worker = inspect('wms-preview-worker')
        worker_env = environment(worker)
        assert worker_env.get('INVENTREE_DB_NAME') == env['INVENTREE_DB_NAME']
        assert worker_env.get('INVENTREE_CACHE_DB') == '1'
    else:
        # Redis DB 1 is reserved for this preview; refuse a namespace collision.
        assert run('docker', 'exec', 'inventree-cache', 'redis-cli', '-n', '1', 'DBSIZE') == '0'
        create('wms-preview-worker', source, env,
               ['-c', 'exec python manage.py qcluster'])

    if environment(source).get('INVENTREE_CACHE_DB') != '1' or bindings:
        backup = 'wms-preview-before-worker-' + datetime.now().strftime('%Y%m%d%H%M%S')
        run('docker', 'stop', 'wms-preview')
        run('docker', 'rename', 'wms-preview', backup)
        try:
            create('wms-preview', source, env, source['Config']['Cmd'])
            run('docker', 'start', 'wms-preview')
        except Exception:
            # Retain the original container for recovery; never touch database data.
            subprocess.run(['docker', 'rm', '-f', 'wms-preview'], capture_output=True)
            run('docker', 'rename', backup, 'wms-preview')
            run('docker', 'start', 'wms-preview')
            raise
        print('Previous web container retained as:', backup)
    if 'wms-preview-proxy' not in names:
        media_mount = next(m for m in source['Mounts'] if m['Destination'] == '/home/inventree/data')
        proxy_image = inspect('inventree-proxy')['Image']
        config_path = Path(__file__).resolve().parents[1] / 'Caddyfile.preview'
        args = ['docker', 'create', '--name', 'wms-preview-proxy',
                '--restart', 'unless-stopped', '--network', source['HostConfig']['NetworkMode'],
                '-p', '0.0.0.0:18000:8000',
                '-v', str(config_path) + ':/etc/caddy/Caddyfile:ro',
                '-v', media_mount['Source'] + '/media:/var/www/media:ro', proxy_image]
        run(*args)
    run('docker', 'start', 'wms-preview-proxy')
    run('docker', 'start', 'wms-preview-worker')
    print('Preview web and worker now share preview database and Redis DB 1.')


if __name__ == '__main__':
    main()
