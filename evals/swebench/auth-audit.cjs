// Pi 1.1.0 FileAuthStorageBackend writes auth via fs.writeFileSync.
// Journal BEFORE each mutation; failures throw before credentials can rotate on disk.
const fs = require('node:fs');
const path = require('node:path');
const { syncBuiltinESMExports } = require('node:module');

function installAuthAudit(agentDir, secretDir) {
  const write = fs.writeFileSync;
  const auth = path.join(agentDir, 'auth.json');
  const target = path.join(secretDir, 'auth.json');
  const journal = path.join(secretDir, 'auth-journal.jsonl');
  let sequence = 0;
  fs.writeFileSync = function(file, data, options) {
    const name = typeof file === 'string' ? path.resolve(file) : '';
    if (name === auth || name === target) {
      const pending = path.join(secretDir, `auth-pending-${process.pid}-${++sequence}`);
      try {
        write(pending, '', {flag: 'wx', mode: 0o600});
        if (fs.realpathSync(name) !== target || !fs.statSync(target).isFile()) throw Error();
        const text = Buffer.isBuffer(data) ? data.toString('utf8') : data;
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw Error();
        // O_NOFOLLOW prevents journal-link traversal. Append is private and atomic per snapshot.
        const fd = fs.openSync(journal, fs.constants.O_WRONLY | fs.constants.O_APPEND |
          fs.constants.O_CREAT | fs.constants.O_NOFOLLOW, 0o600);
        try {
          const row = Buffer.from(JSON.stringify(parsed) + '\n');
          if (fs.writeSync(fd, row) !== row.length) throw Error();
          fs.fsyncSync(fd);
        } finally { fs.closeSync(fd); }
        fs.unlinkSync(pending);
      } catch {
        process.stderr.write('PEPI_EVAL_AUTH_AUDIT_FAILED\n');
        throw new Error('Eval credential journaling failed');
      }
    }
    return write.call(this, file, data, options);
  };
  syncBuiltinESMExports();
}

installAuthAudit('/opt/pepi-agent', '/run/pepi-secrets');
// Export the same installer for executable isolated tests (runtime uses only fixed paths).
module.exports = { installAuthAudit };
