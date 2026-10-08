#!/usr/bin/env bash
# Exercise actual gateway inference and authenticated private MCP reads.
# The workflow supplies an exact run-owned host and a private repository for read-only checks.
set -euo pipefail

: "${STAGING_HOST:?STAGING_HOST is required}"
: "${STAGING_PRIVATE_REPOSITORY:?A verified-private staging fixture repository is required}"
: "${STAGING_MODEL:?The intended staging model is required}"
[[ "$STAGING_HOST" =~ ^openclaw-staging-[0-9]+-[0-9]+\.[a-zA-Z0-9.-]+$ ]] || {
  echo "ERROR: Refusing a host that is not a run-scoped staging host" >&2
  exit 1
}
[[ "$STAGING_PRIVATE_REPOSITORY" =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ ]] || {
  echo "ERROR: Invalid private fixture repository name" >&2
  exit 1
}

[[ "$STAGING_MODEL" =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ ]] || {
  echo "ERROR: Invalid staging model identifier" >&2
  exit 1
}

# Tailscale verifies the SSH host key. The on-host CLI reads its own config;
# no gateway token is looked up on the runner or forwarded over SSH.
tailscale ssh "ubuntu@$STAGING_HOST" \
  "node --input-type=module - '${STAGING_HOST%%.*}' '$STAGING_PRIVATE_REPOSITORY' '$STAGING_MODEL'" <<'JS'
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';

