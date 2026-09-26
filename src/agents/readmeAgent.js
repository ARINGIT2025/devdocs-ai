'use strict';

const fs = require('fs');
const path = require('path');
const { GitHelper } = require('../utils/gitHelper');

// ---------------------------------------------------------------------------
// Trigger rules — file-path patterns that indicate a README update may be
// needed.  README.md itself is explicitly excluded so it never triggers its
// own update.
// ---------------------------------------------------------------------------

/** Exact file names (basename only) that are always relevant. */
const RELEVANT_BASENAMES = new Set([
  'package.json',
  '.env.example',
]);

/**
 * Path segment prefixes (normalised to forward-slashes) whose presence
 * signals a relevant structural change.
 */
const RELEVANT_PREFIXES = [
  'src/',
  'app/',
  'bob/',
];

/**
 * Returns true when the normalised file path should trigger a README review.
 * @param {string} filePath
 * @returns {boolean}
 */
function _isRelevant(filePath) {
  // Normalise to forward-slashes for cross-platform comparisons.
  const normalised = filePath.replace(/\\/g, '/');
  const base = path.basename(normalised);

  // README.md changing does not trigger its own update.
  if (base === 'README.md') return false;

  if (RELEVANT_BASENAMES.has(base)) return true;

  return RELEVANT_PREFIXES.some((prefix) => normalised.startsWith(prefix));
}

// ---------------------------------------------------------------------------
// Context helpers — read project files gracefully (return null on absence)
// ---------------------------------------------------------------------------

/**
 * Attempts to read and JSON-parse a file.  Returns null on any error.
 * @param {GitHelper} git
 * @param {string}    relPath
 * @returns {Promise<object|null>}
 */
