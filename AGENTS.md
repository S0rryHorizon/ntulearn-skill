# AGENTS.md

## Project purpose

`ntulearn-skill` is a local-first NTULearn synchronization, indexing, and retrieval project. This repository is intended to contain public code and documentation, never a user's private learning data.

## Privacy boundary

Never commit credentials, passwords, cookies, tokens, session or CSRF data, personal identifiers, real course data, real NTULearn materials, private indexes/databases, authenticated logs, or raw authenticated request dumps.

Real data may exist only in the ignored `.local/` workspace or, preferably, outside the repository under `~/.ntulearn-skill/`. Use only synthetic data in tests and examples. Before every commit, inspect staged files and verify ignore rules.

## NTULearn safety

All future NTULearn access is read-only by default. Do not submit assignments, take quizzes, modify course content, post discussions, send messages, change profiles, perform destructive actions, bypass permissions, or enumerate resources the user is not authorized to access.

## Evidence rule

Reconnaissance findings must be labeled as `CONFIRMED`, `OBSERVED`, `HYPOTHESIS`, or `UNKNOWN`. Never present an inference as verified NTULearn behavior. Raw reconnaissance belongs only in `.local/recon/`; only reviewed and de-identified conclusions may enter `docs/development/`.

## Architecture

Keep the core independent from Codex, ChatGPT, and other LLM products. Favor local-first operation, incremental synchronization, source provenance, and privacy by default. AI integrations must remain thin adapters over stable core APIs and CLI behavior.

## Development navigation

- Architecture: `docs/architecture/overview.md`
- Data boundaries: `docs/privacy/data-boundaries.md`
- Project phases: `docs/development/phases.md`
- Durable decisions: `docs/decisions/`

Do not place extensive design material in this file; keep it as a concise rule and navigation entry point.
