import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import qz from 'qz-tray';
import {
  LocalPrintClient,
  type LocalPrintJob,
  type QzDriver,
  printErrorKind,
  validSavedQueue
} from '../../src/functions/printing/LocalPrintClient.ts';

const job: LocalPrintJob = {
  id: 'a0245c72-544c-4faa-a09d-7e79484971e6',
  state: 'ready',
  model_type: 'part',
  items: [1],
  copies: 3,
  queue: '标签打印机',
  total: 3,
  detail: '',
  file_sha256: 'digest'
};
const options = {
  bounds: null,
  colorType: 'color',
  copies: 1,
  density: 0,
  duplex: false,
  fallbackDensity: null,
  interpolation: 'bicubic',
  jobName: `WMS-${job.id}`,
  legacy: false,
  margins: 0,
  orientation: null,
  paperThickness: null,
  printerTray: null,
  rasterize: false,
  rotation: 0,
  scaleContent: false,
  size: { width: 50, height: 30 },
  units: 'mm',
  forceRaw: false,
  encoding: null,
  spool: null
};

function fixture() {
  let currentSession: string | undefined = 'user-1';
  let active = true;
  let version = '2.2.5';
  let printers = [job.queue];
  let signatureHandler: any;
  let printHandler = async () => undefined;
  let signHook = async () => undefined;
  let connectHook = async () => undefined;
  const calls = {
    connects: 0,
    prints: 0,
    results: [] as string[],
    signed: [] as any[]
  };
  const driver: QzDriver = {
    websocket: {
      isActive: () => active,
      connect: async (config: any) => {
        calls.connects++;
        assert.deepEqual(config.port.insecure, []);
        await connectHook();
        active = true;
      },
      disconnect: async () => {
        active = false;
      }
    },
    security: {
      setCertificatePromise: (_handler, config) =>
        assert.equal(config.rejectOnFailure, true),
      setSignatureAlgorithm: (value) => assert.equal(value, 'SHA512'),
      setSignaturePromise: (handler) => {
        signatureHandler = handler;
      }
    },
    api: { getVersion: async () => version },
    printers: {
      find: async (_query, _signature, timestamp) => {
        await resolveSignature(
          JSON.stringify({ call: 'printers.find', params: {}, timestamp })
        );
        return printers;
      }
    },
    configs: {
      create: (queue, opts) => ({
        getPrinter: () => ({ name: queue }),
        getOptions: () => opts
      })
    },
    print: async (config, data, _signature, timestamp) => {
      await resolveSignature(
        JSON.stringify({
          call: 'print',
          params: {
            printer: config.getPrinter(),
            options: config.getOptions(),
            data
          },
          timestamp
        })
      );
      calls.prints++;
      await printHandler();
    }
  };
  async function resolveSignature(message: string) {
    const digest = createHash('sha256').update(message).digest('hex');
    return new Promise<string>(signatureHandler(digest));
  }
  const api = {
    get: async () => ({ data: { data: 'JVBERi0=', options } }),
    post: async (path: string, body: any) => {
      if (path.endsWith('/sign/')) {
        calls.signed.push(JSON.parse(body.message));
        if (body.job) await signHook();
        return { data: { signature: 'signed' } };
      }
      calls.results.push(body.state);
      return { data: { ...job, state: body.state } };
    }
  };
  return {
    client: new LocalPrintClient(driver, api, () => currentSession, 50),
    calls,
    logout: () => {
      currentSession = undefined;
    },
    version: (value: string) => {
      version = value;
    },
    printers: (values: string[]) => {
      printers = values;
    },
    onPrint: (value: typeof printHandler) => {
      printHandler = value;
    },
    onSign: (value: typeof signHook) => {
      signHook = value;
    },
    onConnect: (value: typeof connectHook) => {
      active = false;
      connectHook = value;
    },
    resolveSignature
  };
}

test('saved queues never silently fall back to a default printer', () => {
  assert.equal(validSavedQueue('removed', ['other']), null);
  assert.equal(validSavedQueue(null, ['other']), null);
  assert.equal(validSavedQueue('other', ['other']), 'other');
});

test('connection is shared and unsupported versions disconnect', async () => {
  const f = fixture();
  f.onConnect(async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
  });
  await Promise.all([f.client.connect('2.2.5'), f.client.connect('2.2.5')]);
  assert.equal(f.calls.connects, 1);
  f.version('2.1.0');
  await assert.rejects(f.client.connect('2.2.5'), /version/);
});

test('copies stay one, PDF is base64, and queue submission is acknowledged', async () => {
  const f = fixture();
  assert.equal((await f.client.submit(job)).state, 'submitted');
  assert.equal(f.calls.prints, 1);
  const message = f.calls.signed.find((call) => call.call === 'print');
  assert.deepEqual(message.params.printer, { name: job.queue });
  assert.equal(message.params.options.copies, 1);
  assert.equal(message.params.data[0].flavor, 'base64');
  assert.deepEqual(f.calls.results, ['submitted']);
  await assert.rejects(f.resolveSignature('arbitrary operation'), /Unapproved/);
});