async function _readJson(git, relPath) {
  try {
    const raw = await git.readFile(relPath);
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/**
 * Attempts to read a text file.  Returns null on any error.
 * @param {GitHelper} git
 * @param {string}    relPath
 * @returns {Promise<string|null>}
 */
async function _readText(git, relPath) {
  try {
    return await git.readFile(relPath);
  } catch {
    return null;
  }
}

/**
 * Returns a sorted list of file paths under a directory, relative to
 * repoPath.  Returns an empty array if the directory does not exist.
 * @param {string} repoPath  Absolute path to the repo root.
 * @param {string} relDir    Directory path relative to repoPath.
 * @returns {Promise<string[]>}
 */
async function _listDir(repoPath, relDir) {
  const abs = path.join(repoPath, relDir);
  try {
    const entries = await fs.promises.readdir(abs, { withFileTypes: true });
    return entries
      .filter((e) => !e.name.startsWith('.'))
      .map((e) => path.join(relDir, e.name).replace(/\\/g, '/'))
      .sort();
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// README generator — deterministic, no LLM
// ---------------------------------------------------------------------------

/**
 * Builds the env-vars section from the contents of .env.example.
 * @param {string|null} envExample
 * @returns {string}
 */
function _buildEnvSection(envExample) {
  if (!envExample || !envExample.trim()) return '';

  const vars = envExample
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'));

  if (vars.length === 0) return '';

  const rows = vars
    .map((l) => {
      const [key, ...rest] = l.split('=');
      const value = rest.join('=').trim();
      const desc = value ? `Example: \`${value}\`` : 'Required';
      return `| \`${key.trim()}\` | ${desc} |`;
    })
    .join('\n');

  return [
    '## Configuration',
    '',
    'Copy `.env.example` to `.env` and fill in the values:',
    '',
    '| Variable | Notes |',
    '|---|---|',
    rows,
    '',
  ].join('\n');
}

/**
 * Builds the project-structure section from the known src/ tree.
 * @param {string[]} srcFiles  Paths relative to repo root under src/.
 * @returns {string}
 */
function _buildStructureSection(srcFiles) {
  if (srcFiles.length === 0) return '';

  // Group by immediate sub-directory of src/.
  const groups = {};
  for (const f of srcFiles) {
    const parts = f.replace(/\\/g, '/').split('/');
    // parts[0] === 'src', parts[1] === sub-dir or file
    const group = parts.length > 2 ? parts[1] : '';
    if (!groups[group]) groups[group] = [];
    groups[group].push(parts.slice(2).join('/') || parts[1]);
  }

  const lines = ['## Project Structure', '', '```', 'src/'];
  for (const [group, files] of Object.entries(groups).sort()) {
    if (group) {
      lines.push(`├── ${group}/`);
      files.forEach((f) => lines.push(`│   └── ${f}`));
    } else {
      files.forEach((f) => lines.push(`└── ${f}`));
    }
  }
  lines.push('```', '');
  return lines.join('\n');
}

/**
 * Builds the dependencies section from package.json's `dependencies` map.
 * @param {object|null} pkg
 * @returns {string}
 */
function _buildDepsSection(pkg) {
  if (!pkg || !pkg.dependencies) return '';

  const deps = Object.entries(pkg.dependencies);
  if (deps.length === 0) return '';

  const rows = deps
    .map(([name, ver]) => `| \`${name}\` | \`${ver}\` |`)
    .join('\n');

  return [
    '## Dependencies',
    '',
    '| Package | Version |',
    '|---|---|',
    rows,
    '',
  ].join('\n');
}

// ---------------------------------------------------------------------------
// ReadmeAgent
// ---------------------------------------------------------------------------

/**
 * Detects when README.md may be stale and regenerates it from the actual
 * repository context using deterministic rule-based logic (no LLM).
 *
 * @example
 * const agent = new ReadmeAgent();
 * const result = await agent.run({ dryRun: true });
 * console.log(result);
 */
class ReadmeAgent {
  /**
   * @param {GitHelper} [gitHelper]  Provide a pre-configured GitHelper, or
   *   leave blank to create a default one pointing at the repo root.
   */
  constructor(gitHelper) {
    this.git = gitHelper || new GitHelper();
  }

  // -------------------------------------------------------------------------
  // analyze
  // -------------------------------------------------------------------------

  /**
   * Inspects a list of changed file paths and decides whether README.md needs
   * to be updated.
   *
   * @param {string[]} changedFiles  File paths as returned by GitHelper.getChangedFiles().
   * @returns {{
   *   needsUpdate: boolean,
   *   reasons: string[],
   *   triggers: string[]
   * }}
   *   `needsUpdate`  — whether at least one relevant change was found.
   *   `reasons`      — human-readable explanation for each trigger.
   *   `triggers`     — the specific file paths that caused the decision.
   */
  analyze(changedFiles) {
    if (!Array.isArray(changedFiles)) {
      throw new TypeError('analyze: changedFiles must be an array.');
    }

    const triggers = changedFiles.filter(_isRelevant);
    const reasons = triggers.map((f) => {
      const norm = f.replace(/\\/g, '/');
      if (norm === 'package.json') return 'package.json changed (name, version, scripts, or dependencies may have changed)';
      if (norm === '.env.example') return '.env.example changed (environment variable requirements may have changed)';
      if (norm.startsWith('src/agents/')) return `New or modified agent: ${f}`;
      if (norm.startsWith('src/cli/')) return `CLI entry point changed: ${f}`;
      if (norm.startsWith('src/utils/')) return `Utility changed: ${f}`;
      if (norm.startsWith('src/')) return `Source file changed: ${f}`;
      if (norm.startsWith('app/')) return `Python source changed: ${f}`;
      if (norm.startsWith('bob/')) return `Bob agent setup changed: ${f}`;
      return `Relevant file changed: ${f}`;
    });

    return {
      needsUpdate: triggers.length > 0,
      reasons,
      triggers,
    };
  }

  // -------------------------------------------------------------------------
  // generate
  // -------------------------------------------------------------------------

  /**
   * Builds a README markdown string from the actual repository context.
   * Preserves any substantive existing README content when it is present.
   * Does not invent features, commands, or configuration that do not exist.
   *
   * @param {{
   *   pkg:        object|null,
   *   envExample: string|null,
   *   srcFiles:   string[],
   *   existing:   string|null,
   * }} repoContext  As returned by _gatherContext().
   * @returns {string} Full README markdown content.
   */
  generate(repoContext) {
    const { pkg, envExample, srcFiles, existing } = repoContext;

    const name = pkg ? (pkg.name || 'devdocs-ai') : 'devdocs-ai';
    const version = pkg ? (pkg.version || '') : '';
    const description = pkg ? (pkg.description || '') : '';
    const repoUrl = pkg && pkg.repository
      ? (typeof pkg.repository === 'string' ? pkg.repository : pkg.repository.url || '')
      : '';
    const homepage = pkg ? (pkg.homepage || repoUrl) : '';

    // If the existing README is substantive (> 100 chars of non-whitespace),
    // return it unchanged — only a blank or near-blank README gets replaced.
    if (existing && existing.replace(/\s/g, '').length > 100) {
      return existing;
    }

    const sections = [];

    // --- Header ---
    sections.push(`# ${name}${version ? ` \`v${version}\`` : ''}`);
    sections.push('');
    if (description) {
      sections.push(description);
      sections.push('');
    }

    if (homepage) {
      sections.push(`> Repository: ${homepage}`);
      sections.push('');
    }

    // --- Overview ---
    sections.push('## Overview');
    sections.push('');
    sections.push(
      'DevDocs AI is an automated documentation pipeline that analyses a repository\'s ' +
      'source code and generates or updates developer-facing documentation including ' +
      'README files, API references, changelogs, and tutorials.'
    );
    sections.push('');

    // --- Getting Started ---
    sections.push('## Getting Started');
    sections.push('');
    sections.push('**Prerequisites:** Node.js >= 20, Python >= 3.9, Git');
    sections.push('');
    sections.push('```bash');
    sections.push('git clone https://github.com/ARINGIT2025/devdocs-ai.git');
    sections.push('cd devdocs-ai');
    sections.push('');
    sections.push('# Install Node.js dependencies');
    sections.push('npm install');
    sections.push('');
    sections.push('# Install Python dependencies');
    sections.push('pip install -r requirements.txt');
    sections.push('```');
    sections.push('');

    // --- Configuration (from .env.example) ---
    const envSection = _buildEnvSection(envExample);
    if (envSection) sections.push(envSection);

    // --- Project Structure (from src/) ---
    const structureSection = _buildStructureSection(srcFiles);
    if (structureSection) sections.push(structureSection);

    // --- Dependencies (from package.json) ---
    const depsSection = _buildDepsSection(pkg);
    if (depsSection) sections.push(depsSection);

    // --- License ---
    const license = pkg ? (pkg.license || '') : '';
    if (license) {
      sections.push('## License');
      sections.push('');
      sections.push(`Licensed under the **${license}** license.`);
      sections.push('');
    }

    return sections.join('\n');
  }

  // -------------------------------------------------------------------------
  // write
  // -------------------------------------------------------------------------

  /**
   * Writes content to `README.md` at the repository root.
   * Uses GitHelper.writeFile — does NOT commit or push.
   *
   * @param {string} content  Full README markdown to write.
   * @returns {Promise<void>}
   */
  async write(content) {
    if (typeof content !== 'string') {
      throw new TypeError('write: content must be a string.');
    }
    await this.git.writeFile('README.md', content);
  }

  // -------------------------------------------------------------------------
  // run
  // -------------------------------------------------------------------------

  /**
   * Executes the full README update workflow.
   *
   * @param {{dryRun?: boolean}} [options]
   * @returns {Promise<{
   *   updated:      boolean,
   *   dryRun:       boolean,
   *   needsUpdate:  boolean,
   *   reasons:      string[],
   *   triggers:     string[],
   *   content:      string|null,
   * }>}
   */
  async run(options = {}) {
    const dryRun = options.dryRun === true;

    // 1. Obtain changed files.
    const changedFiles = await this.git.getChangedFiles();

    // 2. Analyse whether an update is needed.
    const analysis = this.analyze(changedFiles);

    if (!analysis.needsUpdate) {
      return {
        updated: false,
        dryRun,
        needsUpdate: false,
        reasons: [],
        triggers: [],
        content: null,
      };
    }

    // 3. Gather repository context.
    const repoContext = await this._gatherContext();

    // 4. Generate README content.
    const content = this.generate(repoContext);

    // 5. Write only when allowed.
    if (!dryRun) {
      await this.write(content);
    }

    return {
      updated: !dryRun,
      dryRun,
      needsUpdate: true,
      reasons: analysis.reasons,
      triggers: analysis.triggers,
      content,
    };
  }

  // -------------------------------------------------------------------------
  // Internal helpers
  // -------------------------------------------------------------------------

  /**
   * Collects the repository context data used by `generate()`.
   * All reads are graceful — missing files produce null/[].
   *
   * @returns {Promise<{
   *   pkg:        object|null,
   *   envExample: string|null,
   *   srcFiles:   string[],
   *   existing:   string|null,
   * }>}
   */
  async _gatherContext() {
    const [pkg, envExample, existing, srcFiles] = await Promise.all([
      _readJson(this.git, 'package.json'),
      _readText(this.git, '.env.example'),
      _readText(this.git, 'README.md'),
      _listDir(this.git.repoPath, 'src'),
    ]);

    return { pkg, envExample, srcFiles, existing };
  }
}

module.exports = { ReadmeAgent };
