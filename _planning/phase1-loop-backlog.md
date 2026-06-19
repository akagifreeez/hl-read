# hl-read Phase 1 — ループ実行バックログ（単一の真実）

このファイルがループの状態。毎回これを読み、**未チェックの先頭1項目だけ**を「反復契約」に沿って完遂し、チェックを付けてコミット&push。全部終わったら wrap-up して停止。

対象: `D:\working\hl-read`（現 v0.2.0 → 完了時 v0.3.0）
親計画: `_planning/layer1-expansion-plan.md` の Phase 1
原則: **読み取り専用不変条件を全項目で維持**（Exchange未import・鍵欄なし・発注経路ゼロ）。

---

## 反復契約（1項目＝1反復＝1コミット。この10点を毎回満たす）

1. **エンドポイント確定**: SDK(`Info`)の実メソッド名 or `post(type=...)` を実コード/実APIで確認（推測で書かない）。
2. **lib実装**: `hl_read/info.py` に正規化メソッドを追加。必ず `_call()`（耐障害）＋必要なら `_cached()` を通す。生dictをそのまま返さず整形。
3. **CLI**: `hl_read/cli.py` にサブコマンド/フラグを追加。`--json` 対応。
4. **MCP**: `hl_read/mcp_server.py` に対応ツールを追加。
5. **オフラインテスト**: `tests/test_info.py` にSDKモックでパース（＋該当ならキャッシュ）テストを追加。
6. **全テスト緑**: venvで全件パス（落ちたら直す。赤のまま次へ進まない）。
7. **不変条件チェック**: `Exchange` / `private_key` / `sign` / `wallet` を新規に持ち込んでいないことを grep 確認（理想はそれを assert するテスト）。
8. **ライブ検証**: mainnet 読み取りのみで実行し出力を確認（実アドレス使用・**取引は絶対にしない**）。
9. **README更新**: 新メソッド/CLI/MCPツール数を反映。
10. **コミット&push**: 著者 `akagifreeez / akagifreeezworks@gmail.com`、フッタ2行（Co-Authored-By / Claude-Session）。→ このファイルのチェックを付ける。

### 環境（毎回固定・文脈リセットで失わないため明記）
- venv: `D:\working\hl-mini\.venv\Scripts\python.exe` ＋ `PYTHONPATH=D:\working\hl-read`。**venvは絶対に作り直さない**（ensurepipでハング）。`mcp`は導入済。
- Bashは `cd /d/working/hl-read` を明示（cwd取り違え注意）。
- ライブ検証アドレス例（クジラ・読み取り専用）: `0x010461c14e146ac35fe42271bdc1134ee31c703a`

---

## バックログ（上から順に。各行1反復）

- [x] **1. predicted_fundings()** — 全コイン予測funding（他venue比較含む）。lib + CLI `predicted` + MCP `get_predicted_fundings` + test + 検証 + commit/push ✅
- [ ] **2. portfolio(addr)** — アカウント価値/PnL時系列(day/week/month/all)。lib + CLI `portfolio <addr>` + MCP `get_portfolio` + test + 検証 + commit/push
- [ ] **3. fills_by_time(addr, since, until)** — 期間指定約定。lib + CLI `fills` に `--since/--until` + MCP `get_fills_by_time` + test + 検証 + commit/push
- [ ] **4. ledger(addr)** — 入出金/送金履歴(non-funding ledger)。lib + CLI `ledger <addr>` + MCP `get_ledger` + test + 検証 + commit/push
- [ ] **5. 出力形式** — 全CLIに `--format table|json|csv|ndjson` ＋ `export` サブコマンド（fills/ledger/candles→ファイル）+ test + commit/push
- [ ] **6. wrap-up** — version 0.3.0 / README のMCPツール数・機能一覧更新 / 全テスト緑の最終確認 / memory(hl-read-toolkit.md) 更新 / 最終 commit/push → **ループ停止**

（任意の拡張候補＝今回は対象外。必要なら後続: order_history / fees / staking / vault_equities / sub_accounts / explorer API）

---

## 進捗ログ（反復ごとに1行追記）
- 反復1 ✅ predicted_fundings: lib/CLI(`predicted`)/MCP(get_predicted_fundings→計12ツール)/test(17/17緑)。ライブ確認=230コイン・BTC HL+0.0013%/1h vs Bin/Bybit。不変条件grep OK。
