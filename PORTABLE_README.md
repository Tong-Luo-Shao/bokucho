# Bokucho for Windows

## Start

1. Unzip `Bokucho-Windows-x64.zip` into a folder you can write to, such as
   `Documents`.
2. Open `Bokucho.exe` in that folder. The first start takes a little longer.
3. Click **建立專案** (Create project) for a new translation, or **開啟資料夾**
   (Open folder) for a folder that already holds a `project.toml`.
4. The app starts in Traditional Chinese. For English, click the gear icon in
   the left bar and set **介面語言** (Interface language) to **English**.

Then work down the left bar: **◇ Model** for your AI and its key, **≡ Strings**
to extract, translate and check the lines, **! Quality** for problems, and
**↑ Release** to build the translated game.

For every engine, **Release** produces one `<project>-<source>2<target>-patcher.exe`,
for example `MyGame-ja2zh-tw-patcher.exe`. Players put it in their game folder
and run it; **Restore** in the same executable undoes it. Players need only this
executable, and the release holds translation data and tools.

For LiveMaker, the patch carries translation data, the chosen font sources and
the compiler: it checks the original, compiles the translated scripts and font,
rebuilds the archive, and keeps `<game>.bak` for **Restore**. If compilation
fails, the original stays in place.

Choose the game text font on the release screen. For a game using a Windows
font, the patch also carries Bokucho's UIF `winmm.dll` (or your selected build).
At application time it builds the proxy font and puts it beside the game with
UIF. These added files are tracked and removed by **Restore** when unchanged.

For RPG Maker MV/MZ, the patch installs `js/plugins/Bokucho.js` and registers
it in `plugins.js`. It mounts text as data loads, and original JSON files stay in
place. Translated pictures load from a separate `bokucho/` folder, including
encrypted images.

For RPG Maker XP, VX and VX Ace, which have no plugin loader, the patch injects the
translation into the player's own data the way LiveMaker's does: a packed game's
`.rgssad`, `.rgss2a` or `.rgss3a` archive is rebuilt with the translated data
(everything else is copied across untouched), an unpacked game's changed `Data/`
files are replaced, and each original is kept as `<file>.bak`.

For Ren'Py, the patch adds the native `game/tl/<language>/` translation and a
small script that starts the game in that language. Original files stay as they are;
**Restore** removes what was added, along with what Ren'Py compiled from it. Making
the translation needs the Ren'Py SDK: set `sdk_path` under `[engine]` in
`project.toml`. This patch does not carry translated pictures.

Try patches on a copy of your game folder first.

## The app folder

`Bokucho.exe` works with the folders beside it, so move the whole folder
together:

| Folder | What is in it |
|---|---|
| `Resource/` | Your API keys, in `credentials.json` |
| `ocr-runtime/` | The runtime for reading pictures, filled on first use |
| `models/` | The models for reading pictures, filled on first use |
| `fonts/` | The fonts pictures can be typeset in, which come with the app |

`Resource/credentials.json` keeps your API keys in readable form (Base64).
Keep it private, and delete it before you give the folder to anyone.

Window size, theme, accent, row height, interface language and recent projects
are saved in your Windows user profile. Each project stays in the folder you
chose for it.

## OCR: words inside pictures

This needs an NVIDIA graphics card.

- **Scan and read text** on **▣ Images** uses PP-OCRv6: one model finds the
  words on each picture, and a second one reads them.
- **Erase and typeset** uses LaMa to erase the old words, then writes the
  translation in their place.

A tagged release carries a small bootstrap in `ocr-runtime/`.
The first OCR scan downloads the CUDA runtime and the models, and the first
erase downloads the eraser, each with download progress in the window. The app
shows the size and asks before each download:

| Download | Size | Saved in |
|---|---|---|
| CUDA runtime (PyTorch for CUDA 13.0) | about 2 GB | `ocr-runtime/` |
| PP-OCRv6 detector and recognizer models | about 165 MB | `models/` |
| LaMa eraser | about 206 MB | `%USERPROFILE%\.cache\bokucho\` |

After that, reading and erasing work offline. A scan you stopped carries on
from where it stopped: **Images** asks when you scan again. **Settings → OCR
model** shows what is downloaded, and holds the confidence thresholds and the
batch size.

The typeset translation can be set in one of the fonts in `fonts/`, which come
with the release: GenYoGothic2 TW, GenYoMin2 TW, GenWanMin2 TW, Hanazono
Mincho A, LINE Seed TW, 851 Tegaki Zatsu, GenEi Antique and jf open huninn,
one weight each, all of them free to use and share. The font list on
**▣ Images**, and the font list on an image's text layer, offer them ahead of the
fonts Windows has, each only while it draws every character the translations
use. A project names one of them by its file, so it keeps it when the app folder
moves.

## Build the release yourself

From the Bokucho source folder:

```powershell
uv run --locked --extra build python tools/build.py
```

`--app-only` builds the app alone, without the OCR bootstrap or `fonts/`. To
fill `fonts/` in the source folder, so the app run from source offers them:

```powershell
uv run python tools/fetch_typeset_fonts.py
```

To put both models and the eraser into the built release's `models/` folder
ahead of time:

```powershell
uv run --extra images python tools/fetch_ocr_model.py
```

## Licenses

Bokucho is GPLv3; see `LICENSE`. Third-party parts are listed in
`THIRD_PARTY_NOTICES.md`, and their license texts are in `licenses/`.
