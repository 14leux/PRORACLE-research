# Contributing to PRORACLE

We welcome careful experiments, bug reports, data-quality findings, documentation, and code. A small, reproducible result is more useful than a large claim without a clear method.

## Start with a discussion or issue

Use GitHub Discussions for questions and early ideas when that feature is enabled. Otherwise, open an issue. For a concrete experiment, state the problem and hypothesis so others can review the proposal before substantial work begins. Useful issue labels include `research`, `data-quality`, `evaluation`, `prediction`, `self-hosting`, and `good first issue`.

An experiment proposal should identify:

1. The question and a testable hypothesis.
2. The baseline and the proposed change.
3. The data source, date range, and permission to use or share it.
4. The evaluation split, sample size, metric, and expected uncertainty.
5. The result that would count as improvement and the regression checks to retain.

## Share findings safely

Submit written findings through the field-note or experiment issue template. Include enough detail for another person to reproduce the method, but do not attach raw provider data, private production reports, API responses, account identifiers, or credentials. Use synthetic examples when possible. Check the [data policy](docs/DATA_POLICY.md) before sharing derived data or outputs. Report null and negative results with the same care as positive findings.

## Keep project knowledge public and concise

GitHub issues and pull requests are the working record for public project questions, evidence, and routine decisions. Put the relevant rationale in the issue or pull request so future contributors do not need private chat history to understand the change.

For a durable choice that changes architecture, data boundaries, licensing, or the evaluation contract, maintainers may add a short decision record under `docs/decisions/` and link it to the discussion or pull request. State the context, options considered, chosen approach, trade-offs, and what evidence would prompt a review. Do not submit private operational histories, meeting transcripts, or internal deliberations. Contributors do not need a session log, handoff record, or other private project-management ceremony.

Contributors may share knowledge under a name, handle, or without public attribution, as described in [CONTRIBUTORS.md](CONTRIBUTORS.md). Include only information you have the right to share.

## Pull requests

- Keep each change focused and explain the user-visible or research reason.
- Include tests for code changes and the exact command used to run them.
- For model or prompt changes, compare against a fixed baseline and an untouched evaluation period. Describe leakage checks, sample size, uncertainty, and any regressions.
- For prediction-quality claims, distinguish accuracy from probability calibration and financial return. State the evaluation cohort and limitations; synthetic results demonstrate the tooling only.
- Do not add odds as prediction inputs, automate prompt changes, or make claims of betting profit without a separately reviewed research design and evidence.
- Do not include secrets, real user data, raw provider responses, or production identifiers.
- Do not add a dependency without describing why it is needed and checking its license.
- Save a new model or prompt as a new versioned file. Never overwrite a released prompt, and never let a tool or a model change one automatically.
- Expect the CI checks to pass before review: offline tests on two Python versions, a compile check, the synthetic demo, and a secret scan. They run with no secrets, and pull requests from forks never receive any.
- Changes to workflows, migrations, the prediction contract, the evaluator, prompts, or any accuracy or return claim get closer maintainer review; see [the maintainer guide](docs/MAINTAINING.md).

Contributions are submitted under Apache-2.0 for code and CC BY 4.0 for original documentation and reports, unless a file says otherwise. Contributors keep copyright in their contributions and grant the project those licenses by submitting a pull request; there is no CLA requirement at this stage. Only submit work you created or have permission to contribute under these terms.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) and [Security Policy](SECURITY.md).
