# Projects Host & Project Studio — visual user manual

[Project overview](../README.md) · [Offline interactive demo](../demo.html)

Start with the numbered picture guide. Each number identifies the exact highlighted control. Work through one step at a time; the longer reference is below it.

### Try the working offline example

Screenshot of this project’s interactive demo. Side-by-side services are explicitly simulated.

![Projects Host & Project Studio — numbered controls](images/guide-1.jpg)

1. **Create a separate work copy.** Prepare a change without replacing the saved live design.
2. **Review before publishing.** The simulated publish refuses an unreviewed change.
3. **Check what was preserved.** The dummy release changes while the example saved plan remains intact.

## Detailed task reference

### Find and open a tool

1. Use search or category filters in the project library. Open Preview to see what a tool does; Open tool goes to the actual application.
2. Only projects granted to the signed-in account are shown. An owner-only tool is not shared by simply copying its URL.
3. Use the project’s own save status and export options; the library overview itself does not save its designs.

### Review a change in Project Studio

1. Choose the intended project and create a work copy. Describe the change with a concrete example of the desired behaviour.
2. Inspect the changed files and test the affected workflow in that work copy. Keep unrelated tools and live saved data outside the change.
3. Publish only after review/checks finish, then verify the actual hosted version and essential actions. A successful local edit is not a live deployment.

### Grant access deliberately

1. Open Access Manager as the owner, enter the intended person’s email and choose only the requested projects.
2. Save the grants and share the normal sign-in link. Confirm access as appropriate without weakening the server checks.
3. Revoke unwanted grants through the same owner workflow. LAN access is not an exception to authentication and authorisation.

### Handle an update or restore

1. Before editing a shared saved design, read its live API version/ETag and preserve newer changes. Do not restore a source seed over a live draft.
2. For application updates, use the signed release workflow and inspect verification output. Keep deployment keys out of Git.
3. Use version restore as an explicit owner operation after identifying the target version. Back up live data separately; a Git commit is not a copy of the database or uploads.

## Keep your work

Source lives in Git; saved designs and service data live on the NAS; credentials stay in protected configuration. Maintain independent recovery copies of each required category. The public release allowlist must not contain private configuration, logs, credentials, SDKs or unrelated projects.

## If something looks wrong

- Tool missing: check account grants before assuming a broken link.
- Cloudflare tunnel error: check the tunnel/network/service, not only browser cache.
- Save conflict: reload the current version and reconcile instead of overwriting.
- Publish failed: read validation/startup output and keep the known-good release until resolved.

## What this manual does not promise

This is installation-specific infrastructure and remains private. A portable public edition needs a configuration/authentication guide and clean deployment testing. The demo changes only mock permissions, work copies and release state.

Screenshots show the actual application where captured. The offline demo is a deliberately smaller simulation and identifies its dummy services. It never controls devices, sends notifications, uploads files or launches printing.
