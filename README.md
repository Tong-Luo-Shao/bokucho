# Bokucho 墨蝶

[English](README-en.md) · [下載](../../releases) · [Windows 使用說明](PORTABLE_README.md) · [參與開發](CONTRIBUTING.md)

Bokucho 是開源的遊戲翻譯工作台。它把遊戲裡的文字和圖片整理成可校對的翻譯專案，由 AI 打草稿、由人定稿，再做成玩家執行一次就能套用、也能還原的補丁。

支援 **LiveMaker / LiveNovel**、**Ren'Py**、**RPG Maker MV / MZ / XP / VX / VX Ace**。目前仍是 pre-alpha，提供 Windows 桌面版與 Python CLI。

> [!IMPORTANT]
> Bokucho 只發行翻譯資料與套用工具，不包含、不提供也不託管任何遊戲的執行檔、破解補丁或其他未經授權的遊戲資料。原廠遊戲檔案由翻譯者與玩家以各自的合法副本提供。

## 功能特色

- **完整桌面流程**：建立專案、擷取、翻譯、校對、品質檢查與發行都在同一個程式裡。
- **支援各大 AI 供應商**：內建 Claude、Gemini、OpenRouter、OpenAI、DeepSeek、OpenAI 相容 API，包括 Ollama、LM Studio 等本機服務。
- **人工成果不會被蓋掉**：機器草稿和校對過的譯文分開管理，翻譯只處理未翻譯的內容。
- **譯名一致**：專案與全域詞彙表、提示詞庫、上下文、依 token 分批、多把 API key 輪替與速率限制。
- **來源語言篩選**：用 BCP 47 語言標籤逐行篩選後才送出；混合語言內容的其他行與原始位置保持不變。
- **品質檢查**：檢查變數、控制碼、格式、行數、原文殘留、目標語言與詞彙表使用情況。
- **圖片翻譯**：用 PP-OCRv6 偵測與辨識文字、LaMa 擦除原字，再自動排版，也能手動編輯。
- **補丁不含遊戲原檔**：依引擎採用套用時編譯、掛載外掛或原生翻譯檔。
- **GUI 與 CLI 共用流程**：可以互動操作，也能放進腳本與 CI。

## 支援引擎

| 引擎 | 擷取與回寫 | 補丁帶翻譯圖片 | 玩家套用時 |
|---|---|---|---|
| LiveMaker / LiveNovel | 打包或解包的遊戲、劇本、選項、變數顯示文字 | 是 | 對玩家的原版注入譯文、建立代理字型並重新封裝，保留備份 |
| RPG Maker MV / MZ | JSON 資料、事件文字、選項、plugin note tags | 是 | 安裝並啟用 `Bokucho.js`，原始 JSON 不變 |
| RPG Maker XP / VX / VX Ace | `.rxdata`、`.rvdata`、`.rvdata2` 資料，以及 `.rgssad`、`.rgss2a`、`.rgss3a` 封包 | 是 | 封包版重建封包，解包版替換 `Data/` 檔案，保留備份。VX 與 VX Ace 遊戲若自帶字型，會把譯文缺的字補進該字型的副本 |
| Ren'Py | 由官方 SDK 產生的 `game/tl/` 翻譯檔 | 否 | 安裝 `game/tl/<語言>/` 並在啟動時切換到該語言，不改動原檔 |

每個引擎的成品都是同一種補丁，檔名為 `<專案>-<來源語言>2<目標語言>-patcher.exe`，例如 `MyGame-ja2zh-tw-patcher.exe`。玩家把它放進遊戲資料夾執行即可，用同一個 EXE 的「還原」可以復原。

KiriKiri / KAG、Unity、Godot 尚不支援翻譯。

## 系統需求

