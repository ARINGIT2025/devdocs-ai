'use strict';

require('dotenv').config();

const fs = require('fs');
const path = require('path');
const { simpleGit } = require('simple-git');

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Lazily loads @octokit/rest (ESM-only) via dynamic import so this CJS module
 * can use it without converting the whole project to ESM.
 * @returns {Promise<import('@octokit/rest').Octokit>}
 */
async function _buildOctokit() {
  const token = process.env.GITHUB_TOKEN;
  if (!token) {
    throw new Error(
      'GITHUB_TOKEN is not set. Add it to your .env file or environment.'
    );
  }
  const { Octokit } = await import('@octokit/rest');
  return new Octokit({ auth: token });
}

// ---------------------------------------------------------------------------
// GitHelper
// ---------------------------------------------------------------------------

/**
 * Reusable helper that wraps simple-git (local operations) and @octokit/rest
 * (GitHub API operations).  All methods are async.
 *
 * @example
 * const helper = new GitHelper();                     // cwd = repo root
 * const helper = new GitHelper('/path/to/repo');      // explicit root
 */
class GitHelper {
  /**
   * @param {string} [repoPath] Absolute or relative path to the git repo root.
   *   Defaults to the directory two levels above this file (repo root).
   */
  constructor(repoPath) {
    this.repoPath = repoPath || path.resolve(__dirname, '..', '..');
    this.git = simpleGit(this.repoPath);
    /** @type {import('@octokit/rest').Octokit | null} */
    this._octokit = null;
  }

  /**
   * Returns a cached Octokit instance, creating it on first call.
   * Throws if GITHUB_TOKEN is missing.
   * @returns {Promise<import('@octokit/rest').Octokit>}
   */
  async _getOctokit() {
    if (!this._octokit) {
      this._octokit = await _buildOctokit();
    }
    return this._octokit;
  }

  // -------------------------------------------------------------------------
  // Local git operations
  // -------------------------------------------------------------------------

  /**
   * Returns the unified diff of the current working tree against HEAD.
   * Includes both staged and unstaged changes.
   * @returns {Promise<string>} Raw diff text (empty string when nothing changed).
   */
  async getLatestDiff() {
    const [staged, unstaged] = await Promise.all([
      this.git.diff(['HEAD']),
      this.git.diff(),
    ]);
    return [staged, unstaged].filter(Boolean).join('\n');
  }

  /**
   * Returns a deduplicated list of file paths that differ from HEAD.
   * Covers staged changes, unstaged changes, and untracked files.
   * @returns {Promise<string[]>}
   */
  async getChangedFiles() {
    const status = await this.git.status();
    const files = new Set([
      ...status.staged,
      ...status.modified,
      ...status.not_added,   // untracked
      ...status.created,
      ...status.deleted,
      ...status.renamed.map((r) => r.to),
    ]);
    return Array.from(files).filter(Boolean);
  }

  /**
   * Reads a file from disk relative to the repo root.
   * @param {string} filePath Path relative to the repo root, or absolute.
   * @returns {Promise<string>} UTF-8 file contents.
   * @throws If the file does not exist or cannot be read.
   */
  async readFile(filePath) {
    const resolved = path.isAbsolute(filePath)
      ? filePath
      : path.join(this.repoPath, filePath);
    return fs.promises.readFile(resolved, 'utf8');
  }

  /**
   * Writes content to a file relative to the repo root.
   * Creates intermediate directories if they do not exist.
   * @param {string} filePath Path relative to the repo root, or absolute.
   * @param {string} content  Content to write (UTF-8).
   * @returns {Promise<void>}
   */
  async writeFile(filePath, content) {
    const resolved = path.isAbsolute(filePath)
      ? filePath
      : path.join(this.repoPath, filePath);
    await fs.promises.mkdir(path.dirname(resolved), { recursive: true });
    await fs.promises.writeFile(resolved, content, 'utf8');
  }

  /**
   * Reads and parses `package.json` at the repo root, then returns its
   * `version` field.
   * @returns {Promise<string>} The semver version string (e.g. `"1.0.0"`).
   * @throws If `package.json` is missing or has no `version` field.
   */
  async getPackageVersion() {
    const raw = await this.readFile('package.json');
    const pkg = JSON.parse(raw);
    if (!pkg.version) {
      throw new Error('package.json does not contain a "version" field.');
    }
    return pkg.version;
  }

  // -------------------------------------------------------------------------
  // GitHub API operations
  // -------------------------------------------------------------------------

  /**
   * Stages the given files, creates a commit, and pushes to the current branch.
   * @param {string}   message Commit message.
   * @param {string[]} files   Paths to stage (relative to repo root). Pass
   *   `['.']` to stage everything.
   * @returns {Promise<void>}
   */
  async commitAndPush(message, files) {
    if (!files || files.length === 0) {
      throw new Error('commitAndPush: at least one file path is required.');
    }
    await this.git.add(files);
    await this.git.commit(message);
    await this.git.push();
  }

  /**
   * Opens a pull request on GitHub.
   * Requires GITHUB_TOKEN to be set and the remote to be a github.com repo.
   *
   * @param {object} opts
   * @param {string} opts.owner  GitHub owner (user or org).
   * @param {string} opts.repo   Repository name (without owner).
   * @param {string} opts.title  PR title.
   * @param {string} opts.body   PR description (markdown).
   * @param {string} opts.head   Branch to merge from (e.g. `"my-feature"`).
   * @param {string} opts.base   Branch to merge into (default `"main"`).
   * @returns {Promise<{number: number, url: string}>} PR number and HTML URL.
   */
  async openPullRequest({ owner, repo, title, body, head, base = 'main' }) {
    const octokit = await this._getOctokit();
    const { data } = await octokit.rest.pulls.create({
      owner,
      repo,
      title,
      body,
      head,
      base,
    });
    return { number: data.number, url: data.html_url };
  }

  /**
   * Returns the most recent commit message on the current branch.
   * @returns {Promise<string>}
   */
  async getLastCommitMessage() {
    const log = await this.git.log({ maxCount: 1 });
    if (!log.latest) {
      throw new Error('No commits found in this repository.');
    }
    return log.latest.message;
  }
}

module.exports = { GitHelper };
