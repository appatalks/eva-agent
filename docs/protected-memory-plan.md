# Protected Memory Plan

Status: first local implementation slice complete on the `protected-memory` branch.
The FIDO2 PRF adapter, Kusto encrypted schema, cloud-release confirmation, and
production hardening remain planned follow-up work.

## Existing asset storage

Eva currently has several unrelated storage mechanisms:

- Browser session storage uses IndexedDB database `eva_sessions_db`.
  - `sessions` stores chat snapshots keyed by session ID.
  - `blobs` stores native browser `Blob` values in the `data` field, with `id`,
    `sessionId`, MIME `type`, and creation time.
  - Deleting a session deletes its associated blobs.
  - This is session persistence, not semantic memory, and Eva does not add a
    separate encryption layer to these values.
- Generated files and other artifacts are ordinary filesystem files below
  `EVA_CONFIG_DIR/artifacts`, defaulting to
  `~/.config/eva-standalone/artifacts` on Linux. The bridge validates artifact
  names and confines file access to that directory.
- Imported local voice profiles are WAV files below
  `~/.local/share/eva/local-voices/voices` on Linux. Bundled profiles live in
  `core/audio`.
- Electron API credentials use `safeStorage` and are written as encrypted
  values in the user data file `auth.enc.json`. This is an existing example of
  OS-backed encryption, but it is not a general protected-memory vault.
- SQLite and Kusto memory currently store text, numbers, timestamps, and JSON-
  shaped metadata. `Knowledge` and `Conversations` do not currently provide a
  binary or encrypted-value abstraction.

## Protected artifacts

Yes, Eva can have a protected-artifacts directory alongside the ordinary
artifact directory. The proposed Linux path is:

`~/.config/eva-standalone/protected-artifacts`

The directory itself is not the cryptographic primitive. Each file inside it
must be encrypted before it is written. This gives Eva authenticated random
access to individual files, avoids decrypting an entire archive, and keeps
deletion and key rotation manageable. A protected artifact should use an
opaque generated filename such as `pmf-<record-id>.bin`; the original filename,
MIME type, size, and user label belong in protected metadata unless the user
explicitly marks them public.

For small assets, the complete file can be represented by the same canonical
payload envelope used for protected memory records. For large binaries, use a
streaming envelope made of independently authenticated chunks:

- A random per-file data key encrypts the file.
- Each chunk uses AES-256-GCM with a unique nonce derived from a random file
  nonce and the chunk number.
- Associated data authenticates the vault ID, record ID, file version, chunk
  number, and total/chunk layout metadata.
- The final file manifest stores the algorithm, chunk size, chunk count, nonce
  scheme, and authentication tags, but never plaintext.
- The per-file data key is wrapped by the in-memory vault key and the wrapped
  key is stored in protected metadata.

The YubiKey should unwrap or derive the vault key; it should not be asked to
encrypt every byte of a large file. After a successful touch/PIN operation,
the bridge can stream-encrypt or stream-decrypt the requested file using the
unwrapped key. Decrypted content should be returned through a scoped bridge
operation or a controlled temporary file, then removed immediately after the
consumer closes it. A normal artifact URL must never serve protected files.

Protected artifact operations should include create/import, list-metadata,
unlock, stream-read, export-after-confirmation, rotate-key, and delete. The
list operation while locked may show only a generic record ID and user-approved
label. Protected files must be excluded from normal artifact listing, purge,
MCP file tools, embeddings, reflections, and conversation history.

### Platform paths

The implementation should derive the root from Electron's
`app.getPath('userData')` and pass it to the bridge through `EVA_CONFIG_DIR`.
The protected vault then lives at `<userData>/protected-artifacts` on every
desktop platform:

| Platform | Typical user-data root | Protected artifact directory |
| --- | --- | --- |
| Linux | `~/.config/eva-standalone` | `~/.config/eva-standalone/protected-artifacts` |
| macOS | `~/Library/Application Support/<Eva app name>` | `<userData>/protected-artifacts` |
| Windows | `%APPDATA%/<Eva app name>` | `<userData>\\protected-artifacts` |

