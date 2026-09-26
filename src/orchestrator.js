'use strict';

const { GitHelper } = require('./utils/gitHelper');
const { ReadmeAgent } = require('./agents/readmeAgent');

// ---------------------------------------------------------------------------
// DevDocsOrchestrator
// ---------------------------------------------------------------------------

/**
 * Coordinates multiple documentation agents across the DevDocs AI pipeline.
 *
 * Agents are registered with `registerAgent()` and run in parallel via
 * `runAgentsParallel()`.  One agent failing does not prevent others from
 * completing.  The orchestrator never commits, pushes, or creates PRs — those
 * decisions are left to the CLI layer.
 *
 * @example
 * // Default setup — ReadmeAgent pre-registered
 * const { DevDocsOrchestrator } = require('./src/orchestrator');
 * const orchestrator = DevDocsOrchestrator.create();
 * const result = await orchestrator.run({ dryRun: true });
 *
 * @example
 * // Manual setup
 * const orchestrator = new DevDocsOrchestrator();
 * orchestrator.registerAgent(new ReadmeAgent());
 * const result = await orchestrator.run({ dryRun: false });
 */
class DevDocsOrchestrator {
  /**
   * @param {object}    [opts]
   * @param {GitHelper} [opts.gitHelper]  Pre-configured GitHelper instance.
   *   Defaults to a new GitHelper pointing at the repo root.
   */
  constructor(opts = {}) {
    this.git = opts.gitHelper || new GitHelper();
    /** @type {Array<{name: string, agent: object}>} */
    this._agents = [];
  }

  // -------------------------------------------------------------------------
  // Factory
  // -------------------------------------------------------------------------

  /**
   * Creates an orchestrator with ReadmeAgent pre-registered.
   * This is the recommended entry point for the default pipeline.
   *
   * @param {object}    [opts]           Passed through to the constructor.
   * @param {GitHelper} [opts.gitHelper] Shared GitHelper (optional).
   * @returns {DevDocsOrchestrator}
   */
  static create(opts = {}) {
    const gitHelper = opts.gitHelper || new GitHelper();
    const orchestrator = new DevDocsOrchestrator({ gitHelper });
    orchestrator.registerAgent(new ReadmeAgent(gitHelper));
    return orchestrator;
  }

  // -------------------------------------------------------------------------
  // registerAgent
  // -------------------------------------------------------------------------

  /**
   * Registers an agent instance with the orchestrator.
   *
   * Validation rules:
   *   - The agent must expose a callable `run` method.
   *   - An agent with the same constructor name may only be registered once
   *     (prevents accidental double-registration of the same class).
   *
   * @param {object} agent  Any object with an async `run(options)` method.
   * @returns {DevDocsOrchestrator}  Returns `this` for fluent chaining.
   * @throws {TypeError}  If the agent has no callable `run` method.
   * @throws {Error}      If an agent of the same type is already registered.
   */
  registerAgent(agent) {
    if (!agent || typeof agent.run !== 'function') {
      throw new TypeError(
        'registerAgent: agent must expose a callable run() method.'
      );
    }

    const name = agent.constructor ? agent.constructor.name : 'UnknownAgent';

    const duplicate = this._agents.find((entry) => entry.name === name);
    if (duplicate) {
      throw new Error(
        `registerAgent: an agent of type "${name}" is already registered. ` +
        'Remove the existing registration before adding another.'
      );
    }

    this._agents.push({ name, agent });
    return this;
  }

  // -------------------------------------------------------------------------
  // processChanges
  // -------------------------------------------------------------------------

  /**
   * Obtains the current changed files via GitHelper and builds the shared
   * context object that is passed to every agent.
   *
   * @param {object} [options]  The options object from `run()`.
   * @returns {Promise<{
   *   changedFiles: string[],
   *   repoPath:     string,
   *   options:      object,
   * }>}
   */
  async processChanges(options = {}) {
    const changedFiles = await this.git.getChangedFiles();

    return {
      changedFiles,
      repoPath: this.git.repoPath,
      options,
    };
  }

  // -------------------------------------------------------------------------
  // runAgentsParallel
  // -------------------------------------------------------------------------

  /**
   * Runs all registered agents concurrently.  Uses `Promise.allSettled` so
   * that a failure in one agent is captured and reported without aborting the
   * others.
   *
   * Each agent receives the shared `context.options` object (which includes
   * `dryRun`, `since`, etc.) — it is the agent's own responsibility to
   * honour those flags.
   *
   * @param {{
   *   changedFiles: string[],
   *   repoPath:     string,
   *   options:      object,
   * }} context  As returned by `processChanges()`.
   * @returns {Promise<Array<{
   *   agent:   string,
   *   status:  'fulfilled' | 'rejected',
   *   result:  object | null,
   *   error:   string | null,
   * }>>}
   */
  async runAgentsParallel(context) {
    if (this._agents.length === 0) {
      return [];
    }

    const settled = await Promise.allSettled(
      this._agents.map(({ agent }) => agent.run(context.options))
    );

    return settled.map((outcome, idx) => {
      const { name } = this._agents[idx];
      if (outcome.status === 'fulfilled') {
        return {
          agent: name,
          status: 'fulfilled',
          result: outcome.value,
          error: null,
        };
      }
      // 'rejected' — capture the error without rethrowing.
      const err = outcome.reason;
      return {
        agent: name,
        status: 'rejected',
        result: null,
        error: err instanceof Error ? err.message : String(err),
      };
    });
  }

  // -------------------------------------------------------------------------
  // run
  // -------------------------------------------------------------------------

  /**
   * Executes the complete orchestration workflow:
   *   1. Build shared context (changed files + repo info).
   *   2. Run all registered agents in parallel.
   *   3. Return a structured summary — does NOT commit, push, or open PRs.
   *
   * @param {object}  [options]
   * @param {boolean} [options.dryRun=false]  When true, no files are written.
   *   Forwarded to every registered agent.
   * @param {string}  [options.since]         Optional git ref / SHA.
   *   Passed through to agents via context — agents that understand `since`
   *   may use it to scope their analysis.
   * @returns {Promise<{
   *   agentCount:   number,
   *   dryRun:       boolean,
   *   changedFiles: string[],
   *   repoPath:     string,
   *   results:      Array<object>,
   *   summary:      { fulfilled: number, rejected: number },
   * }>}
   */
  async run(options = {}) {
    const dryRun = options.dryRun === true;

    // No agents registered — return a useful no-op result.
    if (this._agents.length === 0) {
      return {
        agentCount: 0,
        dryRun,
        changedFiles: [],
        repoPath: this.git.repoPath,
        results: [],
        summary: { fulfilled: 0, rejected: 0 },
      };
    }

    // 1. Build shared context.
    const context = await this.processChanges({ ...options, dryRun });

    // 2. Run agents in parallel.
    const results = await this.runAgentsParallel(context);

    // 3. Build summary counts.
    const summary = results.reduce(
      (acc, r) => {
        acc[r.status === 'fulfilled' ? 'fulfilled' : 'rejected']++;
        return acc;
      },
      { fulfilled: 0, rejected: 0 }
    );

    return {
      agentCount: this._agents.length,
      dryRun,
      changedFiles: context.changedFiles,
      repoPath: context.repoPath,
      results,
      summary,
    };
  }
}

module.exports = { DevDocsOrchestrator };
