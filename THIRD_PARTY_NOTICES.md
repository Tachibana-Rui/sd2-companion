# Third-party data notices

## sd2-data names

`deck-analyzer/data/catalog.json` retains the upstream provenance for unit and division names:

- Upstream project: <https://github.com/sd2-nerd-hq/sd2-data>
- Pinned commit: `d53eccbf261e05925167aa8c5b2d44ee356c0690`
- Author declared by upstream `package.json`: `sd-sodbot-hq`
- License declared by upstream `package.json`: `ISC`
- Original declaration: <https://github.com/sd2-nerd-hq/sd2-data/blob/d53eccbf261e05925167aa8c5b2d44ee356c0690/package.json>

The pinned upstream tree does not contain a separate LICENSE, COPYING or copyright-notice file. This notice preserves the actual attribution and license declaration; it does not invent an upstream copyright year or a different permission grant. Existing `names_source`, `names_commit` and `names_license` fields in the catalog are preserved unchanged.

## Steel Division 2 data

The JSON resources contain structured map identifiers and unit/production metadata derived from Steel Division 2 data. Their source and verification metadata are retained in the files. Steel Division 2 and its original game content belong to Eugen Systems and their respective rights holders. This repository is an independent companion project and is not an official Eugen Systems product.

Game executables, original archives, original images, personal recordings and reports are not included in this source snapshot. References to artwork paths in metadata do not mean the artwork is bundled here.

## Build tooling

PyInstaller is an external build dependency installed from `player-stats/requirements-build.txt`; its source is not vendored here. Its licensing and bootloader exception are available at <https://pyinstaller.org/en/stable/license.html>.
