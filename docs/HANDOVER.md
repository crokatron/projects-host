# Projects Host & Project Studio: maintainer handover

[One-screen summary](START-HERE.md) · [User manual](USER-MANUAL.md) · [Feature overview](../README.md) · [Recorded validation](../VALIDATION.md)

## Purpose and behaviour to preserve

The shared NAS platform that serves the tools, checks access, preserves saved drafts and publishes reviewed releases. Project Studio provides a workflow for creating a work copy, requesting a change and reviewing it before publishing. Access Manager controls which projects a person can open; source backup, publishing and saved-data backup are separate operations.

- **Project library:** Search/filter tools, read previews and open dedicated project pages. (NAS platform.)
- **Access Manager:** Owner-managed per-project grants behind authenticated sign-in. (NAS platform.)
- **Project Studio:** Create a separate work copy, request changes and review before publishing. (NAS platform.)
- **Saved drafts:** Persist shared app designs on the NAS independently of the Windows PC. (NAS platform.)
- **Release workflow:** Curate files, validate, sign, deploy and verify a release with rollback on failed startup. (NAS platform.)
- **History and restores:** Owner-only version history and deliberate restoration workflow. (NAS platform.)
- **App services:** Gateway routes to HA, CAD, plant, QR backup and other protected services. (NAS platform.)

The [demo](../demo.html) is a smaller, labelled simulation with fictional data. It explains cause and effect without the real service; it is not evidence of a working production installation. Numbered screenshots in the user manual distinguish actual-app captures from demo captures.

## Files and sources of truth

| Actual package path | Maintainer role |
| --- | --- |
| [projects-host/host](../projects-host/host) | Gateway, authorization, service adapters and supervisor |
| [projects-host/site](../projects-host/site) | Index, Studio, user access and history interfaces |
| [projects-host/build_release.py](../projects-host/build_release.py) | Explicit release allowlist/assembler |
| [projects-host/validate_release.py](../projects-host/validate_release.py) | Release verification |
| [projects-host/Publish-Projects.ps1](../projects-host/Publish-Projects.ps1) | Installation-specific publishing entry point |
| [demo-source](../demo-source) | Editable demo HTML/CSS/runtime and embedded screenshot tour |
| [SOURCE-MANIFEST.json](../SOURCE-MANIFEST.json) | Original copied-source paths and SHA-256 checkpoint values |

The original working source is under C:/Users/lamon/Desktop/AI TOOLS/NAS for NAS modules; adjacent CAD/watch source locations are recorded by the full backup’s reviewed source manifest. This package contains only the explicitly copied source, not an automatic mirror. The main private nas-projects backup retains the broader shared host context.

## Setup, run and tests

Run commands from this repository root unless a `cd` is shown. Use a disposable checkout and fictional data for first setup. Dependencies, SDKs, signing material and personal data are not bundled. The following are continuation instructions grounded in the retained source, **not commands executed or newly certified by this handover**.

This module package is not a complete standalone NAS installation. Use the current working NAS/projects-host/README.md and full source backup to plan a clean test deployment. The copied launcher refers to other files/runtime paths not all included here.

Selected project check:

No independent automated production test suite is included for this module/single-file package. Use the project-specific checks below and the shared host test suite where applicable; do not invent an `npm test` command.

For the documentation demo alone, serve this repository on loopback with `python -m http.server 8765 --bind 127.0.0.1` and open `/demo.html`. Choose a free port if another preview is running. JavaScript syntax can be checked with `node --check demo-source/runtime.js`. `python demo-source/rebuild.py` rewrites demo.html, so run it only in a disposable checkout or an authorised demo-editing session. A local server cannot provide missing HA/CAD/NAS functions. Inspect browser tests for installed dependencies, fixture requirements and network targets before running them with the approved browser tooling.

## Data, configuration and backup boundaries

PROJECT_DATA defaults to /srv/data, with private configuration kept outside published releases. Studio workspaces and private metadata are separate from installed code. The signing key lives in protected local .codex-private storage; recover it separately and never embed it in a release.

Source lives in Git; saved designs and service data live on the NAS; credentials stay in protected configuration. Maintain independent recovery copies of each required category. The public release allowlist must not contain private configuration, logs, credentials, SDKs or unrelated projects.

Git preserves reviewed source and documentation. It does not copy browser storage, application databases, uploaded models/photos, accepted device settings, SDKs or credentials. Maintain independent recovery copies of the data identified above, including matching configuration and asset versions. Never add real secrets to examples, issues or logs. Private source copies on GitHub are not proof of a complete NAS/offsite data backup.

## Current verification and Git state

Before these documentation edits on 14 October 2026, this package was clean on branch `main` at `f7a535994f55ec6481f4536654bea4f60c897b7f` and its private upload was verified. All 28 manifest-listed source entries matched their stored checksums before editing. The upload follow-up has completed all 17 repositories and is paused; it does not run this tool or back up its live data.

These handover pages and README links accompany the source package. Preserve them when continuing; stage only reviewed changes. Recheck current `git status --short --branch` and `git diff` before starting. No project-specific background schedule was inspected or changed in this documentation pass. Any service schedules in older notes are historical claims to verify in the actual deployment.

Earlier package checks and limits are recorded in [VALIDATION.md](../VALIDATION.md). This handover adds documentation/link/whitespace/secret and source-preservation checks only. No current NAS release, real hardware state, fresh installation, complete data restoration or offsite-backup freshness was verified here.

## Recovery approach

1. Preserve the current installation/data before changing it. Identify the exact code commit and data snapshot required; do not assume this package matches the running service.
2. Recover source into a new folder. Verify the source manifest and restore the project-specific configuration/assets described above separately. Recreate dependencies from the retained project files and guides.
3. Restore user data into an isolated instance or clean test browser. Check a representative editable record, associated files and save/reload behaviour, then compare to the original. Do not test recovery by overwriting production.
4. Record what actually restored and what remains missing. A demo opening or a passing syntax check is not a production recovery drill. Real deployment/device actions need the existing authorised workflow.

## Known limits and next work

This is installation-specific infrastructure and remains private. A portable public edition needs a configuration/authentication guide and clean deployment testing. The demo changes only mock permissions, work copies and release state.

**Priority:** Inventory missing shared modules and deployment prerequisites before a portable installer. Exercise authorization, ETag conflicts, signed publishing and rollback on an isolated installation. Never bypass LAN authentication or replace live drafts with source seeds.

Common symptoms to check for this tool:

- Tool missing: check account grants before assuming a broken link.
- Cloudflare tunnel error: check the tunnel/network/service, not only browser cache.
- Save conflict: reload the current version and reconcile instead of overwriting.
- Publish failed: read validation/startup output and keep the known-good release until resolved.

Keep [the release checklist](../RELEASE-CHECKLIST.md) and [third-party notes](../THIRD-PARTY.md) with the package. Original contributions use MIT; bundled/upstream code and assets keep their own licences. Public publication is a separate decision; preparing documentation is not permission to publish.
