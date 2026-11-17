<div align="center">

# NTU COOL Get Class Material

**一行指令，把 NTU COOL 的整門課搬回你的電腦**

Download NTU COOL course files, announcements, pages, and videos into organized local folders.

[![PyPI](https://img.shields.io/pypi/v/get-class-material?color=blue)](https://pypi.org/project/get-class-material/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey)](#快速開始)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

📄 講義附件 · 📢 公告 · 📝 課程頁面 · 🎬 上課影片 · 🤖 NotebookLM 匯入（選用）

</div>

---

## 目錄

- [快速開始](#快速開始)
- [下載結果](#下載結果)
- [常用指令](#常用指令)
- [疑難排解](#疑難排解)
- [授權、隱私與使用規範](#授權隱私與使用規範)

## 快速開始

> **事前準備**：先安裝 [Python 3.11 以上](https://www.python.org/downloads/)。Windows 安裝時若有 **Add Python to PATH** 選項，請勾選，裝完再開新的 PowerShell。macOS 可使用 [python.org 的安裝程式](https://www.python.org/downloads/macos/)。

**Windows（PowerShell）**

```powershell
python -m pip install get-class-material
get-class-material
```

**macOS（終端機）**

```bash
python3 -m pip install get-class-material
get-class-material
```

## 下載結果

預設存到「文件（Documents）」下的 `ntu-cool-gcm_material`。每門課會建立「課程名稱（課程 ID）」資料夾，單元名稱有週次時會依週次整理。

```text
ntu-cool-gcm_material/
└── 課程名稱 (57544)/
    ├── course_overview.md        ← 課程索引，從這裡開始
    ├── announcements/
    │   └── announcements.md
    ├── week1/
    │   ├── 講義.pdf
    │   ├── 課程頁面.md
    │   ├── 上課錄影.mp4
    │   └── attachments/
    └── week2/ …
```

| 內容 | 存檔方式 |
| --- | --- |
| 講義與附件 | 保留原始檔案格式 |
| 課程頁面與公告 | 頁面存成 Markdown，公告集中在 `announcements/announcements.md` |
| YouTube 與 NTU COOL 上課錄影 | 下載成功的影片存為 `.mp4` |
| 課程索引 | 從 `course_overview.md` 查看單元和檔案位置 |

> **提示**：可隨時重跑。補抓教材或下載中斷時，重新執行 `get-class-material` 並選同一門課。程式會重用已完成的檔案，並在伺服器支援時續傳中斷的檔案。

## 常用指令

| 要做什麼 | 指令 |
| --- | --- |
| 只下載附件、頁面與公告 | `get-class-material --skip-youtube --skip-cool-videos` |
| 指定下載位置 | `get-class-material --out "我的課程"` |
| 依課程 ID 下載（網址中 `courses/` 後的數字） | `ntu-cool-materials download-course --course-id 57544` |
| 重新登入 NTU COOL | `get-class-material --refresh-session` |
| 檢查環境 | `ntu-cool-materials doctor` |
| 查看選項與版本 | `get-class-material --help`、`get-class-material --version` |
| 更新工具 | Windows 用 `python -m pip install --upgrade get-class-material`，macOS 用 `python3 -m pip install --upgrade get-class-material` |

<details>
<summary>其他指令與相容性</summary>

- 跳過附件、頁面或公告可分別加上 `--skip-pdfs`、`--skip-pages`、`--skip-announcements`。
- 加上 `--lang en` 使用英文介面，或加上 `--keep-terminal` 在結束時保留終端機。
- 舊指令 `ntu-cool-gcm` 仍可使用。
- 列出可見課程可執行 `ntu-cool-materials courses --refresh-session`。單獨下載並顯示公告可執行 `ntu-cool-materials announcements --course-id 57544 --refresh-session --print`。

</details>

## 疑難排解

| 遇到的狀況 | 處理方式 |
| --- | --- |
| 找不到 `python` 或版本太舊 | 安裝 Python 3.11 以上並重開終端機。macOS 用 `python3 --version` 確認。 |
| 已安裝套件，但找不到 `get-class-material` | Windows 可用 `python -m ntu_cool_materials pick` 啟動，macOS 請改用 `python3`。 |
| NTU COOL 登入已過期 | 執行 `get-class-material --refresh-session`，在瀏覽器完成登入。 |
| 缺少 Chromium、FFmpeg 或 Node.js | 執行 `ntu-cool-materials doctor --fix`。若指令找不到，可改用 `python -m ntu_cool_materials doctor --fix`（macOS 用 `python3`）。 |
| Windows 出現 WinError 4551 | 若啟動器被封鎖，可試 `python -X utf8 -m ntu_cool_materials pick`。若被封鎖的是 Chromium，可安裝 [Google Chrome](https://www.google.com/chrome/) 作為登入備援。 |
| Windows 找不到 `winget` | 從 Microsoft Store 安裝 **App Installer**，或手動安裝 [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) 和 [Node.js](https://nodejs.org/)。裝完請重開 PowerShell。 |
| macOS 出現 `CERTIFICATE_VERIFY_FAILED` | 若使用 python.org 的 Python，在「應用程式 → Python 3.x」執行 `Install Certificates.command`。 |

<details>
<summary>YouTube 影片下載失敗</summary>

公開與不公開（unlisted）影片會先嘗試直接下載。若工具詢問是否使用已登入的瀏覽器帳號重試，請先確認該帳號能播放影片。

- **Chrome／Edge**：輸入 `y` 前，先完全結束瀏覽器與背景程序，讓工具能讀取登入 cookie。
- **Safari**：可在「系統設定 → 隱私權與安全性 → 完整磁碟取用權限」授權使用的終端機。
- **缺少影音工具**：先跑 `doctor --fix`。macOS 若已安裝 Homebrew，也可執行 `brew install ffmpeg node`。
- 受 DRM 保護的影片無法透過本工具下載。

</details>

<details>
<summary>給開發者（原始碼安裝與測試）</summary>

```powershell
git clone https://github.com/jabir95tsai/get_class_material.git
cd get_class_material
python -m pip install -e .
python -m playwright install chromium
python -m unittest discover -s tests
```

macOS 請用 `python3`。開發筆記見 [CLAUDE.md](CLAUDE.md)。

</details>

## 授權、隱私與使用規範

> **注意：下載的教材僅限個人學習使用，請勿公開散布。** 詳細條款如下。

1. **軟體開源授權（MIT License）**
   - 本專案原始碼依 [MIT License](LICENSE) 授權釋出（Copyright (c) 2026 jabir）。
   - 任何人均得免費使用、複製、修改、合併、發布或散佈本軟體，惟須於軟體複本中保留原著作權聲明與授權條文；本軟體依「現況（AS IS）」提供，不附帶任何明示或默示擔保，開發者不對使用本工具所生之任何直接或間接損害負擔保或賠償責任。
2. **第三方套件與外部元件授權**
   - 本工具相依或使用之第三方軟體包括 `requests`、`playwright`、`yt-dlp`（選用功能另含 `notebooklm-py`、`markitdown`），以及使用者端安裝之 Chromium、FFmpeg 與 Node.js；各元件均依其原專案授權條款規範。
3. **課程教材著作權與使用範圍**
   - 本工具僅以使用者本人登入之合法權限存取課程資料，不繞過任何身分驗證、權限控管或 DRM 保護。
   - 下載所得之講義、投影片、頁面文字與影音內容，其著作權仍歸原授課教師、學校或原權利人所有。**僅限修課學生個人學習與複習使用**，禁止公開轉載、上傳網路、商業利用或散布予未經授權之第三方。
   - 若啟用選用之 NotebookLM 匯入功能（透過非官方個人版介面運作），請確認僅上傳你有權傳輸至第三方服務之教材，並自行遵守學校規定與 Google 服務條款。
4. **帳號安全與本機憑證隱私**
   - 台大帳號密碼僅由使用者親自輸入於工具開啟之 NTU COOL 官方登入頁，本程式不讀取、不側錄、不儲存亦不回傳密碼。
   - 登入後之工作階段憑證與 cookie 僅存於本機 `~/.ntu-cool-gcm/.secrets/`（若目前目錄已有 `.secrets/` 則沿用），不會傳送至開發者或任何第三方伺服器。該資料夾等同於你的登入狀態，**切勿上傳至 GitHub、雲端硬碟或分享給他人**。