The exact macOS and Windows application names must come from Electron rather
than being hard-coded. Existing Windows bridge startup already overrides
`EVA_CONFIG_DIR` below `app.getPath('userData')`; the protected-artifact path
should use that same root. Linux should move toward the same Electron-derived
root for packaged builds while retaining the current `EVA_CONFIG_DIR` override
for the standalone bridge and developer workflows.

The crypto format, metadata schema, opaque filenames, and access-control
behavior should be identical on all three platforms. File permissions are a
defense in depth only: use mode `0700`/`0600` where supported on Unix, and
restrict the directory and files to the current Windows user through normal
ACLs. BitLocker, FileVault, and Linux disk encryption do not replace the
application-level vault because they normally unlock with the operating-system
session and would not provide Eva's record-level YubiKey gate.

## Goals

1. Allow a user to store arbitrary sensitive values, including text, JSON, and
   binary data, as protected memory.
2. Keep a redacted index so Eva can truthfully say that protected information
   exists without seeing its value while locked.
3. Require physical YubiKey presence and user presence for unlock.
4. Make protected data unavailable to ordinary memory recall, MCP queries,
   background jobs, logs, telemetry, reflections, and chat-history persistence.
5. Support both local SQLite and Kusto storage without putting plaintext into
   either database.
6. Keep the first implementation focused on the standalone Electron app and
   local bridge. Browser-only support can follow once the key-provider contract
   is stable.

## Non-goals and threat model

Protected memory is intended to protect data at rest and prevent accidental
model exposure while locked. It cannot protect a value after the user unlocks
it and explicitly releases it to a model, nor can it protect a machine whose
OS, bridge process, or active user session is already compromised.

The design must not claim that YubiKey unlock keeps a value away from a cloud
provider. Before a cloud model receives an unlocked value, Eva should display
the active provider and require a separate release confirmation. Local LM
Studio can use the same flow without a network release.

## Proposed storage model

Use separate protected tables. Do not place protected values in `Knowledge`,
`Conversations`, or ordinary FTS indexes.

### ProtectedMemoryIndex

This table is intentionally readable by the bridge while locked and contains
only metadata:

- `RecordId`
- `PublicLabel`, generic by default, such as `protected identity record`
- `Category`, such as `government_identifier`, `financial`, `medical`, or
  `document`
- `Status`, normally `locked` or `disabled`
- `RequiresYubiKey`
- `KeyVersion`
- `CreatedAt` and `UpdatedAt`

The user may choose a more descriptive label, but the UI should warn that the
label remains visible while locked. No plaintext value, searchable value,
embedding, SSN fragment, filename, or user-provided alias belongs here.

### ProtectedMemoryRecords

This table contains encrypted payloads only:

- `RecordId`
- `Ciphertext`
- `Nonce`
- `WrappedDataKey`
- `DataKeyNonce`
- `Algorithm`
- `SchemaVersion`
- `KeyVersion`
- `CreatedAt` and `UpdatedAt`

SQLite may use BLOB columns. Kusto should use base64 strings because the Kusto
schema and inline-ingest path are string/dynamic oriented. The plaintext value
is canonical JSON containing a type tag and the original bytes, so text,
binary files, and structured values share one envelope format.

### ProtectedMemoryKeySlots

This table contains no YubiKey secret or private key:

- `SlotId`
- `Provider`
- `ChallengeOrSalt`
- `WrappedVaultKey`
- `WrapNonce`
- `KeyVersion`
- `Status`
- `CreatedAt` and `LastUsedAt`

The vault key is random and exists only in bridge memory after unlock. A vault
can have more than one key slot so a replacement YubiKey can be enrolled while
the old key still works. Losing every enrolled key means the protected data is
not recoverable unless the user separately created an explicitly designed
recovery export.

## Cryptographic design

Recommended envelope:

1. Generate a random 32-byte vault key at enrollment.
2. Generate a random 32-byte data-encryption key per protected record.
3. Serialize the protected value as canonical UTF-8 JSON, including its type
   and binary bytes when applicable.
