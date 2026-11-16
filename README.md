# NTU COOL Get Class Material

[![PyPI](https://img.shields.io/pypi/v/get-class-material)](https://pypi.org/project/get-class-material/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

一行指令下載 NTU COOL 整門課的講義、公告、頁面文字與上課影片，依週次整理，方便丟給 AI 複習。  
One command to download NTU COOL slides, pages, announcements, and videos by week.

---

## 開始使用（Windows）

1. 按 `Win` 鍵 → 輸入 `PowerShell` → 按 `Enter`。
2. 複製貼上以下兩行，按 `Enter`：

```powershell
python -m pip install get-class-material
get-class-material
```


> - **找不到 `python`？** 到 <https://www.python.org/downloads/> 安裝，務必勾選底部的 **`Add Python to PATH`**，裝完**重開 PowerShell**。
> - **檔案存放在哪？** 預設在「文件 (Documents)」裡的 `ntu-cool-gcm_material`。
> - **下載內容：** 講義附件（保留原格式）、頁面（`.md`）、公告（`announcements.md`）、YouTube／上課錄影（`.mp4`）、目錄索引（`course_overview.md`，可直接拖進 ChatGPT / Gemini / NotebookLM）。
> - **補抓與更新：** 補抓新檔案直接重跑 `get-class-material`（舊檔自動跳過）；更新版本跑 `python -m pip install --upgrade get-class-material`。

---

## 開始使用（MacOS）

Mac 內建 Python 3.9 不能用，請先到 <https://www.python.org/downloads/macos/> 安裝新版（不要用 Homebrew Python），完成後開「終端機」執行：

```bash
python3 -m pip install get-class-material
get-class-material
```

- 出現 `command not found`：執行 `python3 -m ntu_cool_materials doctor --fix` 後重開終端機。
- Safari 私人 YouTube 影片：需於「系統設定 → 隱私權與安全性 → 完整磁碟取用權限」開啟「終端機」。
- 更新版本：`python3 -m pip install --upgrade get-class-material`。

---

## 疑難排解

```text
執行時卡關了？從上往下問自己：

1. 輸入指令就出現「找不到 python / pip」或「無法辨識」？
   ├─ Yes ─▶【解方 A】安裝 Python 3.11+ 並勾選 Add Python to PATH
   └─ No  ─▶ 往下看第 2 題

2. 出現「應用程式控制原則已封鎖」(WinError 4551) 或「已停用指令碼執行」？
   ├─ Yes（封鎖檔案 / 4551）─▶【解方 B】改用 python -X utf8 指令 / Chrome 備援
   ├─ Yes（已停用指令碼）   ─▶【解方 C】調整 PowerShell 執行原則
   └─ No                    ─▶ 往下看第 3 題

3. 卡在自動安裝元件（找不到 winget、缺 ffmpeg / Node.js、Mac command not found）？
   ├─ Yes（找不到 winget）              ─▶【解方 D】補裝 App Installer 或手動裝工具
   ├─ Yes（command not found / 缺元件） ─▶【解方 E】執行 doctor --fix 檢查與自動修復
   └─ No                                ─▶ 往下看第 4 題

4. 出現「CERTIFICATE_VERIFY_FAILED」（Mac SSL 憑證錯誤）？
   ├─ Yes ─▶【解方 F】執行 Mac Python 的 Install Certificates.command
   └─ No  ─▶ 往下看第 5 題

5. 可以選課，但 YouTube 影片下載失敗？
   ├─ Yes ─▶【解方 G】關閉 Chrome / Edge 後按 y 重試（或開 Safari 權限）
   └─ No  ─▶ 往下看第 6 題

6. 下載到一半斷網、當掉，或提示「登入已過期」？
   └─ Yes ─▶【解方 H】直接重跑（自動續傳），或加 --refresh-session 強制重登
```

<details>
<summary>點此展開各解方詳細步驟（解方 A ～ H）</summary>

### 解方 A：找不到 `python` / `pip`，或版本低於 3.11
- **Windows：** 到 <https://www.python.org/downloads/> 下載安裝檔 → **務必勾選畫面最下方的 `Add Python to PATH`**（或 `Add python.exe to PATH`）→ 點 Install Now → 裝完**關掉原本的 PowerShell，開一個新視窗**，執行 `python --version` 確認為 `3.11` 以上。
- **Mac：** 內建 Python 3.9 不能用。請到 <https://www.python.org/downloads/macos/> 下載 `.pkg` 安裝（不要用 Homebrew Python），裝完重開終端機，指令一律改用 `python3`。

### 解方 B：Windows「應用程式控制原則已封鎖此檔案」（WinError 4551）
- **主程式被擋：** 改用 Python 模組方式啟動：
  ```powershell
  python -X utf8 -m ntu_cool_materials pick
  ```
- **內建 Chromium 瀏覽器被擋：** 請安裝一般版 [Google Chrome](https://www.google.com/chrome/)（具官方簽章），工具會自動改用 Chrome 登入。
- **`ffmpeg.exe` / `node.exe` 被擋：** 其餘講義、頁面與上課錄影仍可正常下載；完整 YouTube 畫質合併需請電腦管理員放行該執行檔（請勿關閉 Windows Smart App Control，關閉後無法重新開啟）。

### 解方 C：PowerShell「已停用指令碼執行」
1. 對 PowerShell 圖示按右鍵 →「**以系統管理員身分執行**」。
2. 貼上執行並按 `Y` 確認：
   ```powershell
   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
   ```
3. **關掉系統管理員視窗**，改用一般模式重開 PowerShell 即可。

### 解方 D：找不到 `winget`（自動安裝 ffmpeg / Node.js 失敗）
- **方法一：** 到 Microsoft Store 搜尋並安裝「**應用程式安裝程式 (App Installer)**」，裝完重開 PowerShell。
- **方法二：** 手動下載安裝 [ffmpeg（essentials 版）](https://www.gyan.dev/ffmpeg/builds/) 與 [Node.js（LTS 版）](https://nodejs.org/)，裝完重開 PowerShell。

### 解方 E：缺元件、`command not found` 或先前跳過安裝 ffmpeg
- 檢查環境狀態（Mac 請將 `ntu-cool-materials` 改寫為 `python3 -m ntu_cool_materials`）：
  ```powershell
  ntu-cool-materials doctor
  ```
- 嘗試自動修復並補裝缺少項目：
  ```powershell
  ntu-cool-materials doctor --fix
  ```
- 執行完畢後**關掉視窗重開**再試一次。Mac 若需單獨裝 YouTube 元件，可先裝 Homebrew 再執行 `brew install ffmpeg node`。

### 解方 F：Mac 出現 `CERTIFICATE_VERIFY_FAILED`
打開 Finder →「應用程式」→「`Python 3.x`」資料夾 → 點兩下執行 **`Install Certificates.command`**。

### 解方 G：YouTube 私人／限齡／會員影片下載失敗
- 不公開（unlisted）影片免登入即可下載；只有私人、限齡或會員影片會詢問 `要用瀏覽器裡已登入的帳號重試嗎? [y/N]`。
- **使用 Chrome / Edge：** 輸入 `y` 之前，**請先完全關掉所有 Chrome / Edge 視窗**（含背景執行），否則登入 cookie 檔會被鎖定而讀取失敗；Firefox 通常不需關閉。
- **Mac 使用 Safari：** 需至「系統設定 → 隱私權與安全性 → 完整磁碟取用權限」開啟「終端機」。
- 少數受 DRM 保護的影片無法下載屬平台限制；瀏覽器也須事先登入有權限觀看該影片的帳號。

### 解方 H：下載到一半中斷，或 NTU COOL 登入過期
- 直接重跑 `get-class-material` 即可：已完整下載的檔案會自動跳過，中斷處會安全續傳，登入過期也會自動跳出瀏覽器重登。
- 若想強制重新登入，請執行：
  ```powershell
  get-class-material --refresh-session
  ```

</details>

---

## 進階功能與其他指令

<details>
<summary>自動匯入 NotebookLM（選用・實驗功能）</summary>

```powershell
python -m pip install "get-class-material[notebooklm]"
notebooklm login
get-class-material --notebooklm
```

- Mac 把 `python` 改為 `python3`；若 Google 擋登入改用 `notebooklm login --browser chrome`。
- 已下載的課程可直接執行 `ntu-cool-materials notebooklm` 選課匯入（個人版預設上限 50 來源，可用 `--notebooklm-max-sources` 調整；加 `--notebooklm-no-media` 略過影音；Excel 會自動轉 `.xlsx.md`）。

</details>

<details>
<summary>進階指令一覽</summary>

```powershell
# 不要影片，只抓講義與頁面（可混搭 --skip-pdfs / --skip-pages / --skip-announcements / --skip-youtube / --skip-cool-videos）
get-class-material --skip-youtube --skip-cool-videos

# 指定存檔資料夾
get-class-material --out D:\我的課程

# 指定課程 ID 下載（網址 courses/ 後面的數字）
ntu-cool-materials download-course --course-id 57544

# 環境檢查與自動修復
ntu-cool-materials doctor
ntu-cool-materials doctor --fix

# 其他常用參數：--refresh-session（強制重登）、--keep-terminal（結束不關視窗）、--lang en（英文介面）、--version（查版本）
```

舊版指令 `ntu-cool-gcm` 仍相容。另可使用 `ntu-cool-materials courses --refresh-session` 與 `ntu-cool-materials announcements --course-id 57544 --refresh-session --print`。

</details>

<details>
<summary>給開發者（原始碼安裝與測試）</summary>

```powershell
git clone https://github.com/jabir95tsai/get_class_material.git
cd get_class_material
pip install -e .
python -m playwright install chromium
python -m unittest discover -s tests
```

架構見 [CLAUDE.md](CLAUDE.md) · [PyPI](https://pypi.org/project/get-class-material/) · [GitHub](https://github.com/jabir95tsai/get_class_material) · [MIT License](LICENSE)

</details>

---

## 授權、隱私與使用規範

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
