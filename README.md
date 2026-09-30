# LINE Bot：每日猜數字(1A2B)

本機沒有安裝 Python，所以整包用 Docker 跑。

## 1. 取得 LINE 金鑰

到 [LINE Developers Console](https://developers.line.biz/console/)：

1. 建立 Provider → 建立 **Messaging API** channel
2. **Basic settings** 頁 → 複製 `Channel secret`
3. **Messaging API** 頁 → 最下面發行並複製 `Channel access token (long-lived)`
4. 同一頁把 **Auto-reply messages** 和 **Greeting messages** 關掉（不然會跟自動回覆打架）

## 2. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`，填入剛剛的兩把金鑰。

## 3. 啟動

```bash
docker compose up --build
```

服務跑在 `http://localhost:8000`，健康檢查：`curl http://localhost:8000/health`

## 4. 讓 LINE 連得到你（開發用）

LINE 的 webhook 必須是公開的 HTTPS 網址，本機要用通道工具：

```bash
ngrok http 8000
```

把 ngrok 給的網址加上 `/callback`（例如 `https://abcd-1234.ngrok-free.app/callback`），
填進 **Messaging API → Webhook URL**，按 **Verify**，並把 **Use webhook** 打開。

## 5. 玩法

用手機加入這個 Bot 好友，傳訊息：

- 每天所有人共用同一組 **4 位不重複**的數字（0-9），台灣時間 00:00 換新題目。
- 傳一組 4 位數字（例如 `1234`）給 Bot 猜，Bot 回覆 `xAyB`，並附上你今天所有猜過的紀錄：
  - `A` = 數字對、位置也對
  - `B` = 數字對、位置不對
- 猜對就鎖定，當天不能再猜。
- 若在 **4 次以內**猜對，會獲得「出題資格」：下一則傳的 4 位**不重複**數字會被排入未來某一天的每日題目（先出先排隊；佇列空了就隨機出題）。
  - 還沒出題或跳過之前，傳其他文字 Bot 都會提醒你先出題。
- **排行榜**：猜對每日題目後會顯示當日排行榜，依猜的次數由少到多排列（同次數並列名次，先猜對的排前面），列前 10 名，你不在前 10 名時會另外顯示你的名次。名稱取自 LINE 個人資料，抓不到時顯示「神秘玩家」。
- **每日自動清除舊資料**：每天第一個人開始猜當天的題目時，Bot 會刪除前一天以前的排行榜、猜測紀錄、練習題和舊題目，所以只保留當天的資料，無法查詢過去的排行榜。還沒用掉的出題佇列、出題資格、玩家名稱和管理員設定不會被刪除。
- **練習題 `next`**：猜對每日題目後，傳 `next`（或 `下一題`）可以玩一題只有你自己的隨機題目，猜對後可以再傳 `next` 繼續。練習題**不列入排行榜**；還沒猜完就傳 `next` 的話，會公布上一題練習題的答案並換一題新的。
- 指令：
  - `查詢` / `紀錄` / `進度` — 看今天猜過的紀錄（有進行中的練習題也會一起顯示）
  - `排行榜` / `排行` / `排名` — 看今日排行榜
  - `next` / `下一題` — 猜對每日題目後玩練習題
  - `規則` / `說明` / `help` / `幫助` / `怎麼玩` — 看玩法
  - `跳過` / `取消` / `skip` — 放棄出題資格
- 其他無法辨識的訊息會回覆玩法說明。

## 管理員指令（隱藏功能，別跟其他人說）

第一個傳「`成功了`」或「`重置`」的人會自動綁定成管理員（記錄 LINE user ID 到 SQLite），別人傳同樣的字只會看到普通的規則說明，不會發現這是指令。

- `成功了` — 顯示今天的真實答案（僅管理員本人看得到，玩家出的題目會另外標註）
- `重置` / `重製` — 把今天的答案換成一組新的隨機數字
- `重置 1234` / `重製1234` — 把今天的答案改成指定數字

重置的是**當日的每日題目**（跟玩家用 `next` 開的練習題不同）：會清空所有人今天的猜測紀錄、次數、排行榜和練習題（等於重新開一局），並透過 LINE **廣播**通知所有好友。回覆中不會顯示答案，想看請再傳「成功了」。

> 廣播會消耗 Messaging API 的每月訊息額度；額度用完或權限不足時，重置仍會生效，只是回覆會提示廣播失敗。

想換管理員的話，直接刪除 `data/game.db` 裡 `bot_admin` 這張表的資料，下一個傳管理員指令的人就會變成新管理員。

## 檔案說明

- `app.py` — FastAPI webhook，驗簽後交給 `game.py` 處理並回覆
- `game.py` — 遊戲邏輯與 SQLite 儲存（每日題目、使用者進度與猜測紀錄、排行榜、練習題、出題佇列、管理員、重置廣播）
- `data/game.db` — SQLite 資料庫檔（由 `docker-compose.yml` 掛成 volume，重啟不會遺失；每天換題時會自動刪除前一天以前的紀錄）
- `Dockerfile` / `docker-compose.yml` — 容器設定
- `requirements.txt` — fastapi、uvicorn、line-bot-sdk v3
- `.env` — 你的金鑰（不要進版控）

## 常見問題

- **Webhook Verify 失敗**：確認容器有在跑、ngrok 網址沒過期、URL 結尾是 `/callback`
- **回 400 Invalid signature**：`.env` 的 `LINE_CHANNEL_SECRET` 填錯
- **回 401 Unauthorized**：`LINE_CHANNEL_ACCESS_TOKEN` 填錯或已失效
- **Bot 沒反應**：檢查 Console 裡 **Use webhook** 是否開啟、自動回覆是否關閉
