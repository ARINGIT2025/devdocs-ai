#!/usr/bin/env node
'use strict';

require('dotenv').config();

const { Command } = require('commander');
const { DevDocsOrchestrator } = require('../orchestrator');
const { ReadmeAgent } = require('../agents/readmeAgent');
const { GitHelper } = require('../utils/gitHelper');

// ---------------------------------------------------------------------------
// Output helpers — intentionally minimal, no external logger dependency
// ---------------------------------------------------------------------------

const log = {
  info:    (...a) => console.log('[devdocs]', ...a),
  success: (...a) => console.log('[devdocs] ✔', ...a),
  warn:    (...a) => console.warn('[devdocs] ⚠', ...a),
  error:   (...a) => console.error('[devdocs] ✖', ...a),
};

/**
 * Prints a structured summary of orchestrator/agent results to stdout.
 * Never prints env var values.
 * @param {{
 *   agentCount:   number,
 *   dryRun:       boolean,
 *   changedFiles: string[],
 *   results:      Array<{agent:string, status:string, result:object|null, error:string|null}>,
 *   summary:      {fulfilled:number, rejected:number},
 * }} result
 */
function printRunSummary(result) {
  log.info(`Agents run: ${result.agentCount} | changed files: ${result.changedFiles.length}`);
  if (result.dryRun) log.info('Mode: dry-run — no files were written');

  for (const r of result.results) {
    if (r.status === 'fulfilled') {
      const res = r.result || {};
      const detail = res.needsUpdate === false
        ? 'no update needed'
        : res.updated
          ? 'updated'
          : result.dryRun ? 'would update (dry-run)' : 'processed';
      log.success(`${r.agent}: ${detail}`);
    } else {
      log.error(`${r.agent} failed: ${r.error}`);
    }
  }

  const { fulfilled, rejected } = result.summary;
  log.info(`Summary: ${fulfilled} succeeded, ${rejected} failed`);
  if (rejected > 0) process.exitCode = 1;
}

// ---------------------------------------------------------------------------
// Command handlers
// ---------------------------------------------------------------------------

/**
 * `devdocs run` — run the full documentation pipeline.
 * @param {{dryRun: boolean, since?: string}} opts
 */
async function cmdRun(opts) {
  try {
    log.info(`Starting documentation pipeline${opts.dryRun ? ' (dry-run)' : ''}...`);
    const orchestrator = DevDocsOrchestrator.create();
    const result = await orchestrator.run({ dryRun: opts.dryRun, since: opts.since });

    if (result.agentCount === 0) {
      log.warn('No agents registered — nothing to do.');
      return;
    }

    printRunSummary(result);
  } catch (err) {
    log.error(`run failed: ${err.message}`);
    process.exit(1);
  }
}

/**
 * `devdocs update-readme` — run ReadmeAgent in isolation.
 * @param {{dryRun: boolean}} opts
 */
async function cmdUpdateReadme(opts) {
  try {
    log.info(`Updating README${opts.dryRun ? ' (dry-run)' : ''}...`);
    const agent = new ReadmeAgent();
    const result = await agent.run({ dryRun: opts.dryRun });

    if (!result.needsUpdate) {
      log.info('README is already up to date — no changes needed.');
      return;
    }

    if (result.dryRun) {
      log.info('Dry-run: README would be updated. Triggers:');
      result.triggers.forEach((t) => log.info(`  • ${t}`));
      log.info('Generated content preview (first 300 chars):');
      log.info((result.content || '').slice(0, 300).replace(/\n/g, '\n          '));
    } else {
      log.success('README.md updated successfully.');
      log.info('Triggers:');
      result.triggers.forEach((t) => log.info(`  • ${t}`));
    }
  } catch (err) {
    log.error(`update-readme failed: ${err.message}`);
    process.exit(1);
  }
}

/**
 * `devdocs watch` — poll for git changes and trigger the pipeline.
 *
 * Implementation uses setInterval polling (no fs.watch, which is unreliable
 * across platforms for git-tree changes).  Default interval is 30 s — low
 * enough to feel responsive, high enough not to thrash the git index.
 *
 * @param {{dryRun: boolean, interval: string}} opts
 */
async function cmdWatch(opts) {
  const intervalSec = Math.max(5, parseInt(opts.interval, 10) || 30);
  const dryRun = opts.dryRun;

  log.info(`Watch mode started (interval: ${intervalSec}s${dryRun ? ', dry-run' : ''}).`);
  log.info('Press Ctrl+C to stop.');

  const git = new GitHelper();

  // Track the set of changed files seen on the previous tick so we only
  // trigger the pipeline when the working-tree state actually changes.
  let prevSnapshot = null;

  /**
   * Returns a stable string key for the current changed-file set.
   * @returns {Promise<string>}
   */
  async function snapshot() {
    try {
      const files = await git.getChangedFiles();
      return files.slice().sort().join('\0');
    } catch {
      return '';
    }
  }

  /** Runs one pipeline tick. */
  async function tick() {
    const current = await snapshot();

    if (current === prevSnapshot) return; // nothing changed
    prevSnapshot = current;

    if (!current) {
      log.info('No changed files detected.');
      return;
    }

    log.info('Changes detected — running pipeline...');
    try {
      const orchestrator = DevDocsOrchestrator.create({ gitHelper: git });
      const result = await orchestrator.run({ dryRun });
      printRunSummary(result);
      // Reset exitCode after each successful tick — watch mode should stay alive.
      process.exitCode = 0;
    } catch (err) {
      log.error(`Pipeline error: ${err.message}`);
    }
  }

  // Run immediately on start, then on each interval.
  await tick();
  const timer = setInterval(tick, intervalSec * 1000);

  // Graceful shutdown on Ctrl+C / SIGTERM.
  function shutdown(signal) {
    log.info(`\nReceived ${signal} — stopping watch mode.`);
    clearInterval(timer);
    process.exit(0);
  }
  process.on('SIGINT',  () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

// ---------------------------------------------------------------------------
// CLI definition
// ---------------------------------------------------------------------------

const program = new Command();

program
  .name('devdocs')
  .description('DevDocs AI — automated documentation pipeline for your repository')
  .version('1.0.0', '-v, --version', 'print version number');

// ── run ──────────────────────────────────────────────────────────────────────
program
  .command('run')
  .description('Run the full documentation pipeline (all registered agents)')
  .option('--dry-run', 'Analyse and generate without writing any files', false)
  .option('--since <sha>', 'Limit change detection to commits after this SHA/ref')
  .action(async (opts) => {
    await cmdRun({ dryRun: opts.dryRun, since: opts.since });
  });

// ── update-readme ─────────────────────────────────────────────────────────────
program
  .command('update-readme')
  .description('Run the README auto-updater agent only')
  .option('--dry-run', 'Analyse and generate without writing README.md', false)
  .action(async (opts) => {
    await cmdUpdateReadme({ dryRun: opts.dryRun });
  });

// ── watch ─────────────────────────────────────────────────────────────────────
program
  .command('watch')
  .description('Watch the repository for changes and trigger the pipeline automatically')
  .option('--dry-run', 'Run pipeline in dry-run mode (no files written)', false)
  .option('--interval <seconds>', 'Polling interval in seconds (minimum 5)', '30')
  .action(async (opts) => {
    await cmdWatch({ dryRun: opts.dryRun, interval: opts.interval });
  });

// ---------------------------------------------------------------------------
// Parse — only when executed directly (not when required in tests)
// ---------------------------------------------------------------------------
if (require.main === module) {
  program.parseAsync(process.argv).catch((err) => {
    log.error(err.message);
    process.exit(1);
  });
}

module.exports = { program };
