# hl-read 拡張計画 — Layer 1 を横に広げる

作成: 2026-06-19 / 対象: `D:\working\hl-read`（v0.2.0）

## レイヤーの現状認識

```
┌─────────────────────────────────────────────────┐
│ Layer 2  溜める / 集計分析 / グラフ              │  ← 薄い。hl-liqmap 1本だけ（清算ヒートマップ専用）
├─────────────────────────────────────────────────┤
│ Layer 1  取得・繋ぎ・軽い整形・配信              │  ← 作り込んだ（v0.2.0）= hl-read
└─────────────────────────────────────────────────┘
```

- Layer 1（hl-read）= 取得 + 軽い整形（正規化レベル）+ WS配信 + MCP + 耐障害層。**溜めない・グラフ描かない**（設計上わざと。「鍵を渡さない読み取り専用」を軽量に保つため）。
- 本計画は **Layer 1 を横に広げる**もの。溜める/分析/グラフ（Layer 2）には踏み込まず役割分担を崩さない。

## 原則

- **全項目で読み取り専用不変条件を維持**（すべて Hyperliquid Info 側の読み取り。Exchange/private key には一切触れない。`post()` も read のみ）。
- 各エンドポイントの厳密な名前/可否は実装時に SDK(`Info`) と実 API で確定（SDK 未ラップ分は `post(type=...)` で到達）。

---

## 広げ方を5軸に分解

| 軸 | 意味 | 一言 |
|---|---|---|
| A. 取得エンドポイント網羅 | 同じ Hyperliquid で「取れるのに繋いでない」読み取りを足す | 一番の本丸・横幅 |
| B. 出口(出力・連携)を増やす | 取ったデータの出し先（CSV/NDJSON/MCP/ファイル/webhook） | 接着の出口側 |
| C. 繋ぎの質 | 複数ホスト fail-over・WS 自動再接続・疎通チェック | 信頼性の横幅 |
| D. 配布 | PyPI 公開で「使える形」にして広げる | 到達範囲 |
| E. スコープ拡大（任意・大） | 同インターフェースで他 read-only ソース | 横の最果て |

---

## A. 取得エンドポイント網羅（最重要の横幅）

※ SDK 直メソッド or `Info.post(type=...)` で到達可能。発注系は一切含めない。

### A1. ユーザー系（公開アドレスの読み取り）

| 追加機能 | 取れるもの | 効果 | 工数 |
|---|---|---|---|
| `portfolio(addr)` | アカウント価値/PnLの時系列(day/week/month/all) | ★グラフ素材・後続ダッシュボードの核 | S |
| `ledger(addr)` | 入出金・送金履歴(non-funding ledger) | ★hl-tax-jp（日本雑所得CSV）の土台 | S |
| `fills_by_time(addr,start,end)` | 期間指定約定（現状は直近N件のみ） | 税/分析で必須 | S |
| `order_history(addr)` / `order_status(oid)` | 注文履歴・約定状態 | 取引履歴の完全性 | S |
| `fees(addr)` | 手数料率・出来高ティア | アカウント分析 | S |
| `staking(addr)` | delegations / rewards（ステーキング） | 残高の全体像 | M |
| `vault_equities(addr)` | HLP等vaultへの預け | 残高の全体像 | S |
| `sub_accounts(addr)` | サブアカウント一覧 | 集約 | S |

### A2. 市場・メタ系

| 追加機能 | 取れるもの | 効果 | 工数 |
|---|---|---|---|
| `predicted_fundings()` | 全コインの予測funding（他venue比較含む） | ★funding系の集約・裁定の素材 | S |
| `vault_details(vault)` | HLPのAPR/TVL/履歴 | vault監視 | S |
| `deploy_auctions()` | 新規上場(spot/perp)オークション状態 | ★HIP-3 Market Radar の土台 | M |
| `open_interest()` | OI単体整形（現状はmarkets内に埋没） | 取り回し改善 | S |

### A3. エクスプローラ/オンチェーン系（別ホスト）

| 追加機能 | 取れるもの | 効果 | 工数 |
|---|---|---|---|
| `block(h)` / `tx(hash)` / `address(addr)` | ブロック/Tx/アドレス詳細 | オンチェーン照合 | M |

---

## B. 出口(出力・連携)を増やす

| 項目 | 内容 | 工数 |
|---|---|---|
| `--format csv\|ndjson` | 全CLIに出力形式（現状json/表のみ） | S |
| `export` サブコマンド | fills/ledger/candles をファイルに吐く（分析/税ツールへの橋） | S |
| MCPツール拡張 | 11→A1/A2の主要を追加（portfolio/ledger/predicted_fundings等） | S |
| stream→出口 | `stream_* --to-file`（追記）or callback/webhookブリッジ | M |

## C. 繋ぎの質（信頼性の横幅）

| 項目 | 内容 | 工数 |
|---|---|---|
| WS自動再接続 | 現状の耐障害層はHTTPのみ。stream系の再接続/バックオフを確認＆強化（長時間watchの要） | M |
| 複数APIホスト fail-over | 単一base_url→候補リストで切替 | M |
| `health()` / `--check` | 疎通・レイテンシ・APIバージョン確認 | S |

## D. 配布

| 項目 | 内容 | 工数 |
|---|---|---|
| PyPI公開 | `pip install hl-read`。Layer1を世に出す本丸（→その後hl-liqmapの裸依存も解消） | S |

## E. スコープ拡大（任意・将来・大）

- 同じ `HLRead` 形のインターフェースで他の読み取り専用ソース（別DEX等）のアダプタ。横の最果て。今は保留候補。

---

## 推奨フェーズ（即効性×低リスク順）

- **Phase 1 ｜ 取得の横幅＋出口**（S中心・最も効く）
  `portfolio` / `ledger` / `fills_by_time` / `predicted_fundings` ＋ 対応MCPツール ＋ `--format csv/ndjson`・`export`
  → 後続案（hl-tax-jp / fundingダッシュボード / グラフ素材）の土台が一気に揃う。全部read-only維持。

- **Phase 2 ｜ 繋ぎの質**
  WS自動再接続の確認＆強化 ＋ `health/--check` ＋ 複数ホストfail-over
  → 長時間watch・MCP常駐に耐える"繋ぎ"へ。

- **Phase 3 ｜ 網羅の仕上げ**
  `order_history` / `fees` / `staking` / `vault` / `sub_accounts` / explorer API

- **Phase 4 ｜ PyPI公開** （単独でいつでも実施可。Phase1後が名刺効果◎）

---

## この計画の芯

- 横に広げる＝「取得・繋ぎ・出口」を厚くする。溜める/分析/グラフ（Layer 2）には踏み込まない＝役割分担を崩さない。
- 読み取り専用の売りは全Phaseで不変（Info側の読み取りのみ／post()もread）。
- Phase 1 の4本（portfolio/ledger/fills_by_time/predicted_fundings）は、後続案 hl-tax-jp・HIP-3 Radar・fundingダッシュボードの共通土台になる＝1回の拡張が複数案に効く。
