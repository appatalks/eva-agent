// idb-store.js — IndexedDB storage backend for sessions and blobs
// Replaces localStorage for session snapshots. Supports binary blobs (images, audio).

var EVA_IDB_NAME = 'eva_sessions_db';
var EVA_IDB_VERSION = 1;

var _evaDB = null;

/** Open (or create) the IndexedDB database. Returns a Promise<IDBDatabase>. */
function _evaIdbOpen() {
  if (_evaDB) return Promise.resolve(_evaDB);
  return new Promise(function(resolve, reject) {
    var req = indexedDB.open(EVA_IDB_NAME, EVA_IDB_VERSION);
    req.onupgradeneeded = function(e) {
      var db = e.target.result;
      if (!db.objectStoreNames.contains('sessions')) {
        db.createObjectStore('sessions', { keyPath: 'id' });
      }
      if (!db.objectStoreNames.contains('blobs')) {
        var blobStore = db.createObjectStore('blobs', { keyPath: 'id' });
        blobStore.createIndex('sessionId', 'sessionId', { unique: false });
      }
    };
    req.onsuccess = function(e) {
      _evaDB = e.target.result;
      resolve(_evaDB);
    };
    req.onerror = function(e) {
      console.error('[IDB] Open error:', e.target.error);
      reject(e.target.error);
    };
  });
}

/** Generic IDB put. Returns Promise<void>. */
function _evaIdbPut(storeName, value) {
  return _evaIdbOpen().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(storeName, 'readwrite');
      tx.objectStore(storeName).put(value);
      tx.oncomplete = function() { resolve(); };
      tx.onerror = function(e) { reject(e.target.error); };
    });
  });
}

/** Generic IDB get. Returns Promise<value|undefined>. */
function _evaIdbGet(storeName, key) {
  return _evaIdbOpen().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(storeName, 'readonly');
      var req = tx.objectStore(storeName).get(key);
      req.onsuccess = function() { resolve(req.result); };
      req.onerror = function(e) { reject(e.target.error); };
    });
  });
}

/** Generic IDB delete. Returns Promise<void>. */
function _evaIdbDelete(storeName, key) {
  return _evaIdbOpen().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(storeName, 'readwrite');
      tx.objectStore(storeName).delete(key);
      tx.oncomplete = function() { resolve(); };
      tx.onerror = function(e) { reject(e.target.error); };
    });
  });
}

/** Get all keys from a store. Returns Promise<string[]>. */
function _evaIdbAllKeys(storeName) {
  return _evaIdbOpen().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(storeName, 'readonly');
      var req = tx.objectStore(storeName).getAllKeys();
      req.onsuccess = function() { resolve(req.result || []); };
      req.onerror = function(e) { reject(e.target.error); };
    });
  });
}

// ---------- Session-level helpers ----------

/** Save a session snapshot to IndexedDB. */
function idbSaveSession(id, snapshot) {
  var record = Object.assign({}, snapshot, { id: id });
  return _evaIdbPut('sessions', record);
}

/** Load a session snapshot from IndexedDB. Returns Promise<object|undefined>. */
function idbLoadSession(id) {
  return _evaIdbGet('sessions', id);
}

/** Delete a session and its associated blobs from IndexedDB. */
function idbDeleteSession(id) {
  return _evaIdbOpen().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(['sessions', 'blobs'], 'readwrite');
      // Delete the session
      tx.objectStore('sessions').delete(id);
      // Delete associated blobs via index
      var blobStore = tx.objectStore('blobs');
      var idx = blobStore.index('sessionId');
      var cursorReq = idx.openKeyCursor(IDBKeyRange.only(id));
      cursorReq.onsuccess = function(e) {
        var cursor = e.target.result;
        if (cursor) {
          blobStore.delete(cursor.primaryKey);
          cursor.continue();
        }
      };
      tx.oncomplete = function() { resolve(); };
      tx.onerror = function(e) { reject(e.target.error); };
    });
  });
}

// ---------- Blob helpers ----------

/** Save a blob (image/audio) associated with a session. Returns the blob ID. */
function idbSaveBlob(sessionId, blob, mimeType) {
  var blobId = 'blob_' + Date.now() + '_' + Math.random().toString(36).substring(2, 6);
  return _evaIdbPut('blobs', {
    id: blobId,
    sessionId: sessionId,
    type: mimeType || blob.type || 'application/octet-stream',
    data: blob,
    created: Date.now()
  }).then(function() { return blobId; });
}

/** Load a blob by ID. Returns Promise<{id, sessionId, type, data, created}|undefined>. */
function idbGetBlob(blobId) {
  return _evaIdbGet('blobs', blobId);
}

/** Convert a data URL (base64) to a Blob. */
function dataUrlToBlob(dataUrl) {
  var parts = dataUrl.split(',');
  var mime = parts[0].match(/:(.*?);/)[1];
  var raw = atob(parts[1]);
  var arr = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

/** Convert a Blob to a data URL. Returns Promise<string>. */
function blobToDataUrl(blob) {
  return new Promise(function(resolve, reject) {
    var reader = new FileReader();
    reader.onloadend = function() { resolve(reader.result); };
    reader.onerror = function() { reject(reader.error); };
    reader.readAsDataURL(blob);
  });
}

// ---------- Migration ----------

/** Migrate existing localStorage sessions to IndexedDB (one-time). */
function idbMigrateFromLocalStorage() {
  var migrated = localStorage.getItem('_idb_migrated');
  if (migrated) return Promise.resolve();

  var index;
  try { index = JSON.parse(localStorage.getItem('eva_sessions')) || []; }
  catch(e) { index = []; }

  if (index.length === 0) {
    localStorage.setItem('_idb_migrated', '1');
    return Promise.resolve();
  }

  var promises = [];
  index.forEach(function(entry) {
    var raw = localStorage.getItem('session_' + entry.id);
    if (!raw) return;
    try {
      var data = JSON.parse(raw);
      promises.push(idbSaveSession(entry.id, data).then(function() {
      }));
    } catch(e) {}
  });

  return Promise.all(promises).then(function() {
    localStorage.setItem('_idb_migrated', '1');
    console.log('[IDB] Migrated ' + promises.length + ' sessions from localStorage');
  }).catch(function(e) {
    console.error('[IDB] Migration error:', e);
  });
}

// Request persistent storage so the browser doesn't evict our data
if (navigator.storage && navigator.storage.persist) {
  navigator.storage.persist().then(function(granted) {
    if (granted) console.log('[IDB] Persistent storage granted');
  });
}
