# Security checklist (complexity ≥ 7)
- Input validation at every trust boundary (HTTP, CLI args, file/DB/queue reads). Injection: SQL, shell, path, template, prompt.
- AuthN/AuthZ: every new route/handler checks identity and permission; no IDOR via user-supplied ids.
- Secrets: none in code, logs, fixtures, or error messages. Env/Keychain only.
- Crypto: library primitives only; constant-time compare for tokens.
- Data: deletes and migrations reversible or explicitly approved; PII not widened.
- Dependencies: new ones justified, pinned, from the expected registry.
- Errors: fail closed; no stack traces to clients.
- Concurrency: shared state guarded; idempotency on retries.
