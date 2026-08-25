# Steam Log Scrubber

Steam and Proton log scrubber for safer support sharing on Linux and Windows.

## Windows executable

1. Download the repository source and extract the entire folder.
2. Double-click `build-windows.cmd`.
3. Wait for the `Build complete` message.

The finished standalone app is `dist\SteamLogScrubber.exe`. The builder uses an
installed Python 3.10 or newer when available. Otherwise, it asks Windows Package
Manager to install Python 3.13 for the current user, creates an isolated build
environment, and builds the single-file GUI with PyInstaller.

The executable does not require Python on the computer where it is run. Because it
is locally built and unsigned, Windows may show a reputation warning the first time
it is opened.

## Log collection

Leave the GUI input folder blank to use automatic detection.

- On Windows, automatic detection collects the Steam client's `logs` folder and
  detectable text logs from games opted in with the Windows launch option below.
  Steam is found through its Windows registry entry or default Program Files
  location. The output archive is a `.zip` file.
- On Linux, automatic detection collects the Steam client logs and every detected
  Proton log named `steam-<APPID>.log` from the home directory and `PROTON_LOG_DIR`.
- To scrub any other log folder on either platform, choose it with the GUI's
  `Browse...` button.

### Windows game-log launch option

Add this launch option to a native Windows game when its detectable logs should be
included in automatic Steam Log Scrubber collections:

```text
--steamlogscrubber
```

Steam Log Scrubber reads the game's saved Steam launch options, resolves its AppID
through Steam's manifests and library folders, and includes text logs found in that
game's installation directory. The marker does not enable Proton and does not create
a log itself. Games can also store logs elsewhere in AppData, Documents, or another
game-specific location; use the GUI's input-folder picker for those locations.
Steam also passes user launch options to the game; if a particular game rejects an
unknown option, remove the marker and use the input-folder picker instead.

There is no game or AppID allowlist. Any installed game with the exact marker is
considered for collection.

Proton logging applies only when a game is being run through Proton on Linux. Add
this Steam launch option to the game:

```text
PROTON_LOG=1 %command%
```

Valve documents that this creates `$PROTON_LOG_DIR/steam-$APPID.log`, defaulting to
the user's home directory. Steam Log Scrubber does not contain a game or AppID
allowlist: every matching Proton log is collected, regardless of which game created
it.

## Command line

```text
steamlogscrub [INPUT_FOLDER]
```

With no input folder, the same platform-specific automatic detection is used. Run
`steamlogscrub --help` for ruleset, archive, and dry-run options.

## Build references

- [Valve Proton runtime configuration](https://github.com/ValveSoftware/Proton#runtime-config-options)
- [Microsoft WinGet install command](https://learn.microsoft.com/windows/package-manager/winget/install)
- [PyInstaller one-file bundles](https://pyinstaller.org/en/stable/operating-mode.html#bundling-to-one-file)