test('double clicks cannot send concurrently', async () => {
  const f = fixture();
  let release!: () => void;
  f.onPrint(
    () =>
      new Promise((resolve) => {
        release = () => resolve(undefined);
      })
  );
  const first = f.client.submit(job);
  await assert.rejects(f.client.submit(job), /already submitting/);
  while (!release) await new Promise((resolve) => setTimeout(resolve, 1));
  release();
  await first;
  assert.equal(f.calls.prints, 1);
});

test('removed queue is rejected before print authorization', async () => {
  const f = fixture();
  f.printers(['other']);
  await assert.rejects(f.client.submit(job), /removed/);
  assert.equal(f.calls.prints, 0);
  assert.equal(
    f.calls.signed.some((message) => message.call === 'print'),
    false
  );
});

test('lost connection after send reports uncertainty without automatic retry', async () => {
  const f = fixture();
  f.onPrint(async () => {
    throw new Error('Connection closed before response');
  });
  await assert.rejects(f.client.submit(job), /Connection closed/);
  assert.equal(f.calls.prints, 1);
  assert.deepEqual(f.calls.results, ['uncertain']);
});

test('hung queue submission times out with an uncertain result', async () => {
  const f = fixture();
  f.onPrint(() => new Promise(() => undefined));
  await assert.rejects(f.client.submit(job), /timeout/);
  assert.deepEqual(f.calls.results, ['uncertain']);
  assert.equal(f.calls.prints, 1);
});

test('logout during signing prevents send and prevents reporting as a new user', async () => {
  const f = fixture();
  f.onSign(async () => {
    f.logout();
  });
  await assert.rejects(f.client.submit(job), /session/);
  assert.equal(f.calls.prints, 0);
  assert.deepEqual(f.calls.results, []);
});

test('unmounted dialog stops an authorized operation before the device send', async () => {
  const f = fixture();
  let mounted = true;
  f.onSign(async () => {
    mounted = false;
  });
  await assert.rejects(
    f.client.submit(job, () => {
      if (!mounted) throw new Error('closed');
    }),
    /closed/
  );
  assert.equal(f.calls.prints, 0);
  assert.deepEqual(f.calls.results, ['uncertain']);
});

test('connection errors remain distinguishable', () => {
  assert.equal(printErrorKind(new Error('Certificate rejected')), 'trust');
  assert.equal(printErrorKind(new Error('SecurityError blocked')), 'browser');
  assert.equal(printErrorKind(new Error('timeout')), 'timeout');
  assert.equal(printErrorKind(new Error('Connection refused')), 'connection');
});

test('real QZ SDK sends exactly the authorized JSON digest and SHA512 algorithm', async () => {
  const wire: any[] = [];
  const authorized = new Map<string, string>();
  class Socket {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = 1;
    onopen?: (event: unknown) => void;
    onmessage?: (event: { data: string }) => void;
    onclose?: (event: unknown) => void;
    constructor(url: string) {
      assert.match(url, /^wss:\/\//);
      queueMicrotask(() => this.onopen?.({}));
    }
    send(raw: string) {
      if (raw === 'ping') return;
      const message = JSON.parse(raw);
      wire.push(message);
      let error: string | undefined;
      if (message.call === 'print' || message.call === 'printers.find') {
        const digest = createHash('sha256')
          .update(
            JSON.stringify({
              call: message.call,
              params: message.params,
              timestamp: message.timestamp
            })
          )
          .digest('hex');
        if (
          message.signature !== authorized.get(digest) ||
          message.signAlgorithm !== 'SHA512'
        )
          error = 'Invalid signature or algorithm';
      }
      queueMicrotask(() =>
        this.onmessage?.({
          data: JSON.stringify({
            uid: message.uid,
            error,
            result:
              message.call === 'getVersion'
                ? '2.2.5'
                : message.call === 'printers.find'
                  ? [job.queue]
                  : null
          })
        })
      );
    }
    close() {
      this.readyState = 3;
      this.onclose?.({});
    }
  }
  (qz.api as any).setWebSocketType(Socket);
  const driver = {
    ...qz,
    websocket: {
      ...qz.websocket,
      connect: (config: any) =>
        qz.websocket.connect({ ...config, keepAlive: 0 })
    }
  };
  const api = {
    get: async (path: string) => ({
      data: path.endsWith('certificate/')
        ? 'TEST CERTIFICATE'
        : { data: 'JVBERi0=', options }
    }),
    post: async (path: string, body: any) => {
      if (path.endsWith('/sign/')) {
        const parsed = JSON.parse(body.message);
        if (body.job) assert.deepEqual(parsed.params.options, options);
        const digest = createHash('sha256').update(body.message).digest('hex');
        authorized.set(digest, `test-signature-${digest}`);
        return { data: { signature: authorized.get(digest) } };
      }
      return { data: { ...job, state: body.state } };
    }
  };
  const client = new LocalPrintClient(driver, api, () => 'user-1', 1000);
  try {
    await client.connect('2.2.5');
    await client.submit(job);
    const printed = wire.filter((message) => message.call === 'print');
    assert.equal(printed.length, 1);
    assert.equal(printed[0].signAlgorithm, 'SHA512');
    assert.equal(printed[0].params.options.copies, 1);
  } finally {
    await client.disconnect();
  }
});
