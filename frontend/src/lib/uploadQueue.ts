export interface QueuedUpload {
  id: string;
  shelfId: string;
  name: string;
  type: string;
  lastModified: number;
  blob: Blob;
  createdAt: number;
}

const DB_NAME = 'calibrex-offline';
const STORE = 'uploads';

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: 'id' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Offline storage is unavailable.'));
  });
}

async function transaction<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const request = run(tx.objectStore(STORE));
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Offline upload storage failed.'));
    tx.oncomplete = () => db.close();
    tx.onerror = () => { db.close(); reject(tx.error ?? new Error('Offline upload storage failed.')); };
  });
}

export async function queueUploads(shelfId: string, files: File[]): Promise<number> {
  await queueFiles(shelfId, files);
  return (await queuedUploads()).length;
}

export async function queueFiles(shelfId: string, files: File[], requestIds?: string[]): Promise<QueuedUpload[]> {
  if (requestIds && requestIds.length !== files.length) throw new Error('Send one request id per photo.');
  const saved: QueuedUpload[] = [];
  for (const [index, file] of files.entries()) {
    const item: QueuedUpload = { id: requestIds?.[index] ?? crypto.randomUUID(), shelfId, name: file.name, type: file.type,
      lastModified: file.lastModified, blob: file, createdAt: Date.now() };
    await transaction('readwrite', (store) => store.put(item));
    saved.push(item);
  }
  return saved;
}

export async function queuedUploads(): Promise<QueuedUpload[]> {
  return transaction('readonly', (store) => store.getAll());
}

export async function removeQueuedUpload(id: string): Promise<void> {
  await transaction('readwrite', (store) => store.delete(id));
}