4. Encrypt the payload with AES-256-GCM using a fresh nonce and associated data
   containing `vault_id`, `record_id`, `schema_version`, and `key_version`.
5. Wrap each record data key with the vault key using AES-256-GCM and a separate
   nonce.
6. Derive a temporary key-encryption key from the YubiKey provider output with
   HKDF-SHA-256, then wrap the vault key with that key.
7. Store only ciphertext, nonces, wrapped keys, algorithm identifiers, and
   non-sensitive metadata.

The crypto implementation should use a maintained library such as Python
`cryptography`, not custom primitives or password-based encryption. Key
material must never be written to logs, telemetry, prompts, localStorage,
IndexedDB, Kusto diagnostics, or exception messages.

## YubiKey provider

Define a provider interface before binding the vault to a device:

- `enroll()`
- `derive_key(challenge, user_presence=True)`
- `list_keys()`
- `remove_key(slot_id)`

The preferred provider is YubiKey FIDO2 HMAC-secret/PRF with PIN and touch
policy. It produces a key-derived secret rather than exposing a private key.
The standalone app can use a small native/helper adapter so the bridge never
asks an LLM or MCP tool to operate the key. The adapter must be tested against
the selected YubiKey models and firmware on Linux, macOS, and Windows because
FIDO2 HMAC-secret/PRF availability is device- and firmware-dependent.

The cross-platform adapter should prefer a maintained CTAP2/libfido2 or
Yubico-supported implementation rather than parsing platform-specific HID
traffic. Windows has native FIDO2 driver support; macOS and Linux may need the
appropriate HID/libfido2 runtime permissions or packages. Enrollment should
report a clear unsupported-device error instead of silently falling back to a
software key.

The adapter should have a compatibility path for YubiKey Challenge-Response
when FIDO2 PRF is unavailable, but that path must be clearly labeled and must
not silently fall back to a normal password or a software-only key. Static
browser support should be a later WebAuthn-specific phase because the current
file-based browser UI is not a suitable universal secure origin for hardware
key APIs.

## Locked and unlocked behavior

### Capture

Current implementation note: Unlock in Settings asks the user whether Eva may
release a relevant protected text value to the active model for the unlocked
session. Choosing no keeps the vault locally unlocked but withholds values from
the model. Locking revokes release permission. The bridge injects only a record
whose category or public label matches the current request; artifacts are never
injected. Reflection is suppressed while release permission is active so the
ordinary memory database does not retain a decrypted model response.

The first protected-memory capture should use a dedicated secure UI flow with
fields for label, category, value, and optional file selection. It should not
append the raw input to the normal chat history before protection is complete.

Natural-language capture can be added after that flow exists, but the bridge or
renderer must intercept the protected-memory command before provider history,
localStorage, IndexedDB, or reflection receives the sensitive value.

Current implementation note: the renderer safely intercepts explicit chat
capture commands before normal dispatch. Use `save to protected memory:
<value>` for a generic value, or state `my SSN is <value>; add it to protected
memory` for an SSN. The value is cleared from the composer and sent directly to
the protected vault, never to the selected model or ordinary chat history.

### Locked recall

When a request matches a protected category, the bridge returns only a status
block such as:

`Protected memory contains a matching record, but it is locked. Do not guess the value. Request YubiKey unlock.`

The model must not receive ciphertext or protected-table rows. Eva can say that
the record exists and ask the user to unlock it, but should not reveal more
metadata than the user allowed.

### Unlock and release

1. The UI shows the matching protected record and the active model/provider.
2. The user explicitly chooses `Unlock protected memory`.
3. The user inserts/touches the YubiKey and completes any PIN prompt.
4. The bridge unwraps the vault key in memory and issues a short-lived,
   record-scoped, single-use unlock grant.
5. Only the requested record is decrypted for the current operation.
6. A cloud-provider release confirmation is required before sending the value
   outside the local bridge.
7. The production target is a one-turn or short-idle grant. The current first
  slice keeps the release grant until the user locks the vault; decrypted keys
  and payloads are otherwise kept only in bridge memory as far as the runtime
  permits.

