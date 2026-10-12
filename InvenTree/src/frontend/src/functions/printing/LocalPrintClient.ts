/** Local queue adapter. No automatic retry is permitted after signing. */
export type LocalPrintJob = {
  id: string;
  state:
    | 'generating'
    | 'ready'
    | 'submitting'
    | 'submitted'
    | 'uncertain'
    | 'failed';
  model_type: string;
  items: number[];
  copies: number;
  item_copies?: Record<string, number>;
  queue: string;
  total: number;
  detail: string;
  file_sha256: string;
};

export type QzConfig = {
  getPrinter(): unknown;
  getOptions(): Record<string, unknown>;
};

export type QzDriver = {
  websocket: {
    isActive(): boolean;
    connect(options: unknown): Promise<void>;
    disconnect(): Promise<void>;
  };
  security: {
    setCertificatePromise(
      handler: (
        resolve: (value: string) => void,
        reject: (error: unknown) => void
      ) => void,
      options: { rejectOnFailure: boolean }
    ): void;
    setSignatureAlgorithm(value: string): void;
    setSignaturePromise(
      handler: (
        digest: string
      ) => (
        resolve: (signature: string) => void,
        reject: (error: Error) => void
      ) => void
    ): void;
  };
  api: { getVersion(): Promise<string> };
  printers: {
    find(
      query: undefined,
      signature: string | undefined,
      timestamp: number
    ): Promise<string[]>;
  };
  configs: {
    create(queue: string, options: Record<string, unknown>): QzConfig;
  };
  print(
    config: QzConfig,
    data: unknown[],
    signature: string | string[],
    timestamp: number
  ): Promise<void>;
};

export type PrintApi = {
  get(path: string): Promise<{ data: any }>;
  post(path: string, body: unknown): Promise<{ data: any }>;
};

export const LOCAL_PRINT_URL = '/api/label/local/';

export function validSavedQueue(
  saved: string | null,
  queues: string[]
): string | null {
  return saved && queues.includes(saved) ? saved : null;
}

export function printErrorKind(error: unknown): string {
  const text = String(
    error instanceof Error ? error.message : error
  ).toLowerCase();
  if (/session|login/.test(text)) return 'session';
  if (/version/.test(text)) return 'version';
  if (/denied|reject|trust|certificate/.test(text)) return 'trust';
  if (/security|blocked|notallowed/.test(text)) return 'browser';
  if (/timeout/.test(text)) return 'timeout';
  if (/queue/.test(text)) return 'queue';
  return 'connection';
}

export class LocalPrintClient {
  private connecting?: Promise<void>;
  private busy = false;
  private generation = 0;
  private signatures = new Map<string, string>();

  constructor(
    private qz: QzDriver,
    private api: PrintApi,
    private session: () => string | undefined,
    private timeoutMs = 15000
  ) {
    qz.security.setSignatureAlgorithm('SHA512');
    qz.security.setSignaturePromise((digest) => (resolve, reject) => {
      const signature = this.signatures.get(digest);
      this.signatures.delete(digest);
      if (signature) resolve(signature);
      else reject(new Error('Unapproved QZ operation'));
    });
    qz.security.setCertificatePromise(
      (resolve, reject) => {
        const current = this.capture();
        api
          .get(`${LOCAL_PRINT_URL}certificate/`)
          .then(({ data }) => {
            this.check(current);
            resolve(data);
          })
          .catch(reject);
      },
      { rejectOnFailure: true }
    );
  }

  private capture() {
    const session = this.session();
    if (!session) throw new Error('Login session expired');
    return `${session}:${this.generation}`;
  }

  private check(current: string) {
    if (this.capture() !== current) throw new Error('Login session changed');
  }

