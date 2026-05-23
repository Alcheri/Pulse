# Changelog

All notable changes to Pulse are documented in this file.

## Unreleased

### Added

- Moved supporting project documentation into the `docs/` directory.

### Changed

- Normalised README badges and Black configuration.
- Updated contribution terms.

### Fixed

- Hardened feed fetching and redirect handling.
- Hardened feed state writes and flushes.
- Recovered duplicate feed state keys.
- Avoided empty feed network entries.
- Normalised Pulse network keys.
- Honoured authenticated owner users for owner-only diagnostics.
- Sent Pulse state diagnostics as owner notices.

## v0.2.0 - 2026-04-30

### Added

- Added CodeQL analysis workflow configuration.
- Added build status badges to the README.
- Documented Pulse configuration variables.

### Changed

- Refactored Pulse into separate feed, storage, rendering, and orchestration modules.
- Made feed state network-aware.
- Reformatted Python code with Black.

## v0.1.0 - 2026-04-28

### Added

- Initial Pulse plugin import.
