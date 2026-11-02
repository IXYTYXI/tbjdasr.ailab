# Feishu sync recovery design

## Goal and invariant

Keep scheduled transcript sync progressing after read-only failures while never automatically repeating a remote mutation whose result is unknown. A missing segment in one readback is not proof that the prior insert failed. No remote idempotency key is exposed by the current `lark-cli docs +update` interface; its revision argument has not been validated as an atomic compare-and-swap guard.

## Operation states

For each document segment, persist `pending` immediately before the actual insert. The pending record contains stable identity (`room`, job ID), expected XML, row digest, and start time. On restart, read the document. Exact identity and body match confirms completion; duplicate identity or mismatched body requires review. An absent identity remains uncertain after a sent insert and must not be automatically resent. A failure before the pending record is saved is a read-only preflight failure and is automatically retried on the next loop.

The segment title retains the human timestamp and duration. A new stable identity marker distinguishes rooms and jobs that share timestamps. Existing pending fragments without a marker remain readable using their historical title, but an absent legacy fragment also stays uncertain.

For Base records, perform validation and duplicate lookup before persisting `record_pending`; persist it only immediately before upsert. A pending upsert is resolved by a record lookup. A missing record after an uncertain upsert remains uncertain unless the upsert's idempotency semantics are independently validated.

For a new chapter, put its heading and first fragment in one remote mutation where insertion order permits. Otherwise persist a separate pending heading state before the heading mutation and resolve it by readback, without blind retry.

## Operations and observability

The service should back off per session after repeated failures and summarize identical errors rather than emitting them every two seconds. Periodic remote audit should detect edits or deletion of previously confirmed fragments. New documents use the user-specified folder. Existing receipts and documents keep their old folder identity and are discovered by legacy-key fallback so migration does not create duplicate sessions.

## Verification

Fault injection covers preflight failure, sent insert with lost response and stale read, partial or mismatched fragment, duplicate title, Base preflight failure, uncertain upsert, uncertain heading, cross-room identity, and legacy receipt lookup. The entire local test suite runs before push. Live Feishu version semantics are not assumed from local tests.
