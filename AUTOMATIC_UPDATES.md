# Desktop updates

Starting with 2.8.2, the desktop bridge checks the public GitHub latest-release
endpoint in the background at startup. The footer also offers a manual check.
Drafts, prereleases, older/equal versions and incomplete releases are excluded.
No GitHub account, access token or Homebrew is needed. The 2.8.1 package predates
the updater and must be replaced manually once.

A release must publish `ZuckerMixer-X.Y.Z.dmg`,
`ZuckerMixer-X.Y.Z-Windows.zip`, and `SHA256SUMS.txt`. Download URLs are restricted
to this repository's GitHub release path. Files must match both the declared
size and SHA-256 checksum. Windows ZIP paths/symlinks are checked before
extraction. macOS bundles are verified with codesign. Both packages must match
the release version and pass the frozen self-check before the running app closes.
The current macOS signing is ad hoc; this does not claim Apple notarization.

The user accepts the update explicitly. Unsaved cuts, pending mix writes and
active renders/analysis block replacement. The update is staged beside the app,
and a separate copy of the updater waits for the original process to exit.
This is necessary on Windows because the running exe and DLLs are locked.
The helper renames the old app to a versioned backup, promotes the stage, launches
the new app and waits up to three minutes for its versioned startup receipt.
A crash or missing/wrong receipt restores and relaunches the previous app.
The helper writes `result.json` under the application's state `updates` directory.
Completed helper/download directories are removed on a subsequent startup;
rollback applications are retained. User recordings, settings and cuts are never
inside the replaced application folder.

Automatic replacement currently requires a writable application parent folder
and enough space for staging, the helper and rollback. Protected system folders
report an actionable error before closing; the updater never silently escalates.
Network/check failures do not block app startup. A failed download leaves the
installed app running; users can check again and retry.

Validation covers numeric versions, platform assets, incomplete/prerelease
exclusion, checksum failures, unsafe ZIP members, startup receipts, parent exit,
rollback, offline startup, active-work/dirty-cut guards and frozen helper dispatch
on macOS and Windows CI.
