# AIHub

A local AI task coordinator with a browser dashboard, human approval gates and manual handoffs to Codex, Claude and Gemini. Built with Python, SQLite and vanilla JavaScript.

**Status: v0.4 development portfolio snapshot.** This is a single-user local application, not a hosted service or an autonomous coding agent. The public repository starts with clean history and synthetic project profiles; private databases, logs, internal audit documents and machine-specific verification scripts are excluded.

## Implemented

- Create missions and use a local Ollama model to propose structured tasks for Researcher, Writer, Coder and Reviewer roles.
- Validate model JSON on the server, reject unknown fields/roles and allow at most one repair attempt.
- Create and edit project context profiles, including privacy and preferred-worker settings.
- Review, edit and approve tasks; validate dependency graphs and block execution until prerequisites are ready.
- Review dependency results before approval. Changes invalidate approvals or stale downstream context.
- Run approved local text-generation tasks through Ollama, with persistent execution state and error reporting.
- Prepare editable manual prompts for Codex, Claude or Gemini; copy them, explicitly confirm external work started, then confirm result import.
- Preserve result revisions, review decisions and handoff snapshots in SQLite. Copying a prompt does not complete a task.
- Browse mission history, search/filter missions, view progress and export a text report from the dark dashboard.

External provider names describe **manual copy/paste destinations**. AIHub does not call their APIs, launch their CLIs, read their authentication or automatically verify their reports.

## Engineering highlights

The backend separates HTTP routing, input validation, coordination, Ollama communication, provider descriptions and SQLite persistence. Versioned database migrations, transactions, revision history and approval checks support recoverable workflows. The frontend uses plain HTML/CSS/JavaScript without a build step.

The automated tests cover validation, HTTP protections, mocked Ollama failures, task states, approvals, migrations, persistence, dependency handoffs, stale results and manual-provider workflows. They use temporary databases and fake model clients; they do not execute external AI agents.

## Run locally

Tested with Python 3.13 on Windows. No third-party Python packages are required.

```powershell
git clone https://github.com/VasvariMark/aihub.git
cd aihub
python -B -m aihub
```

Open **http://127.0.0.1:8765/**. Stop with Ctrl+C. Use the exact loopback URL, not a public hostname.

For mission planning and local execution, Ollama must already be running at `http://127.0.0.1:11434` with the selected model installed. The default is `qwen3.5:9b`; AIHub does not download models. Ollama/model setup is separate from this repository.

```powershell
python -B -m aihub --model qwen3.5:9b --context 4096 --port 8765
```

The application creates its own `data/aihub.sqlite3` and logs on first startup. Start this fresh checkout independently of any existing AIHub data directory. Do not point an older release at a newer database. Back up real data with SQLite's backup API before upgrades; raw copying a running WAL database is unsafe.

## Typical workflow

1. Edit a demo project profile or create your own, then enter a mission.
2. Inspect the proposed tasks, dependencies, worker choice and context.
3. Approve each task. Run local tasks in AIHub.
4. For a manual worker, review the complete prompt, copy it to your chosen external application and confirm you started work.
5. Paste the report back and explicitly confirm import. Review unverified claims before accepting results for downstream tasks.

## Tests

```powershell
python -B -m unittest tests.test_app tests.test_v2 tests.test_manual tests.test_v04 -q
```

This portfolio copy retains 117 automated tests. Live external-provider execution is not part of these tests. The publication check does not claim a new live Ollama or browser end-to-end run.

## Source layout

- `aihub/server.py`: loopback HTTP server and request protections
- `aihub/coordinator.py`, `context.py`, `models.py`: task workflow, context budgeting and validation
- `aihub/ollama.py`: local model API client
- `aihub/manual.py`, `providers.py`: manual prompts and worker registry
- `aihub/storage.py`, `projects.py`, `revisions.py`: persistence and history
- `aihub/static/`: dashboard HTML, CSS and JavaScript
- `tests/`: standard-library unittest suites

## Security and limitations

The server binds to 127.0.0.1 and applies Host, Origin and CSRF checks. Do not expose it through a reverse proxy, port forwarding or a public network. It is not a multi-user authentication system.

Generated and imported text is untrusted. Validation and prompt separation reduce risks but cannot guarantee factual accuracy or prevent every prompt-injection attempt. Review external prompts before copying: their contents leave the local privacy boundary when you paste them elsewhere. Provider accounts and any usage charges or subscription limits are managed separately; no API entitlement is assumed.

Automatic Codex execution remains disabled because reliable Windows filesystem isolation was not verified. No dashboard shell execution or automatic repository edits are provided. Local execution is serialized; cancellation is limited to queued work. Context budgets are conservative and can reject long inputs instead of silently truncating them.

## Planned / not implemented

- Automated cloud-provider API adapters and usage accounting
- Verified sandboxed external workers and isolated repository editing
- More extensive live-provider and browser integration testing

These are possible next steps, not current capabilities or delivery commitments.

## AI assistance

This is an AI-assisted development project. AI tools contributed to implementation, tests, documentation and troubleshooting. It is presented as a portfolio of the resulting engineering work, not as a claim that every line was written without AI assistance. Automated test results describe tested behavior, not a security certification.
