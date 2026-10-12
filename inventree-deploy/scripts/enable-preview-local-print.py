#!/usr/bin/env python3
"""Enable the local-print acceptance instance using an existing test identity.

Keeps the previous container for rollback. Never changes production containers.
Private key material stays outside the repository and is mounted read-only.
"""
import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    root = Path(__file__).resolve().parents[2]
    identity = Path.home() / '.wms-local-print-preview'
    if (identity / 'active').is_dir():
        identity = (identity / 'active').resolve()
    build = root / 'tmp/local-print-build'
    assert (build / '.vite/manifest.json').is_file()
    for filename in ['private-key.pem', 'digital-certificate.txt']:
        assert (identity / filename).is_file()
    source = json.loads(run('docker', 'inspect', 'wms-preview'))[0]
    env = dict(v.split('=', 1) for v in source['Config']['Env'])
    assert env['INVENTREE_DB_NAME'] == 'wms_inventory_acceptance'
    assert env['INVENTREE_CACHE_DB'] == '1'
    assert env['INVENTREE_STATIC_ROOT'] == '/tmp/wms-static'
    env.update({
        'INVENTREE_LOCAL_PRINT_ENABLED': 'True',
        'INVENTREE_LOCAL_PRINT_QZ_VERSION': '2.2.5',
        'INVENTREE_LOCAL_PRINT_DOWNLOAD_URL': env['INVENTREE_SITE_URL'] + '/local-print-setup/',
        'INVENTREE_LOCAL_PRINT_CERTIFICATE': '/run/wms-print/digital-certificate.txt',
        'INVENTREE_LOCAL_PRINT_PRIVATE_KEY': '/run/wms-print/private-key.pem',
    })
    targets = ['/run/wms-print/digital-certificate.txt', '/run/wms-print/private-key.pem',
               '/home/inventree/src/backend/InvenTree/web/static/web']
    source['HostConfig']['Binds'] = [b for b in source['HostConfig']['Binds']
                                     if b.split(':')[1] not in targets]
    source['HostConfig']['Binds'] += [
        f'{identity / "digital-certificate.txt"}:{targets[0]}:ro',
        f'{identity / "private-key.pem"}:{targets[1]}:ro',
        f'{build}:{targets[2]}:ro',
    ]
    spec = importlib.util.spec_from_file_location('preview_worker', Path(__file__).with_name('enable-preview-worker.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    backup = 'wms-preview-before-local-print-' + datetime.now().strftime('%Y%m%d%H%M%S')
    run('docker', 'stop', '--time', '30', 'wms-preview')
    run('docker', 'rename', 'wms-preview', backup)
    try:
        helper.create('wms-preview', source, env, source['Config']['Cmd'])
        run('docker', 'start', 'wms-preview')
    except Exception:
        subprocess.run(['docker', 'rm', '-f', 'wms-preview'], capture_output=True)
        run('docker', 'rename', backup, 'wms-preview')
        run('docker', 'start', 'wms-preview')
        raise
    print('Previous container retained:', backup)
    print('Test site:', env['INVENTREE_SITE_URL'])


if __name__ == '__main__':
    main()
