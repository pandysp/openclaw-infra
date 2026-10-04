#!/usr/bin/env node
// Provisioning and Claude refresh share proper-lockfile's two lock domains.
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { spawnSync, execFile } = require('node:child_process');
const { randomUUID, createHash } = require('node:crypto');
const modules = path.join(os.homedir(), '.npm-global/lib/node_modules/openclaw/node_modules');

class PartialRotation extends Error {}
class RotationUncertain extends Error {}
class MigrationPending extends Error {}

async function present(file) {
  try {
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Invalid credential state');
    return true;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

function credentials(data) {
  const value = JSON.parse(data);
  const auth = value.claudeAiOauth;
  if (!auth || typeof auth.accessToken !== 'string' || !auth.accessToken ||
      (auth.refreshToken !== undefined && typeof auth.refreshToken !== 'string')) {
    throw new Error('Invalid credential JSON');
  }
  return value;
}

function rawDigest(data) { return createHash('sha256').update(data).digest('hex'); }

async function sameFile(first, second) {
  const [a, b] = await Promise.all([fs.stat(first, { bigint: true }), fs.stat(second, { bigint: true })]);
  return a.dev === b.dev && a.ino === b.ino;
}

async function fileIdentity(file) {
  const stat = await fs.stat(file, { bigint: true });
  return { dev: stat.dev.toString(), ino: stat.ino.toString(), hash: rawDigest(await fs.readFile(file)) };
}

async function matchesAlias(file, alias) {
  if (!alias) return false;
  const stat = await fs.stat(file, { bigint: true });
  return stat.dev.toString() === alias.dev && stat.ino.toString() === alias.ino &&
    rawDigest(await fs.readFile(file)) === alias.hash;
}

async function replayedLogin(migration, old, current) {
  return migration && (await sameFile(old, current) || await matchesAlias(old, migration.loginAlias));
}

async function replayedTracking(migration, old, current) {
  return migration && (await sameFile(old, current) || await matchesAlias(old, migration.trackingAlias));
}

async function journalAlias(directory, key, old, current, check, moved = false) {
  const migration = await receipt(directory);
  if (!migration) throw new Error('Alias cleanup requires a migration receipt');
  const expected = key === 'loginAlias' ? migration.sourceIdentity : migration.versionIdentity;
  const proven = moved ? await matchesAlias(current, expected) : await sameFile(old, current);
  if (proven) {
    // The inode is confirmed at the shared name, not just hashed at source.
    // Persist proof before source sync/startup can permit an SDK refresh.
    await atomicWrite(path.join(directory, '.migration.json'), JSON.stringify({ ...migration,
      [key]: await fileIdentity(current) }), check);
  } else if (moved || !await matchesAlias(old, migration[key])) throw new Error('Unproven migration alias');
}

function digest(data) {
  // Preserve Python's original checksum encoding (Unicode, floats and key sort)
  // so upgrading the helper cannot mistake a refreshed login for a new seed.
  const result = spawnSync('python3', [path.join(__dirname, 'claude-oauth-seed.py')], {
    input: data, encoding: 'utf8', timeout: 10000,
  });
  if (result.status !== 0 || !/^[0-9a-f]{64}\n$/.test(result.stdout || '')) {
    throw new Error('Configured-seed checksum failed');
  }
  return result.stdout.trim();
}

async function withLocks(directories, work) {
  // This is the library/version in the pinned OpenClaw install, also used by
  // Claude 2.1.286. Reuse its heartbeat, stale detection and compromise checks.
  if (require(path.join(modules, 'proper-lockfile/package.json')).version !== '4.1.2') {
    throw new Error('Unsupported OAuth lock library; inspect the pinned runtime');
  }
  const lockfile = require(path.join(modules, 'proper-lockfile'));
  const releases = [];
  let compromised = false;
  let failure;
  const check = () => { if (compromised) throw new Error('OAuth lock ownership lost'); };
  const deadline = Date.now() + 90000;
  try {
    for (const directory of [...new Set(directories)].sort()) {
      if (!path.isAbsolute(directory) || await fs.realpath(directory) !== directory) {
        throw new Error('Noncanonical secure storage directory');
      }
      for (const lockfilePath of [path.join(directory, '.oauth_refresh.lock'), `${directory}.lock`]) {
        const target = lockfilePath === `${directory}.lock` ? lockfilePath : directory;
        const release = await lockfile.lock(target, {
          lockfilePath, realpath: false, stale: 60000, update: 5000,
          retries: { retries: Math.max(0, Math.floor((deadline - Date.now()) / 1000)),
                     minTimeout: 1000, maxTimeout: 1000 },
          onCompromised: () => { compromised = true; },
        });
        releases.push(release);
        check();
      }
    }
    const result = await work(check);
    check();
    return result;
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    let releaseError;
    for (const release of releases.reverse()) {
      try { await release(); }
      catch (error) { releaseError = error; }
    }
    if (releaseError && !failure) throw releaseError;
  }
}

async function syncFile(file) {
  const stream = await fs.open(file, 'r');
  try { await stream.sync(); } finally { await stream.close(); }
}

async function syncDirectory(directory) {
  const stream = await fs.open(directory, 'r');
  try { await stream.sync(); } finally { await stream.close(); }
}

async function syncAncestry(directory, check) {
  // Ansible may have just created shared/auth or host control directories.
  // Persist their parent entries before committing state beneath them.
  for (let current = directory; ; current = path.dirname(current)) {
    check(); await syncDirectory(current);
    if (current === os.homedir() || current === path.dirname(current)) return;
  }
}

async function stage(file, data) {
  const directory = await fs.mkdtemp(path.join(path.dirname(file), '.claude-seed-'));
  const temporary = path.join(directory, 'value');
  try {
    const stream = await fs.open(temporary, 'wx', 0o600);
    try { await stream.writeFile(data); await stream.sync(); }
    finally { await stream.close(); }
    await syncDirectory(directory);
    return { directory, temporary };
  } catch (error) {
    await fs.rm(temporary, { force: true });
    await fs.rmdir(directory);
    throw error;
  }
}

async function atomicWrite(file, data, check) {
  const { directory, temporary } = await stage(file, data);
  let replaced = false;
  try {
    check();
    await fs.rename(temporary, file);
    replaced = true;
    await syncDirectory(path.dirname(file));
    await syncDirectory(directory);
  } catch (error) {
    error.replaced = replaced;
    throw error;
  } finally {
    try {
      await fs.rm(temporary, { force: true });
      await fs.rmdir(directory);
      await syncDirectory(path.dirname(file));
    } catch (error) {
      error.replaced = replaced;
      throw error;
    }
  }
}

async function receipt(directory) {
  const file = path.join(directory, '.migration.json');
  if (!await present(file)) return null;
  const value = JSON.parse(await fs.readFile(file));
  if (value.directory !== directory || !path.isAbsolute(value.legacy) ||
      !['moving', 'moved', 'seeding'].includes(value.phase) ||
      (value.phase === 'seeding' && !/^[0-9a-f]{64}$/.test(value.seedHash)) ||
      (value.operation !== undefined && !['adopt', 'replace'].includes(value.operation)) ||
      (value.committed !== undefined && typeof value.committed !== 'boolean') ||
      (value.requestId !== undefined && (typeof value.requestId !== 'string' || !requestPattern.test(value.requestId))) ||
      ['loginAlias', 'trackingAlias', 'sourceIdentity', 'versionIdentity'].some(key => value[key] !== undefined &&
        (!value[key] || !/^\d+$/.test(value[key].dev) || !/^\d+$/.test(value[key].ino) || !/^[0-9a-f]{64}$/.test(value[key].hash)))) {
    throw new Error('Invalid authentication migration receipt');
  }
  return value;
}

const restartMarker = path.join(os.homedir(), '.config/openclaw/claude-auth-restart');
const rotationHistory = path.join(os.homedir(), '.config/openclaw/claude-auth-rotation');
const requestPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

async function completedRotation(directory) {
  if (!await present(rotationHistory)) return null;
  const value = JSON.parse(await fs.readFile(rotationHistory));
  // Read the earlier single-request draft without discarding its completed ID.
  const requests = value.requests || (value.requestId ? { [value.requestId]: value.seedHash } : null);
  if (!path.isAbsolute(value.directory) || !requests || typeof requests !== 'object' || Array.isArray(requests) ||
      Object.entries(requests).some(([id, hash]) => !requestPattern.test(id) || typeof hash !== 'string' || !/^[0-9a-f]{64}$/.test(hash))) {
    throw new Error('Invalid completed rotation requests');
  }
  if (value.directory !== directory) throw new Error('Rotation history belongs to another storage directory');
  return { directory, requests };
}

async function completeRotationRequest(directory, requestId, seedHash, check) {
  if (requestId === undefined || requestId === null) return;
  const history = await completedRotation(directory);
  if (history && Object.hasOwn(history.requests, requestId)) {
    if (history.requests[requestId] !== seedHash) throw new Error('Rotation request identifier reused for a different seed');
    // A prior rename can be visible after its parent fsync failed. Persist
    // the retained proof before acknowledging or deleting recovery receipts.
    check(); await syncFile(rotationHistory);
    await syncAncestry(path.dirname(rotationHistory), check);
    return;
  }
  check();
  await fs.mkdir(path.dirname(rotationHistory), { recursive: true, mode: 0o700 });
  await syncAncestry(path.dirname(rotationHistory), check);
  await atomicWrite(rotationHistory, JSON.stringify({ directory,
    requests: { ...(history?.requests || {}), [requestId]: seedHash } }), check);
}

async function restartRequest() {
  return await present(restartMarker) ? (await fs.readFile(restartMarker, 'utf8')).trim() : 'NONE';
}

async function captureRestart(directory) {
  return withLocks([directory], async check => { check(); return restartRequest(); });
}

async function acknowledgeRestart(nonce, directory) {
  return withLocks([directory], async check => {
    if (nonce !== 'NONE' && await restartRequest() === nonce) {
      check();
      await fs.unlink(restartMarker);
      await syncDirectory(path.dirname(restartMarker));
      return 'ACKNOWLEDGED';
    }
    return 'PRESERVED';
  });
}

async function requireRestart(nonce, check) {
  // A concurrent provisioner can install the unit immediately after this
  // write. Journal unconditionally; absence of a consumer is not completion.
  check();
  await fs.mkdir(path.dirname(restartMarker), { recursive: true, mode: 0o700 });
  await syncAncestry(path.dirname(restartMarker), check);
  await atomicWrite(restartMarker, `${nonce}\n`, check);
}

async function finishRotation(directory, destination, version, check, committed, adoptExisting = false) {
  const marker = path.join(directory, '.rotation.json');
  if (!await present(marker)) return false;
  const value = JSON.parse(await fs.readFile(marker));
  const staged = path.dirname(value.replacement || '');
  if (!/^[0-9a-f]{64}$/.test(value.seedHash) || path.dirname(staged) !== directory ||
      !/^\.claude-seed-[A-Za-z0-9]+$/.test(path.basename(staged)) ||
      path.basename(value.replacement) !== 'value' ||
      (value.previousHash !== undefined && value.previousHash !== null && !/^[0-9a-f]{64}$/.test(value.previousHash)) ||
      (value.phase !== undefined && !['prepared', 'committed'].includes(value.phase)) ||
      (value.requestId !== undefined && (typeof value.requestId !== 'string' || !requestPattern.test(value.requestId)))) {
    throw new Error('Invalid credential rotation receipt');
  }
  const tracked = await present(version) && (await fs.readFile(version, 'utf8')).trim() === value.seedHash;
  const history = await completedRotation(directory);
  const recordedSeed = value.requestId && history && Object.hasOwn(history.requests, value.requestId) ? history.requests[value.requestId] : null;
  if (recordedSeed && recordedSeed !== value.seedHash) throw new Error('Rotation request identifier reused for a different seed');
  if (recordedSeed && !tracked) throw new Error('Stale completed rotation receipt; preserve current tracking');
  const completed = value.phase === 'committed' || recordedSeed === value.seedHash;
  let stageExists = true;
  try { if (await fs.realpath(staged) !== staged) throw new Error('Invalid credential rotation stage'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; stageExists = false; }
  if (!stageExists && !tracked) throw new Error('Missing rotation stage without committed tracking');
  if (await present(value.replacement)) {
    const stagedData = await fs.readFile(value.replacement);
    credentials(stagedData);
    if (digest(stagedData) !== value.seedHash) throw new Error('Staged credential checksum mismatch');
    await requireRestart(randomUUID(), check);
    let preserve = false;
    if (await present(destination)) {
      const current = await fs.readFile(destination);
      const unchanged = value.previousHash !== undefined && value.previousHash !== null &&
        rawDigest(current) === value.previousHash;
      if (!unchanged || completed || adoptExisting) {
        credentials(current);
        preserve = completed || adoptExisting || digest(current) === value.seedHash;
        if (!preserve) {
          // A source link can replay before its fsync. A later SDK refresh
          // makes pre-/post-rename history indistinguishable: never guess.
          committed();
          throw new RotationUncertain('Unprovable credential replacement');
        }
      }
    }
    check();
    if (preserve) {
      committed();
      await syncFile(destination); await syncDirectory(directory);
      check(); await fs.unlink(value.replacement);
    } else {
      try { await fs.rename(value.replacement, destination); committed(); }
      catch (error) {
        try { if (!await present(value.replacement)) committed(); }
        catch (probeError) { committed(); }
        throw error;
      }
    }
  } else {
    if (!await present(destination)) throw new Error('Interrupted credential replacement is missing');
    const current = await fs.readFile(destination);
    credentials(current);
    await requireRestart(randomUUID(), check);
    if (!completed && digest(current) !== value.seedHash && !adoptExisting) {
      committed();
      throw new RotationUncertain('Missing source without credential commit proof');
    }
  }
  committed();
  check(); await syncFile(destination);
  // Persist the destination BEFORE source removal; interruption can replay
  // source aliases, but must never discard the only durable login name.
  await syncDirectory(directory);
  if (stageExists) await syncDirectory(staged);
  // Tracker equality can predate --rotate. Journal a fresh commit only after
  // this transaction's credential barriers, before writing tracking.
  await atomicWrite(marker, JSON.stringify({ ...value, phase: 'committed' }), check);
  await atomicWrite(version, `${value.seedHash}\n`, check);
  // Resolving a request includes explicit adoption, not just replacement.
  // Retain its identity before cleanup and lock release can report an error.
  await completeRotationRequest(directory, value.requestId, value.seedHash, check);
  const migration = await receipt(directory);
  if (migration && migration.phase === 'seeding' && migration.operation === 'replace' && migration.seedHash === value.seedHash) {
    await atomicWrite(path.join(directory, '.migration.json'), JSON.stringify({ ...migration, committed: true,
      operation: adoptExisting ? 'adopt' : 'replace' }), check);
  }
  check(); await fs.unlink(marker);
  await syncDirectory(directory);
  if (stageExists) { await fs.rmdir(staged); await syncDirectory(directory); }
  return { seedHash: value.seedHash, requestId: value.requestId || null };
}

async function seed(source, destination, version, rotate, adoptExisting = false, requestId = null) {
  if (rotate && adoptExisting) throw new Error('Choose rotation or adoption, not both');
  if (path.dirname(destination) !== path.dirname(version)) throw new Error('Split credential state');
  const data = await fs.readFile(source);
  const desired = credentials(data);
  const wanted = digest(data);
  if (requestId !== null && (!rotate || typeof requestId !== 'string' || !requestPattern.test(requestId))) throw new Error('Invalid rotation request identifier');
  const forceRequest = rotate ? (requestId || wanted) : null;
  let replaced = false;
  try {
    return await withLocks([path.dirname(destination)], async check => {
      await syncAncestry(path.dirname(destination), check);
      const pendingMove = await receipt(path.dirname(destination));
      if (pendingMove && (pendingMove.phase === 'moving' ||
          await present(path.join(pendingMove.legacy, '.credentials.json')) ||
          await present(path.join(pendingMove.legacy, '.credentials-seed.sha256')))) {
        throw new MigrationPending('Finish migration before seeding');
      }
      let recovered = await finishRotation(path.dirname(destination), destination, version, check, () => { replaced = true; }, adoptExisting);
      const exists = await present(destination);
      const history = await completedRotation(path.dirname(destination));
      const recordedSeed = forceRequest && history && Object.hasOwn(history.requests, forceRequest) ? history.requests[forceRequest] : null;
      if (recordedSeed && recordedSeed !== wanted) throw new Error('Rotation request identifier reused for a different seed');
      const duplicateForce = recordedSeed === wanted && exists;
      const previous = await present(version) ? (await fs.readFile(version, 'utf8')).trim() : null;
      if (previous !== null && !/^[0-9a-f]{64}$/.test(previous)) throw new Error('Invalid seed tracking');
      if (duplicateForce && previous !== null && previous !== wanted) throw new Error('Completed rotation request reused after another seed; choose a new request identifier');
      const directory = path.dirname(destination);
      const migration = await receipt(directory);
      // Cleanup can finish before migration receipt deletion. Replacement
      // and explicit adoption both resolve the recorded request.
      if (forceRequest && migration?.committed === true && migration.requestId === forceRequest && migration.seedHash !== wanted) {
        throw new Error('Rotation request identifier reused for a different seed');
      }
      if (migration?.phase === 'seeding' && migration.committed === true &&
          (migration.operation === 'replace' || (migration.operation === 'adopt' && forceRequest && migration.requestId === forceRequest)) &&
          previous === migration.seedHash && wanted === migration.seedHash) {
        recovered = { seedHash: migration.seedHash, requestId: migration.requestId || null };
      }
      const liveData = exists && (!rotate || recovered || duplicateForce) ? await fs.readFile(destination) : null;
      const live = liveData === null ? null : credentials(liveData);
      const marker = path.join(directory, '.migration.json');
      // The checksum can already match before a forced replacement. Only this
      // transaction's durable commit bit proves seeding actually finished.
      const pending = migration && !(migration.phase === 'seeding' && migration.committed === true && previous === migration.seedHash);
      if (pending && migration.phase === 'seeding') {
        if (!migration.operation && !adoptExisting) throw new RotationUncertain('Unprovable legacy seeding transaction');
        if (migration.seedHash !== wanted) throw new Error('Configured source changed during interrupted seeding');
      }
      const transactionRequest = forceRequest || (pending ? migration?.requestId : undefined);
      const recoveredForce = forceRequest && recovered && recovered.requestId === forceRequest;
      const replacing = (rotate && !recoveredForce && !duplicateForce) ||
        (pending && migration.operation === 'replace' && !adoptExisting && !duplicateForce);
      if (migration && !pending) {
        check(); await syncFile(destination);
        await completeRotationRequest(directory, migration.requestId, migration.seedHash, check);
        check(); await fs.unlink(marker); await syncDirectory(directory);
      }
      if (duplicateForce) await completeRotationRequest(directory, forceRequest, wanted, check);
      if (!pending && live && previous === wanted && !replacing) {
        const consumerExists = await present(path.join(os.homedir(), '.config/systemd/user/openclaw-gateway.service'));
        return recovered || (consumerExists && await restartRequest() !== 'NONE') ? 'RECOVERED' : 'PRESERVED';
      }
      if ((adoptExisting || pending) && !exists) throw new Error('Migration login is missing');
      const adopt = live && !replacing && (adoptExisting || pending || duplicateForce || (previous === null &&
        (digest(liveData) === wanted || (desired.claudeAiOauth.refreshToken && live.claudeAiOauth.refreshToken))));
      if (pending) {
        await atomicWrite(marker, JSON.stringify({ ...migration, phase: 'seeding', seedHash: wanted,
          operation: adopt ? 'adopt' : 'replace', requestId: transactionRequest || undefined, committed: false }), check);
      }
      const status = adopt ? 'ADOPTED' : exists ? 'ROTATED' : 'SEEDED';
      if (!adopt) {
        const staged = await stage(destination, data);
        // Record the prior bytes: a replayed source name alone cannot prove
        // replacement is still pending after an interrupted directory sync.
        const rotationMarker = path.join(directory, '.rotation.json');
        try {
          await atomicWrite(rotationMarker, JSON.stringify({ replacement: staged.temporary, seedHash: wanted, phase: 'prepared',
            previousHash: exists ? rawDigest(await fs.readFile(destination)) : null,
            requestId: transactionRequest || undefined }), check);
        } catch (error) {
          const recorded = await present(rotationMarker) ? JSON.parse(await fs.readFile(rotationMarker)) : null;
          if (!recorded || recorded.replacement !== staged.temporary) {
            await fs.rm(staged.temporary, { force: true });
            await fs.rmdir(staged.directory);
          }
          throw error;
        }
        await finishRotation(directory, destination, version, check, () => { replaced = true; });
      } else {
        check(); await syncFile(destination);
        await atomicWrite(version, `${wanted}\n`, check);
        await completeRotationRequest(directory, transactionRequest, wanted, check);
      }
      if (pending) {
        await atomicWrite(marker, JSON.stringify({ ...migration, phase: 'seeding', seedHash: wanted,
          operation: adopt ? 'adopt' : 'replace', requestId: transactionRequest || undefined, committed: true }), check);
        check(); await fs.unlink(marker); await syncDirectory(directory);
      }
      return status;
    });
  } catch (error) {
    if (error instanceof RotationUncertain) throw error;
    if (replaced) throw new PartialRotation('Credentials changed; restart before retrying');
    throw error;
  }
}

async function execute(program, args, timeout) {
  return new Promise((resolve, reject) => {
    execFile(program, args, { timeout, env: { ...process.env, XDG_RUNTIME_DIR: `/run/user/${process.getuid()}` } }, (error, stdout, stderr) => {
      if (error) { error.stdout = stdout; error.stderr = stderr; reject(error); }
      else resolve({ stdout, stderr });
    });
  });
}

async function verifyWriters(check) {
  check();
  let states;
  try {
    const result = await execute('ps', ['-C', 'claude,claude.exe', '-o', 'stat='], 10000);
    if (result.stderr.trim()) throw new Error('Native Claude shutdown verification returned diagnostics');
    states = result.stdout;
  }
  catch (error) {
    if (error.code !== 1 || error.stdout.trim() || error.stderr.trim()) throw error;
    states = '';
  }
  const tokens = states.split('\n').map(line => line.trim()).filter(Boolean);
  if (tokens.some(token => !/^[RSDTtWXZI][<NLsl+]*$/.test(token))) {
    throw new Error('Native Claude shutdown verification returned invalid process states');
  }
  if (tokens.some(token => !['Z', 'X'].includes(token[0]))) {
    throw new Error('Native Claude writers remain; login was not moved');
  }
  check();
}

async function migrate(legacy, directory, quiesce = '--verify-writers') {
  if (!['--quiesce-gateway', '--verify-writers'].includes(quiesce)) throw new Error('Invalid migration writer-verification mode');
  return withLocks([legacy, directory], async check => {
    const old = path.join(legacy, '.credentials.json');
    const current = path.join(directory, '.credentials.json');
    const sourceExists = await present(old);
    const destinationExists = await present(current);
    const oldVersion = path.join(legacy, '.credentials-seed.sha256');
    const newVersion = path.join(directory, '.credentials-seed.sha256');
    const remainingLegacy = sourceExists || await present(oldVersion);
    const migration = await receipt(directory);
    if (migration && migration.legacy !== legacy) throw new Error('Migration source mismatch');
    const replayed = sourceExists && destinationExists && await replayedLogin(migration, old, current);
    if (sourceExists && destinationExists && !replayed) throw new Error('Ambiguous credential migration');
    if (!remainingLegacy && !migration) return destinationExists ? 'ALREADY_MIGRATED' : 'NO_LEGACY_LOGIN';
    if (!sourceExists && !destinationExists) throw new Error('Interrupted migration login is missing');
    const preserved = sourceExists && !replayed ? old : current;
    credentials(await fs.readFile(preserved));
    check(); await syncFile(preserved);
    await syncAncestry(directory, check); await syncAncestry(legacy, check);
    if (!remainingLegacy && migration?.phase === 'seeding' && migration.committed === true) {
      const version = path.join(directory, '.credentials-seed.sha256');
      if (await present(version) && (await fs.readFile(version, 'utf8')).trim() === migration.seedHash) {
        return 'ALREADY_MIGRATED';
      }
    }
    // Decide while holding BOTH domains, not from Ansible's earlier stat facts.
    // Async systemctl keeps the SDK lock heartbeat alive while the unit stops.
    const gatewayUnit = path.join(os.homedir(), '.config/systemd/user/openclaw-gateway.service');
    if (remainingLegacy && quiesce === '--quiesce-gateway' && await present(gatewayUnit)) {
      await persistAssets([gatewayUnit], check);
      check();
      await execute('systemctl', ['--user', 'stop', 'openclaw-gateway'], 45000);
    }
    if (remainingLegacy) {
      await verifyWriters(check);
    }
    if (remainingLegacy) {
      // Stop can create/replace either login even in a tracker-only resume.
      // Earlier absence is not authorization to overwrite or ignore it.
      if (await present(old) !== sourceExists ||
          (sourceExists ? await present(current) && !replayed : !await present(current))) {
        throw new Error('Credential paths changed during shutdown');
      }
      // Shutdown can perform a final credential write outside refresh locking.
      // Validate and persist its last bytes after all native writers stop.
      if (replayed && !await replayedLogin(migration, old, current)) throw new Error('Migration source changed during shutdown');
      credentials(await fs.readFile(preserved));
      check(); await syncFile(preserved);
    }
    const hasVersion = await present(oldVersion);
    const versionExists = await present(newVersion);
    const trackingReplayed = hasVersion && versionExists && await replayedTracking(migration, oldVersion, newVersion);
    if (hasVersion && versionExists && !trackingReplayed) throw new Error('Ambiguous seed migration');
    if (!migration || (sourceExists && !replayed) || (hasVersion && !trackingReplayed)) {
      await atomicWrite(path.join(directory, '.migration.json'), JSON.stringify({
        ...(migration || { legacy, directory, phase: 'moving' }),
        sourceIdentity: sourceExists && !replayed ? await fileIdentity(old) : migration?.sourceIdentity,
        versionIdentity: hasVersion && !trackingReplayed ? await fileIdentity(oldVersion) : migration?.versionIdentity,
      }), check);
    }
    if (sourceExists) {
      if (replayed) {
        await syncDirectory(directory);
        await journalAlias(directory, 'loginAlias', old, current, check);
        check(); await fs.unlink(old);
      } else {
        check(); await fs.rename(old, current);
        await syncDirectory(directory);
        await journalAlias(directory, 'loginAlias', old, current, check, true);
      }
      await syncDirectory(directory); await syncDirectory(legacy);
    }
    // Leave the durable receipt on metadata failure. Compensation after lock
    // loss could overwrite a concurrent writer; locked recovery selects the
    // surviving login and resumes this move instead.
    if (hasVersion) {
      check(); await syncFile(trackingReplayed ? newVersion : oldVersion);
      if (trackingReplayed) {
        await syncDirectory(directory);
        await journalAlias(directory, 'trackingAlias', oldVersion, newVersion, check);
        check(); await fs.unlink(oldVersion);
      } else {
        check(); await fs.rename(oldVersion, newVersion);
        await syncDirectory(directory);
        await journalAlias(directory, 'trackingAlias', oldVersion, newVersion, check, true);
      }
      await syncDirectory(directory); await syncDirectory(legacy);
    }
    if (!migration || migration.phase === 'moving') {
      const moved = await receipt(directory);
      await syncDirectory(directory); await syncDirectory(legacy);
      await atomicWrite(path.join(directory, '.migration.json'), JSON.stringify({ ...moved, phase: 'moved' }), check);
    }
    return sourceExists ? 'MIGRATED' : 'MIGRATION_RESUMED';
  });
}

async function selectStorage(legacy, directory, allowEmpty = false, validateOnly = false) {
  return withLocks([legacy, directory], async check => {
      const oldExists = await present(path.join(legacy, '.credentials.json'));
      const currentExists = await present(path.join(directory, '.credentials.json'));
      const migration = await receipt(directory);
      const replayed = oldExists && currentExists && migration && migration.legacy === legacy &&
        await replayedLogin(migration, path.join(legacy, '.credentials.json'), path.join(directory, '.credentials.json'));
      if ((oldExists && currentExists && !replayed) || (!oldExists && !currentExists && !allowEmpty)) {
        throw new Error('No unique preserved login for recovery');
      }
      const selected = currentExists || !oldExists ? directory : legacy;
      if (oldExists || currentExists) credentials(await fs.readFile(path.join(selected, '.credentials.json')));
      // Host-only: never put a systemd EnvironmentFile in C's writable bind.
      const environment = path.join(os.homedir(), '.config/openclaw/claude-auth.env');
      const value = `CLAUDE_SECURESTORAGE_CONFIG_DIR=${selected}\n`;
      const matches = await present(environment) && await fs.readFile(environment, 'utf8') === value;
      if (validateOnly) {
        // Startup locks both domains even while shared storage is still empty.
        // Its directories must survive reboot before installing Requires.
        await syncAncestry(legacy, check); await syncAncestry(directory, check);
        return 'ENV_VALID';
      }
      // Never publish the shared refresh domain while legacy writers can
      // still use another one. Provisioning migrates/quiesces before selection.
      if (selected === directory && (oldExists || await present(path.join(legacy, '.credentials-seed.sha256')))) {
        await verifyWriters(check);
      }
      if (currentExists && !oldExists && migration?.legacy === legacy &&
          await matchesAlias(path.join(directory, '.credentials.json'), migration.sourceIdentity)) {
        check(); await syncFile(path.join(directory, '.credentials.json'));
        await syncDirectory(directory);
        await journalAlias(directory, 'loginAlias', path.join(legacy, '.credentials.json'), path.join(directory, '.credentials.json'), check, true);
        await syncDirectory(legacy);
      }
      if (replayed) {
        // Journal proof before an atomic SDK refresh can change the shared
        // inode, but retain the legacy name. Only migration may remove it,
        // after stopping the gateway and verifying its remaining writers.
        check(); await syncFile(path.join(directory, '.credentials.json'));
        await syncDirectory(directory);
        await journalAlias(directory, 'loginAlias', path.join(legacy, '.credentials.json'), path.join(directory, '.credentials.json'), check);
      }
      if (matches) return 'ENV_PRESERVED';
      // Pointer commit and service refresh are separate steps. Keep restart
      // intent if the driver dies or restart fails after the pointer changes.
      await requireRestart(randomUUID(), check);
      await atomicWrite(environment, value, check);
      return 'ENV_CHANGED';
  });
}

async function recover(legacy, directory, service = 'openclaw-gateway') {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.@-]*$/.test(service)) throw new Error('Invalid gateway recovery service');
  let failure, selection, refreshed = false, started = false;
  try {
    selection = await selectStorage(legacy, directory);
  } catch (error) {
    failure = error;
  } finally {
    // Bootstrap has no service to recover yet. Keep any journalled intent for
    // a later installer; never retry starting a known absent managed unit.
    let absent = false;
    try {
      const unitName = service.endsWith('.service') ? service : `${service}.service`;
      const unit = path.join(os.homedir(), '.config/systemd/user', unitName);
      const exists = await present(unit);
      absent = unitName === 'openclaw-gateway.service' && !exists;
      if (exists) await persistAssets([unit]);
    } catch (error) { failure = error; }
    if (absent) {
      if (failure) throw failure;
      return 'GATEWAY_NOT_INSTALLED';
    }
    // Even a failed storage/env write cannot skip the independent start attempt.
    const env = { ...process.env, XDG_RUNTIME_DIR: `/run/user/${process.getuid()}` };
    const reload = spawnSync('systemctl', ['--user', 'daemon-reload'], { env, timeout: 15000, stdio: 'ignore' });
    let nonce = 'NONE', action = 'start';
    try {
      nonce = await captureRestart(directory);
      if (nonce !== 'NONE') action = 'restart';
      else {
        const active = spawnSync('systemctl', ['--user', 'is-active', '--quiet', service], { env, timeout: 5000, stdio: 'ignore' });
        if (active.status === 0 && !failure) action = selection === 'ENV_CHANGED' ? 'restart' : null;
        else if (active.status !== 0 && active.status !== 3) failure = new Error('Gateway recovery state check failed');
      }
    } catch (error) { failure = error; }
    const start = action ? spawnSync('systemctl', ['--user', action, service], { env, timeout: 45000, stdio: 'ignore' }) : { status: 0 };
    refreshed = action === 'restart' && start.status === 0;
    started = action === 'start' && start.status === 0;
    if (reload.status !== 0 || start.status !== 0) failure = new Error('Gateway recovery failed');
    else if (!failure && nonce !== 'NONE') {
      try { await acknowledgeRestart(nonce, directory); }
      catch (error) { failure = error; }
    }
  }
  if (failure) throw failure;
  return refreshed ? 'GATEWAY_REFRESHED' : started ? 'GATEWAY_STARTED' : 'GATEWAY_RECOVERED';
}

async function persistAssets(files, check = () => {}) {
  // Host-only startup assets. A transaction caller supplies its ownership check.
  for (const file of files) {
    if (!await present(file)) throw new Error('Required recovery asset is missing');
    check(); await syncFile(file);
    await syncAncestry(path.dirname(file), check);
  }
  return 'ASSETS_PERSISTED';
}

async function main(args) {
  if (args[0] === 'persist-assets' && args.length > 1) return persistAssets(args.slice(1));
  if (args[0] === 'validate-storage' && args.length === 3) return selectStorage(args[1], args[2], true, true);
  if (args[0] === 'prepare-storage' && args.length === 3) return selectStorage(args[1], args[2], true);
  if (args[0] === 'restart-request' && args.length === 2) return captureRestart(args[1]);
  if (args[0] === 'ack-restart' && args.length === 3) return acknowledgeRestart(args[1], args[2]);
  if (args[0] === 'select-storage' && args.length === 3) return selectStorage(args[1], args[2]);
  if (args[0] === 'recover' && [3, 4].includes(args.length)) return recover(args[1], args[2], args[3]);
  if (args[0] === 'migrate' && [3, 4].includes(args.length) &&
      (args.length === 3 || ['--quiesce-gateway', '--verify-writers'].includes(args[3]))) {
    return migrate(args[1], args[2], args[3]);
  }
  if (args.length === 3 || (args.length === 4 && (['--rotate', '--adopt'].includes(args[3]) || args[3].startsWith('--rotate=')))) {
    const rotate = args[3] === '--rotate' || args[3]?.startsWith('--rotate=');
    const requestId = args[3]?.startsWith('--rotate=') ? args[3].slice('--rotate='.length) : null;
    if (requestId === '') throw new Error('Rotation request identifier is empty');
    return seed(args[0], args[1], args[2], rotate, args[3] === '--adopt', requestId);
  }
  throw new Error('Usage: claude-oauth-seed SOURCE DESTINATION VERSION [--rotate[=REQUEST_ID]|--adopt], or migrate OLD_DIR NEW_DIR');
}

if (require.main === module) {
  main(process.argv.slice(2)).then(status => console.log(status)).catch(error => {
    if (error instanceof MigrationPending) {
    console.error('ERROR: Claude migration is unfinished; finish it with claude-auth provisioning before seeding. The current login and recovery journal were preserved.');
  } else if (error instanceof RotationUncertain) {
    console.log('ROTATED_VERSION_PENDING');
    console.error('ERROR: Interrupted Claude credential change is ambiguous; the current login was preserved. Inspect private state and retry with --adopt only to explicitly accept this login. No replacement was guessed.');
  } else if (error instanceof PartialRotation) {
      console.log('ROTATED_VERSION_PENDING');
      console.error('ERROR: Claude credentials changed but provisioning did not finish. Restart the gateway before retrying and check seed tracking and refresh locks; private diagnostics withheld.');
    } else if (error.code === 'ELOCKED') {
      console.error('ERROR: Claude refresh locks remained busy; check active refreshes before retrying provisioning.');
    } else {
      console.error('ERROR: Claude credential provisioning failed; check canonical storage paths, private JSON, permissions and the pinned proper-lockfile dependency. Private diagnostics withheld.');
    }
    process.exitCode = 1;
  });
}
module.exports = { seed, migrate, recover, withLocks };