The protected turn must be marked ephemeral. The frontend must not persist the
raw user value or unlocked answer to normal session history, and the reflection
pipeline must skip the turn or replace protected spans with a redaction token.

## Access controls

- Generic SQLite SQL must deny reads from protected tables through the SQLite
  authorizer, including reads attempted by the local MCP server.
- Generic Kusto query, sample, and ingest tools must deny protected tables.
- Protected-memory operations must use dedicated bridge endpoints with strict
  record-scoped authorization and loopback/origin checks.
- `/v1/memory/context` may expose only locked metadata and unlock state.
- Background summaries, knowledge hygiene, semantic embeddings, FTS, and
  heuristic extraction must exclude protected records and protected turns.
- Error handling, debug logs, telemetry, and ACP prompt construction need a
  redaction boundary before any content is emitted or persisted.
- File names and MIME metadata for protected binary assets should be treated as
  sensitive unless the user explicitly marks them public.

## Proposed implementation phases

### Implemented in the first slice

- Isolated SQLite vault at `<userData>/protected.sqlite3`.
- AES-256-GCM encrypted text, JSON, binary memory records, and chunked artifact
  files below `<userData>/protected-artifacts`.
- Provider-gated lock/unlock state with no software fallback.
- Loopback bridge endpoints and a dedicated Tools & memory settings panel.
- Touch-configured YubiKey OTP challenge-response compatibility provider.
- Unit, integrity, restart, HTTP lock-gate, and static tests.

The current compatibility provider requires a YubiKey OTP slot configured with
touch. For example, configure slot 2 manually with `ykman otp chalresp
--generate --touch 2`, then keep the slot selection in
`EVA_YUBIKEY_CHALLENGE_SLOT` (default `2`). Eva never creates or logs the
YubiKey secret. FIDO2 HMAC-secret/PRF remains the preferred cross-platform
provider for the next hardware integration phase.

1. **Contract and threat-model tests**
   - Add schemas, redaction rules, protected-table deny rules, and tests before
     enabling any real secret storage.
2. **Key-provider adapter**
   - Implement YubiKey enrollment, challenge, touch/PIN handling, timeout,
     cancellation, and multiple-key rotation behind the provider interface.
3. **Encrypted vault service**
   - Add AES-GCM envelope encryption, key wrapping, versioning, atomic writes,
     and memory-only unlock state.
4. **SQLite backend**
   - Add protected tables, authorizer blocks, CRUD methods, and encrypted
     binary payload support.
5. **Kusto backend**
   - Add matching encrypted schemas and tool guards. Verify ciphertext is the
     only value sent to Kusto.
6. **Protected artifacts**
   - Add the sibling `protected-artifacts` directory, opaque encrypted file
     envelopes, chunked streaming for large binaries, metadata-only locked
     listing, and scoped read/export endpoints.
7. **Secure capture and unlock UI**
   - Add a protected-memory panel, YubiKey status, unlock button, release
     confirmation, timeout state, and ephemeral-turn handling.
8. **Pipeline integration**
   - Add locked metadata context, intent matching, scoped unlock grants,
     reflection redaction, and provider-specific release policy.
9. **Recovery and operations**
   - Add key rotation, second-key enrollment, protected-record deletion, export
     policy, diagnostics that reveal status but never values, and documentation.

## Initial acceptance criteria

- A protected SSN capture never appears in `Knowledge`, `Conversations`, FTS,
  embeddings, logs, telemetry, localStorage, or IndexedDB.
- With the vault locked, a matching question produces a truthful locked notice
  and no secret or ciphertext reaches the model.
- Without the enrolled YubiKey, unlock fails closed.
- With the enrolled YubiKey and explicit release approval, only the requested
  record is available for the current turn.
- A protected binary asset can be stored and retrieved after unlock without
  becoming a normal artifact or session blob; the encrypted source remains in
  `protected-artifacts`.
- SQLite and Kusto behavior is equivalent at the access-control boundary.
- Restarting Eva re-locks the vault and leaves no persisted plaintext key.