  private timed<T>(promise: Promise<T>): Promise<T> {
    let timer: ReturnType<typeof setTimeout>;
    return Promise.race([
      promise,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error('Local print timeout')),
          this.timeoutMs
        );
      })
    ]).finally(() => clearTimeout(timer));
  }

  async disconnect() {
    this.generation++;
    this.signatures.clear();
    if (this.qz.websocket.isActive()) await this.qz.websocket.disconnect();
  }

  async connect(version: string) {
    const current = this.capture();
    if (!this.connecting) {
      this.connecting = (async () => {
        if (!this.qz.websocket.isActive()) {
          // Empty insecure port list forbids the SDK's automatic ws fallback.
          await this.timed(
            this.qz.websocket
              .connect({
                host: ['localhost', 'localhost.qz.io'],
                usingSecure: true,
                port: { secure: [8181, 8282, 8383, 8484], insecure: [] },
                retries: 0
              })
              .then(async () => {
                try {
                  this.check(current);
                } catch (error) {
                  await this.disconnect();
                  throw error;
                }
              })
          );
        }
        const actual = await this.timed(this.qz.api.getVersion());
        this.check(current);
        if (actual !== version) {
          await this.disconnect();
          throw new Error(
            `Unsupported QZ version: ${actual}; expected ${version}`
          );
        }
      })()
        .catch(async (error) => {
          await this.disconnect().catch(() => undefined);
          throw error;
        })
        .finally(() => {
          this.connecting = undefined;
        });
    }
    await this.connecting;
    this.check(current);
  }

  async printers(): Promise<string[]> {
    const current = this.capture();
    const timestamp = Date.now();
    const message = JSON.stringify({
      call: 'printers.find',
      params: {},
      timestamp
    });
    const { data } = await this.api.post(`${LOCAL_PRINT_URL}sign/`, {
      message
    });
    this.check(current);
    const digest = await this.prepareSignature(message, data.signature);
    try {
      this.check(current);
      const queues = await this.timed(
        this.qz.printers.find(undefined, undefined, timestamp)
      );
      this.check(current);
      return queues;
    } finally {
      this.signatures.delete(digest);
    }
  }

  private async prepareSignature(message: string, signature: string) {
    const bytes = await crypto.subtle.digest(
      'SHA-256',
      new TextEncoder().encode(message)
    );
    const digest = Array.from(new Uint8Array(bytes), (byte) =>
      byte.toString(16).padStart(2, '0')
    ).join('');
    this.signatures.set(digest, signature);
    return digest;
  }

  async submit(
    job: LocalPrintJob,
    checkActive: () => void = () => undefined
  ): Promise<LocalPrintJob> {
    if (this.busy) throw new Error('Local print already submitting');
    if (job.state !== 'ready') throw new Error('Task is not ready');
    const current = this.capture();
    this.busy = true;
    let signing = false;
    let preparedDigest: string | undefined;
    try {
      const queues = await this.printers();
      checkActive();
      if (!queues.includes(job.queue))
        throw new Error('Selected queue was removed');
      const { data: pdf } = await this.api.get(
        `${LOCAL_PRINT_URL}jobs/${job.id}/pdf/`
      );
      this.check(current);
      checkActive();
      const config = this.qz.configs.create(job.queue, pdf.options);
      const data = [
        { type: 'pixel', format: 'pdf', flavor: 'base64', data: pdf.data }
      ];
      const timestamp = Date.now();
      const message = JSON.stringify({
        call: 'print',
        params: {
          printer: config.getPrinter(),
          options: config.getOptions(),
          data
        },
        timestamp
      });
      signing = true;
      const signed = await this.api.post(`${LOCAL_PRINT_URL}sign/`, {
        job: job.id,
        message
      });
      const digest = await this.prepareSignature(
        message,
        signed.data.signature
      );
      preparedDigest = digest;
      this.check(current);
      checkActive();
      if (!this.qz.websocket.isActive())
        throw new Error('Local connection closed');
      try {
        // QZ 2.2.5 omits signAlgorithm on its pre-signed path. The constrained
        // callback supplies only this exact preauthorized digest and adds SHA512.
        await this.timed(this.qz.print(config, data, [], timestamp));
      } finally {
        this.signatures.delete(digest);
      }
      this.check(current);
      const result = await this.api.post(
        `${LOCAL_PRINT_URL}jobs/${job.id}/result/`,
        { state: 'submitted' }
      );
      return result.data;
    } catch (error) {
      if (
        signing &&
        this.session() &&
        current === `${this.session()}:${this.generation}`
      ) {
        await this.api
          .post(`${LOCAL_PRINT_URL}jobs/${job.id}/result/`, {
            state: 'uncertain'
          })
          .catch(() => undefined);
      }
      throw error;
    } finally {
      if (preparedDigest) this.signatures.delete(preparedDigest);
      this.busy = false;
    }
  }
}