let step = 'host identity';
const pass = label => console.log(`  ✓ ${label}`);
try {
  const [expectedHost, repository, intendedModel] = process.argv.slice(2);
  assert(execFileSync('hostname', {encoding: 'utf8'}).trim() === expectedHost);
  step = 'deployed gateway configuration';
  const state = path.join(os.homedir(), '.openclaw');
  const configPath = path.join(state, 'openclaw.json');
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  step = 'local gateway mode and token authentication';
  assert(config.gateway.mode === 'local');
  assert(config.gateway.auth?.mode === undefined || config.gateway.auth.mode === 'token');
  assert(typeof config.gateway.auth?.token === 'string' && config.gateway.auth.token.length > 0);
  step = 'matching deployed model to workflow';
  assert(config.agents.defaults.model.primary === intendedModel);
  step = 'permanent read-only staging policy';
  assert.deepEqual([...config.tools.allow].sort(), ['github_get_file_contents', 'github-test_get_file_contents'].sort());
  assert((config.tools.alsoAllow || []).length === 0);
  assert(config.agents.defaults.models[intendedModel]?.agentRuntime?.id === 'claude-cli');
  // OpenClaw's allowlist does not reach Claude's own tools; the launcher turns them off.
  step = 'container launches without native tools';
  const runtime = JSON.parse(fs.readFileSync(path.join(state, 'claude-cli-runtime.json'), 'utf8'));
  assert.deepEqual(runtime.extra_args, ['--tools', '']);
  const port = config.gateway.port || 18789;
  const run = args => JSON.parse(execFileSync('openclaw', args, {
    encoding: 'utf8', timeout: 180000, stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      HOME: os.homedir(), PATH: process.env.PATH, XDG_RUNTIME_DIR: `/run/user/${process.getuid()}`,
      OPENCLAW_CONFIG_PATH: path.join(state, 'openclaw.json'), OPENCLAW_STATE_DIR: state,
    },
  }));

  const [owner, repoName] = repository.split('/');
  const mainRead = {tool: 'github_get_file_contents', args: {owner, repo: repoName, path: ''}, sessionKey: 'agent:main:main'};
  step = 'unauthenticated gateway control';
  const unauthorized = await fetch(`http://127.0.0.1:${port}/tools/invoke`, {
    method: 'POST', signal: AbortSignal.timeout(20000), headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(mainRead),
  });
  step = `unauthenticated gateway control (HTTP ${unauthorized.status})`;
  assert(unauthorized.status === 401);
  pass('Gateway denies unauthenticated tool requests');

  step = 'private repository and anonymous-denied control';
  const servers = config.plugins.entries['openclaw-mcp-adapter'].config.servers;
  const main = servers.find(server => server.name === 'github');
  const token = main.env.GITHUB_PERSONAL_ACCESS_TOKEN;
  assert(typeof token === 'string' && token.length > 0);
  const url = `https://api.github.com/repos/${repository}`;
  const metadata = await fetch(url, {headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json'}, signal: AbortSignal.timeout(20000)});
  step = `repository metadata (HTTP ${metadata.status})`;
  assert(metadata.status === 200);
  const repo = await metadata.json();
  assert(repo.private === true && repo.full_name === repository);
  step = 'anonymous repository control';
  const anonymous = await fetch(url, {signal: AbortSignal.timeout(20000)});
  step = `anonymous repository control (HTTP ${anonymous.status})`;
  assert(anonymous.status === 404);
  for (const agent of ['main', 'test']) {
    step = `private MCP directory read for ${agent}`;
    const tool = `${agent === 'main' ? 'github' : 'github-test'}_get_file_contents`;
    const response = await fetch(`http://127.0.0.1:${port}/tools/invoke`, {
      method: 'POST', signal: AbortSignal.timeout(40000),
      headers: {Authorization: `Bearer ${config.gateway.auth.token}`, 'Content-Type': 'application/json'},
      body: JSON.stringify({...mainRead, tool, sessionKey: `agent:${agent}:main`}),
    });
    step = `private MCP directory read for ${agent} (HTTP ${response.status})`;
    assert(response.status === 200);
    const body = await response.json();
    assert(body.ok === true && !body.result.isError);
    const contents = JSON.parse(body.result.content.find(part => part.type === 'text').text);
    assert(Array.isArray(contents) && contents.length > 0);
    assert(contents.every(entry => ['file', 'dir', 'symlink', 'submodule'].includes(entry.type)));
    pass(`Private MCP directory read for ${agent}`);
  }
  // A prompt is not an access control. Disable all OpenClaw tools on this disposable
  // gateway, verify the policy against the previously working private reads,
  // then run inference. Restore the original policy even if inference fails.
  const writeInferencePolicy = disabled => {
    const current = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    current.tools = disabled ? {...config.tools, deny: ['*']} : config.tools;
    const temporary = `${configPath}.smoke.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(current), {mode: 0o600});
    fs.renameSync(temporary, configPath);
    // The gateway pins the config it started with; tools.* file
    // edits never reach it (reload class "none"). Restart so it runs this policy.
    step = `restarting the gateway to ${disabled ? 'disable' : 'restore'} tools`;
    execFileSync('systemctl', ['--user', 'restart', 'openclaw-gateway'], {
      timeout: 60000, stdio: ['ignore', 'pipe', 'pipe'],
      env: {HOME: os.homedir(), PATH: process.env.PATH, XDG_RUNTIME_DIR: `/run/user/${process.getuid()}`},
    });
  };
  const waitForToolPolicy = async denied => {
    for (const agent of ['main', 'test']) {
      step = `${denied ? 'disabling' : 'restoring'} tool access for ${agent}`;
      // A restart takes ~10-20 s on staging; refused connections mean "not up yet".
      const deadline = Date.now() + 120000;
      let applied = false;
      while (!applied && Date.now() < deadline) {
        let response;
        try {
          response = await fetch(`http://127.0.0.1:${port}/tools/invoke`, {
            method: 'POST', signal: AbortSignal.timeout(8000),
            headers: {Authorization: `Bearer ${config.gateway.auth.token}`, 'Content-Type': 'application/json'},
            body: JSON.stringify({...mainRead, tool: `${agent === 'main' ? 'github' : 'github-test'}_get_file_contents`, sessionKey: `agent:${agent}:main`}),
          });
        } catch (error) {
          if (!(error instanceof TypeError)) throw error;
          await new Promise(resolve => setTimeout(resolve, 1000));
          continue;
        }
        assert([200, 404].includes(response.status));
        applied = response.status === (denied ? 404 : 200);
        if (!applied) await new Promise(resolve => setTimeout(resolve, 1000));
      }
      assert(applied);
    }
  };
  const launchLog = path.join(state, 'claude-cli-invocations.jsonl');
  const launchesBefore = fs.existsSync(launchLog) ? fs.readFileSync(launchLog, 'utf8').split('\n').filter(Boolean).length : 0;
  try {
    writeInferencePolicy(true);
    await waitForToolPolicy(true);
    step = 'real gateway inference';
    const runId = randomUUID();
    const marker = `PHOENIX_INFERENCE_${runId}`;
    const expectedModel = intendedModel.split('/')[1];
    console.log(`    Inference run: ${runId}`);
    const response = run(['gateway', 'call', 'agent', '--expect-final', '--timeout', '150000', '--json', '--params', JSON.stringify({
      agentId: 'main', sessionKey: `agent:main:phoenix-${runId}`,
      idempotencyKey: runId, deliver: false, timeout: 120,
      message: `Reply with exactly this text and nothing else: ${marker}`,
    })]);
    // Direct gateway RPC has no embedded/local fallback.
    step = 'completed gateway inference';
    assert(response.status === 'ok');
    // The launcher logs each container with Claude's session ID, which names the transcript.
    step = 'inference ran in an agent container';
    const workspace = config.agents.entries.main.workspace || config.agents.defaults.workspace;
    const transcripts = path.join(os.homedir(), '.claude/projects', workspace.replace(/[^A-Za-z0-9]/g, '-'));
    const launches = fs.readFileSync(launchLog, 'utf8').split('\n').filter(Boolean).slice(launchesBefore).map(line => JSON.parse(line));
    assert(launches.some(launch => launch.agent === 'main' && launch.session
      && fs.readFileSync(path.join(transcripts, `${launch.session}.jsonl`), 'utf8').includes(marker)));
    step = 'exact inference reply';
    assert(response.result.payloads.length === 1 && response.result.payloads[0].text?.trim() === marker);
    step = 'expected inference model';
    assert(response.result.meta.agentMeta.model === expectedModel);
    step = 'nonzero inference output usage';
    assert(Number.isInteger(response.result.meta.agentMeta.usage.output) && response.result.meta.agentMeta.usage.output > 0);
    pass('Configured model produced a real response through the gateway with tools disabled');
    console.log(`    Model: ${response.result.meta.agentMeta.provider}/${response.result.meta.agentMeta.model}`);
  } finally {
    const inferenceStep = step;
    writeInferencePolicy(false);
    await waitForToolPolicy(false);
    step = inferenceStep;
  }
  console.log('Smoke test passed');
} catch (error) {
  // CLI output and JSON parser messages can contain secrets or private content.
  const detail = typeof error.status === 'number' ? `CLI exit ${error.status}`
    : ['SIGTERM', 'SIGKILL'].includes(error.signal) ? `CLI terminated by ${error.signal}`
    : error instanceof SyntaxError ? 'invalid JSON response'
    : error.code === 'ERR_ASSERTION' ? 'unexpected result' : 'request or local read failed';
  console.error(`Smoke test failed during ${step}: ${detail}. Raw output withheld.`);
  process.exitCode = 1;
}
JS
