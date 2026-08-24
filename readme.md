# NTERO

NTERO extracts EverQuest textures and sounds into editable files, rebuilds the
game archives, and launches the game with the edited archives through a local
overlay. It does not modify the archives in your EverQuest installation.

NTERO supports:

- DDS, BMP, and TGA textures from S3D archives, edited as PNG files
- WAV sounds from PFS archives, edited as lossless FLAC files
- Windows overlays and launching `eqgame.exe patchme`

## Install

The easiest option is the standalone Windows executable. Download `ntero.exe`
from the latest release and run it from PowerShell:

```powershell
.\ntero.exe --help
```

To run NTERO from source, install Python 3.14 or newer and
[uv](https://docs.astral.sh/uv/), then run these commands from the repository:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$HOME\.venvs\ntero"
$env:UV_LINK_MODE = "copy"
uv sync --extra dev
uv run ntero --help
```

This keeps the virtual environment outside the OneDrive-synced project. The
included VS Code workspace settings and `just` recipes set this path
automatically; set the variable as shown when using `uv` directly in another
shell.

The examples below use `ntero`. Replace it with `.\ntero.exe` for the
standalone executable or `uv run ntero` when running from source.

## Quick Start

Choose three locations:

- `--game-dir`: your EverQuest installation containing `eqgame.exe`
- `--library-root`: a separate folder where NTERO stores editable and packed files
- `--texture-pack-name`: a short name for this set of texture edits

The game installation and library must be on the same Windows drive if you want
to use `play`.

### 1. Extract

```powershell
ntero extract `
    --game-dir "C:\Games\EverQuest" `
    --library-root D:\NteroLibrary `
    --texture-pack-name my-textures
```

Editable textures are created under:

```text
D:\NteroLibrary\textures\my-textures\<archive>\textures\
```

### 2. Edit

Edit or replace the PNG files inside each `textures` folder. Keep their names,
paths, and transparency behavior unchanged. Do not edit files in `special`.

NTERO does not resize or enhance images. Use the image editor or resizing tool
of your choice before packing.

### 3. Pack

```powershell
ntero pack `
    --library-root D:\NteroLibrary `
    --texture-pack-name my-textures
```

Packed archives are written under:

```text
D:\NteroLibrary\textures\my-textures\packed\
```

By default, textures use BC3/DXT5 compression. For larger, uncompressed BGRA
textures, add `--lossless`:

```powershell
ntero pack `
    --library-root D:\NteroLibrary `
    --texture-pack-name my-textures `
    --lossless
```

Running `pack` again skips archives that have not changed.

### 4. Play

```powershell
ntero play `
    --game-dir "C:\Games\EverQuest" `
    --library-root D:\NteroLibrary `
    --texture-pack-name my-textures
```

NTERO creates `D:\NteroLibrary\overlay`, links in the selected packed archives,
and launches EverQuest in `patchme` mode. Add `--no-launch` to create the overlay
without starting the game.

The game directory, library, and overlay must be on the same drive because the
overlay uses Windows hardlinks. Files linked from the game installation are the
same on-disk files, so do not edit ordinary game files through the overlay.

## Update a Pack

After EverQuest has been patched, update an existing pack before editing newly
added files:

```powershell
ntero update `
    --game-dir "C:\Games\EverQuest" `
    --library-root D:\NteroLibrary `
    --texture-pack-name my-textures
```

`update` adds new textures without overwriting your existing PNG edits. It also
refreshes the saved source archives used by the next `pack`. The pack must
already exist; use `extract` to create a new one.

## Sound Packs

Sounds use the same workflow with `--sound-pack-name`:

```powershell
ntero extract `
    --game-dir "C:\Games\EverQuest" `
    --library-root D:\NteroLibrary `
    --sound-pack-name my-sounds

ntero pack `
    --library-root D:\NteroLibrary `
    --sound-pack-name my-sounds

ntero play `
    --game-dir "C:\Games\EverQuest" `
    --library-root D:\NteroLibrary `
    --sound-pack-name my-sounds
```

Edit FLAC files under
`D:\NteroLibrary\sounds\my-sounds\<archive>\sounds\`. NTERO converts each FLAC
back to a PCM WAV with the original sample rate, channel count, and bit depth
when rebuilding its PFS archive. Keep the sample rate and channel count
unchanged; packing rejects files whose profile no longer matches.

You can pass both `--texture-pack-name` and `--sound-pack-name` to `extract`,
`update`, `pack`, or `play` to process and use both packs together.

Sound packs created before FLAC editing was introduced must be migrated once:

```powershell
uv run python migrate_sound_packs.py `
    --library-root D:\NteroLibrary `
    --sound-pack-name my-sounds
```

Omit `--sound-pack-name` to migrate every sound pack in the library. The script
preserves the edited audio, replaces each editable WAV with FLAC, and upgrades
its manifest. Current NTERO commands accept only the upgraded sound manifests.

## Options and Troubleshooting

Run `ntero <command> --help` for all options. Useful options include:

- `--workers N` changes how many archives `extract`, `update`, or `pack`
  processes at once. Use `--workers 1` when troubleshooting.
- `--lossless` makes texture packing use uncompressed BGRA instead of BC3/DXT5.
- `--no-launch` makes `play` build the overlay without launching EverQuest.

If `pack` reports an alpha or transparency error, check that the edited PNG
still has the same kind of transparency as the extracted image. If `play`
reports a hardlink error, confirm that `--game-dir` and `--library-root` are on
the same drive.

NTERO currently supports PFS version 2 S3D archives. `sky.s3d` is intentionally
left unchanged.

## License

Copyright (C) 2026 Richard Broderick.

NTERO, including its Python and Rust source code, is free software licensed
under the GNU General Public License version 3 only (`GPL-3.0-only`). You may
use, modify, and redistribute it under the terms in [LICENSE](LICENSE).
Third-party components retain their own licenses, which are included under
[ThirdPartyLicenses](ThirdPartyLicenses/).