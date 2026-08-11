# Severity 校準表

發 CRITICAL / HIGH / MEDIUM / LOW 標籤的共用判定規則。消費端：`security-reviewer` / `code-reviewer` / `/pr-review`。

吸收自 openai/codex-security `_bundled_plugin/skills/attack-path-analysis/references/severity-policy.md` + `attack-path-facts.md`（Apache-2.0，2026-08-01）。

## 三段分開做，不要一次想完

1. **釘事實** — 填下面四格，只根據 code 與 repo 證據。
2. **套表** — 機械查表得出級別。
3. **套完不再重新論述** — 事實釘死後不准回頭憑直覺推翻表的結果。要改就回第 1 步改事實，並說明改了哪一格、依據什麼證據。

## 第 1 步：四格事實（不填不准給級別）

| 欄位 | 允許值 |
|---|---|
| **向量** | `remote` / `local_network` / `localhost` / `none` / `unknown` |
| **認證範圍** | `public` / `internal-only` / `admin-only` / `unknown` |
| **攻擊者輸入控制** | `yes` / `plausible` / `no` / `unknown` |
| **前提可達性** | `plausible` / `unlikely` / `unachievable` / `unknown` |

釘事實時強制反問一次：有沒有 repo 證據指向相反結論——不在範圍內 / 只在內部 / 只有 admin 走得到 / 沒有真的跨越信任邊界 / 攻擊者其實碰不到？逐條答，並說明它為何具或不具決定性。

**未驗證就回落**：支撐 HIGH / CRITICAL 的四格事實，只要仍寫著「未驗證」「需確認」或其他 proof gap，該格必須填 `unknown`；六項驗收中依賴該事實的項目不得算通過。不能用「平台理論上支援」代替「這個部署／商家／路徑實際可達」。

不要編造 code 支撐不了的攻擊鏈。先判「這是安全漏洞，還是單純的正確性 bug」。

## 第 2 步：硬抑制（先於套表，命中就 ignore）

- **影響只及自己** — self-XSS、只能弄壞自己 session 的操作。
- **前提不可達或極不現實** — `前提可達性 = unachievable`。
- **需要先握有 admin / root / shell / 實體存取** — 除非「提權本身」就是這個 finding（即那個 delta 才是漏洞）。

## 第 3 步：套表

**likelihood 由向量決定**：`remote` → high、`local_network` → medium、`localhost` → low、`none` → 不加分。`攻擊者輸入控制 = no` 或 `前提可達性 = unlikely` 各降一級。

| impact ↓ ／ likelihood → | high | medium | low | unknown |
|---|---|---|---|---|
| **high** | CRITICAL（須過下方六項，否則 HIGH） | MEDIUM | LOW | MEDIUM |
| **medium** | MEDIUM | LOW | LOW | LOW |
| **low** | LOW | LOW | LOW | LOW |
| **unknown** | MEDIUM | LOW | LOW | LOW |

**注意 HIGH 只有 `impact=high × likelihood=high` 一個入口**（過六項就升 CRITICAL、沒過才留 HIGH）。unknown 一律往下靠。

危險的 sink、可怕的 bug class、敏感的檔案位置，**單獨都不足以撐住高嚴重度**。CRITICAL 的語意是「現在就要處理、威脅是立即而可能的」。

## HIGH / CRITICAL 六項驗收（全過才留，缺一降級）

1. 元件在範圍內（是產品 / runtime 路徑，不是 dev-only、測試、範例、prototype）
2. 攻擊者真實存在（這個威脅角色在這個系統的現實用法裡存在）
3. 攻擊面合理可達
4. 利用路徑可信而非臆測（每一段都有 code 證據，不靠假設接續）
5. 影響屬重大安全影響（不是可用性抱怨或程式碼品味）
6. **換成有信譽的稽核公司做 bug bounty triage，也會判 high 以上**

## 不該留 HIGH / CRITICAL 的情形（降級，不是不報）

- self-XSS 或其他沒有跨越信任邊界的影響
- 缺 header / cookie flag / CSP / TLS 這類衛生問題，但講不出具體利用鏈
- 「跟別的東西串起來也許就危險」——串接假設超過一層
- 只展示了 bug class 的存在，沒有實際可達的利用路徑

## 反向護欄（防止表被拿來當刪除工具）

- **介面是內部或私有 → 降 likelihood，不是直接 ignore。** 內部曝露通常降的是可能性或信心，不是把 finding 抹掉。
- **影響或可能性判為低 → 降級，不是刪掉。** 一條本來就該報的 finding，不因為算出來是 LOW 就消失。
- **找不到 ingress、deployment、部署證據 → 降 confidence 或該格填 `unknown`，不是自動抑制。** 缺證據是 proof gap，不是反證。

## 校準回饋迴路

本表刻意偏保守，導入的直接後果是 Must Fix / Block 次數下降。若長期**完全沒有任何 finding 通過六項驗收拿到 CRITICAL**，代表校準過嚴，回頭放寬（第一順位候選：把 `impact=high × likelihood=medium` 從 MEDIUM 提到 HIGH）。
