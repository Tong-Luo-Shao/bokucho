# Third-Party Notices

Bokucho includes or is built with third-party software. Each component remains subject to
its own license. This notice is a summary, not a replacement for those license terms. The
exact resolved Python package versions used by the build are recorded in `uv.lock` in the
Bokucho source repository.

| Component | Purpose | License |
| --- | --- | --- |
| Python 3.11.9 | Self-contained OCR sidecar runtime | Python Software Foundation License 2.0 |
| pywebview | Native desktop window and JavaScript/Python bridge | BSD-3-Clause |
| watchdog | Project filesystem change monitoring | Apache-2.0 |
| Noto Sans TC (variable, `NotoSansTC-VF.otf`), noto-cjk commit 523d033d6cb47f4a80c58a35753646f5c3608a78 (Sans2.004) | Interface font inside the executable, used where Noto Sans TC is not installed | SIL Open Font License 1.1 |
| pylivemaker | LiveMaker extraction and injection | GPL-3.0-or-later |
| rubymarshal | RPG Maker VX Ace .rvdata2 reading and writing | WTFPL |
| Anthropic Python SDK | Claude provider client | MIT |
| OpenAI Python SDK | OpenAI-compatible provider client | Apache-2.0 |
| OpenCC | Chinese localization transforms | Apache-2.0 |
| Lingua | Source-language detection | Apache-2.0 |
| tomlkit | Comment-preserving project configuration | MIT |
| Typer | Command-line interface | MIT |
| Pydantic | Data validation | MIT |
| PaddlePaddle/PP-OCRv6_medium_det_safetensors, revision 4236c2b61741a259c091fd879dcc4edc339e916c | Pinned OCR text detector, downloaded on first scan | Apache-2.0 |
| PaddlePaddle/PP-OCRv6_medium_rec_safetensors, revision 024cad6a831de75c2c3c26e711ba8c4a82ccd24b | Pinned OCR text recognizer, downloaded on first scan | Apache-2.0 |
| LaMa (`big-lama.pt`, SHA-256 344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9) | Image text eraser, downloaded on first render | Apache-2.0 |
| PyTorch | OCR and eraser model runtime | BSD-3-Clause and bundled third-party licenses |
| Transformers | OCR model support | Apache-2.0 |
| Accelerate | OCR model loading | Apache-2.0 |
| TorchVision | PyTorch image support | BSD-3-Clause |
| OpenCV (opencv-python-headless) | OCR detector contour tracing | Apache-2.0 (OpenCV), MIT (packaging) |
| Python 3 runtime and tkinter (Tcl/Tk) | The player's patcher, frozen by PyInstaller | Python Software Foundation License 2.0; Tcl/Tk license (BSD-style) |
| Zstandard | Patch differences (CPython 3.14's `compression.zstd`, or `backports.zstd` before it) | BSD-3-Clause |
| backports.zstd | `compression.zstd` on Python before 3.14 | Python Software Foundation License 2.0 |
| PyInstaller bootloader | Starts the frozen application and patcher | GPL-2.0-or-later with the bootloader exception |
| Universal Injector Framework (UIF), commit 7ad6bddb566fbfdab60b7e45bffd4af920c2b559, built as `winmm.dll` | Loads the proxy font into a LiveMaker game that draws with a Windows font; a patch puts it beside the game | None published |
| Microsoft Detours, as vendored in UIF | Function hooking inside UIF's `winmm.dll` | MIT |
| JSON for Modern C++ 3.9.1, as vendored in UIF | Reads `uif_config.json` inside UIF's `winmm.dll` | MIT |
| NumPy | Numeric runtime | BSD-3-Clause and bundled third-party licenses |
| Pillow | Image processing | MIT-CMU |
| lxml | LiveMaker support dependency | BSD-3-Clause |
| simple-icons | Provider brand glyphs in the model settings screen | CC0-1.0 |
| GenYoGothic2 TW, GenYoMin2 TW and GenWanMin2 TW (Regular), v2.100 | Fonts shipped in `fonts/` for typesetting pictures | SIL Open Font License 1.1 |
| Hanazono Mincho A (`HanaMinA.ttf`), 2017-09-04 | Font shipped in `fonts/` for typesetting pictures | Hanazono Font License or SIL Open Font License 1.1 (dual) |
| LINE Seed TW (Regular), ver02 | Font shipped in `fonts/` for typesetting pictures | SIL Open Font License 1.1 |
| 851 Tegaki Zatsu, 0.883 | Font shipped in `fonts/` for typesetting pictures | The author's own terms: free use and redistribution, not sold on its own |
| GenEi Antique (`GenEiAntiqueNv6-M`), v6.0a | Font shipped in `fonts/` for typesetting pictures | SIL Open Font License 1.1 |
| jf open huninn, 2.1 | Font shipped in `fonts/` for typesetting pictures | SIL Open Font License 1.1; its Hanzi derive from Kosugi Maru under Apache-2.0 |

Upstream project and source information:

- pywebview: <https://github.com/r0x0r/pywebview>
- watchdog: <https://github.com/gorakhargosh/watchdog>
- Python: <https://www.python.org/downloads/release/python-3119/>
- Noto Sans TC: <https://github.com/notofonts/noto-cjk/tree/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans>
- pylivemaker: <https://github.com/pmrowla/pylivemaker>
- rubymarshal: <https://github.com/d9pouces/RubyMarshal>
- Anthropic SDK: <https://github.com/anthropics/anthropic-sdk-python>
- OpenAI SDK: <https://github.com/openai/openai-python>
- OpenCC: <https://github.com/BYVoid/OpenCC>
- Lingua: <https://github.com/pemistahl/lingua-py>
- PP-OCRv6 detector: <https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_det_safetensors/tree/4236c2b61741a259c091fd879dcc4edc339e916c>
- PP-OCRv6 recognizer: <https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec_safetensors/tree/024cad6a831de75c2c3c26e711ba8c4a82ccd24b>
- PaddleOCR: <https://github.com/PaddlePaddle/PaddleOCR>
- LaMa: <https://github.com/advimman/lama>; checkpoint:
  <https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt>
- PyTorch: <https://github.com/pytorch/pytorch>
- Transformers: <https://github.com/huggingface/transformers>
- Accelerate: <https://github.com/huggingface/accelerate>
- TorchVision: <https://github.com/pytorch/vision>
- OpenCV: <https://github.com/opencv/opencv>; <https://github.com/opencv/opencv-python>
- simple-icons: <https://github.com/simple-icons/simple-icons>
- UIF: <https://github.com/AtomCrafty/UniversalInjectorFramework/tree/7ad6bddb566fbfdab60b7e45bffd4af920c2b559>
- Detours: <https://github.com/microsoft/Detours>
- JSON for Modern C++: <https://github.com/nlohmann/json>
- GenYoGothic2: <https://github.com/ButTaiwan/genyog-font>
- GenYoMin2: <https://github.com/ButTaiwan/genyo-font>
- GenWanMin2: <https://github.com/ButTaiwan/genwan-font>
- Hanazono Mincho: <https://glyphwiki.org/hanazono/>
- LINE Seed TW: <https://seed.line.me/>
- 851 Tegaki Zatsu: <https://pm85122.onamae.jp/851fontpage.html>
- GenEi Antique: <https://okoneya.jp/font/genei-antique.html>
- jf open huninn: <https://github.com/justfont/open-huninn-font>

The complete SIL Open Font License text for Noto Sans TC, unmodified, is shipped as
`licenses/Noto-Sans-CJK-OFL.txt`. The Python Software Foundation license is shipped as
`licenses/Python-PSF-2.0.txt`. The Apache 2.0 texts for the PP-OCRv6 checkpoints and the
LaMa eraser are shipped as `licenses/PP-OCRv6-Apache-2.0.txt` and
`licenses/LaMa-Apache-2.0.txt`. The Python packages the OCR runtime installs carry their
own license files inside `ocr-runtime/`.

Bokucho builds UIF's `winmm.dll` from the commit above with one source change,
`tools/uif-font-path.patch`, which loads the fonts `uif_config.json` names from the DLL's
folder. Its build settings link the C runtime in and turn its log file off. The Detours
and JSON for Modern C++ license texts are shipped as `licenses/Detours-MIT.txt` and
`licenses/nlohmann-json-MIT.txt`.

The fonts in `fonts/` are shipped unmodified, one weight of each, some under a file name
without its version. Their license texts are shipped as `licenses/GenYo-GenWan-OFL.txt`
(GenYoGothic2 TW, GenYoMin2 TW, GenWanMin2 TW), `licenses/Hanazono-License.txt` (Hanazono
Mincho A), `licenses/LINE-Seed-TW-OFL.txt` (LINE Seed TW), `licenses/GenEi-Antique-OFL.txt`
(GenEi Antique, converted from Shift_JIS to UTF-8) and `licenses/jf-open-huninn-OFL.txt`
(jf open huninn; the Apache 2.0 text its Kosugi Maru Hanzi fall under is the one in
`licenses/LaMa-Apache-2.0.txt`). 851 Tegaki Zatsu ships with no license file; a summary of
its author's terms, with where to read them, is `licenses/851-Font-Terms.txt`.

The provider glyphs are the trademarks of their respective owners and are used only to
identify the provider each interface connects to. Bokucho is not affiliated with, endorsed
by, or sponsored by any of them.