- **Windows x64**：使用預先建置的桌面版。
- **遊戲的合法副本**：建立專案與測試補丁時使用。
- **AI 介面**：雲端服務需要自己的 API key；本機 OpenAI 相容服務通常不需要。
- **Ren'Py SDK**：只有 Ren'Py 專案需要，可免費取得 [官方 SDK](https://www.renpy.org/latest.html)。桌面版還沒有這個欄位，請在 `project.toml` 的 `[engine]` 填 `sdk_path`。
- **NVIDIA 顯示卡**：只有圖片 OCR 與自動擦除需要。第一次使用會先顯示下載大小並詢問。

## 快速開始

### 1. 啟動

從 [Releases](../../releases) 下載 `Bokucho-Windows-x64.zip`，解壓縮到可寫入的資料夾，執行 `Bokucho.exe`，並讓整個資料夾保持在一起。
可以在「設定」的「介面語言」改成英文。

### 2. 建立翻譯專案

1. 按「建立專案」。
2. 填寫「專案位置」與「專案名稱」。
3. 在「遊戲路徑」按「瀏覽」，選擇遊戲資料夾或執行檔。
4. 確認偵測到的遊戲引擎。
5. 設定來源與目標語言。預設是日文（`ja`）到繁體中文（`zh-tw`）。
6. 再按一次「建立專案」。

### 3. 設定翻譯模型

1. 打開「模型」。
2. 從「供應商」選擇服務，按「新增介面」。
3. 填入「API Key」，選擇模型，再按「儲存」。
4. 按「測試介面」確認連線，再按「使用介面」。

介面由所有專案共用。同一個供應商可以存多把 API key，也可以為不同模型各建一個介面。

> [!WARNING]
> 金鑰存在 `Resource/credentials.json`，內容只有 Base64 編碼，並未加密。不要分享這個檔案。

### 4. 擷取與翻譯

1. 打開「字串」，按「擷取文字」。再次擷取會把新內容合併進既有翻譯。
2. 翻譯前可以先到「詞彙表」按「從劇本讀取專有名詞」，統一譯名。
3. 回到「字串」，按「翻譯」，在「翻譯儀表板」按「開始」。

翻譯可以暫停和繼續，完成的批次會立即存檔。如果遊戲混有多種語言，到「設定」選擇來源語言；`und` 代表不限制。

### 5. 校對

在「字串」選擇一筆內容，修改譯文後按「標記已校對」，再按「下一個」。狀態流程是：

```text
未翻譯 -> 機翻 -> 已校對
```

翻譯只處理未翻譯的內容，已校對的不會被重寫。需要重翻時，從「標記已校對」旁的選單選「標記未翻譯」。不需要翻譯的內容（例如程式用的字串）選「標記忽略」，遊戲中會保留原文。

### 6. 翻譯圖片（選用）

1. 打開「圖片」，按「掃描並辨識文字」。
2. 辨識結果會以圖片文字單元加入「字串」，沿用相同的翻譯與校對流程。
3. 回到「圖片」，檢查文字框與字型，按「擦除並排版」。

模型下載、GPU 需求、儲存位置與內建字型請見 [Windows 使用說明](PORTABLE_README.md)。

### 7. 檢查與發行

1. 打開「品質檢查」查看缺陷與警告，按「前往」可以跳到對應文字。
2. 打開「發行」，確認引擎、文字單元與未翻譯數量。LiveMaker 專案要先選擇遊戲文字字型。
3. 按「下一步」，再按「開始構建」。
4. 補丁在 `dist/<timestamp>/<專案>-<來源>2<目標>-patcher.exe`。

未翻譯的內容會以原文套用。正式發布前，請在遊戲副本上套用、啟動、測試並還原一次。

## 從原始碼執行

需要 Python 3.12 以上與 [uv](https://docs.astral.sh/uv/)：

```powershell
git clone https://github.com/Tong-Luo-Shao/bokucho.git
cd bokucho
uv sync --locked --extra dev --extra web
uv run --extra web bokucho-gui
```

圖片 OCR 與自動排版需要額外安裝大型依賴：

```powershell
uv sync --locked --extra dev --extra web --extra images
```

## CLI

安裝依賴後，可以用 CLI 跑完整流程：

```powershell
.\.venv\Scripts\Activate.ps1

bokucho init D:\Translations\my-game --game "D:\Games\MyGame"
cd D:\Translations\my-game

$env:ANTHROPIC_API_KEY = "your-key"
bokucho extract
bokucho build-glossary
bokucho translate --limit 20
bokucho translate
bokucho review
bokucho qa
bokucho build-patch
```

新專案預設使用 Claude。Gemini 使用 `GEMINI_API_KEY`，其他 OpenAI 相容介面通常使用 `OPENAI_API_KEY`。也可以先在桌面版的「模型」建立介面，CLI 會讀取同一份專案設定。

執行 `bokucho --help` 查看全部命令，`bokucho <command> --help` 查看參數。主要命令：

- 專案與文字：`init`、`detect`、`extract`、`inject`
- 翻譯：`build-glossary`、`glossary`、`translate`、`import`、`transform`、`review`
- 驗證：`qa`、`verify`
- 圖片：`images extract --ocr`、`images render`
- 發行：`patch-runtime`、`patch-font`、`build-patch`

## 專案資料

每個翻譯專案都是普通檔案，可以用 Git 管理：

```text
project.toml              專案、引擎、模型與轉換設定
strings/*.json            原文、譯文、狀態、歷史與引擎位置
glossary.json             專案詞彙表
images-inventory.json     圖片掃描結果（使用圖片流程時）
out/                      本機 overlay 與圖片草稿
dist/                     發行成品
```

## 文件

- [Windows 桌面版、OCR 下載與 portable 資料](PORTABLE_README.md)
- [LiveMaker 字型與 CP932 代理字](FONT_PROXY.md)
- [參與開發](CONTRIBUTING.md)
- [第三方授權](THIRD_PARTY_NOTICES.md)

## 參與開發

Bokucho 以 `EngineAdapter` 隔離遊戲引擎，以 `Translator` 隔離 LLM 供應商，兩邊透過中立的 `TextUnit` 格式合作。開發環境、測試命令與擴充方式請見 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 授權

Copyright (C) 2026 Bokucho contributors

Bokucho 使用 [GNU GPL-3.0-or-later](LICENSE)。第三方程式、模型與字型的授權列在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
