# Projects Host & Project Studio: start here

**Continuing this tool? Read [the detailed handover](HANDOVER.md).** For pictures and operation, use the [manual](USER-MANUAL.md) and [offline demo](../demo.html).

Shared authenticated gateway, project library, owner access controls, Studio work copies and signed version publishing. Code releases and saved-data ownership are deliberately separate.

**Start with:** Find and open a tool. Use search or category filters in the project library. Open Preview to see what a tool does; Open tool goes to the actual application.

**Source:** this is a private source checkpoint, not a live-system mirror. Check [the source manifest](../SOURCE-MANIFEST.json) and current working files before merging.

**Keep your work safe:** Source lives in Git; saved designs and service data live on the NAS; credentials stay in protected configuration. Maintain independent recovery copies of each required category. The public release allowlist must not contain private configuration, logs, credentials, SDKs or unrelated projects.

**Next maintainer action:** Inventory missing shared modules and deployment prerequisites before a portable installer. Exercise authorization, ETag conflicts, signed publishing and rollback on an isolated installation. Never bypass LAN authentication or replace live drafts with source seeds.

**Status:** private upload verified on 14 October 2026. These guides accompany the private source package. Live runtime, data backups and full recovery were not retested. Public release needs separate approval.
