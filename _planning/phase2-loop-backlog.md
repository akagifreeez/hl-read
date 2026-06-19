# hl-read Phase 2 — ループ実行バックログ（単一の真実）

このファイルがループの状態。毎回これを読み、**未チェックの先頭1項目だけ**を「反復契約」に沿って完遂し、チェックを付けてコミット&push。全部終わったら wrap-up して停止。

対象: `D:\working\hl-read`（現 v0.3.0 → 完了時 v0.4.0）
親計画: `_planning/layer1-expansion-plan.md` の **Phase 2（繋ぎの質）**
原則: **読み取り専用不変条件を全項目で維持**（Exchange未import・鍵欄なし・発注経路ゼロ）。

Phase 2 は「取得を増やす」のではなく「**繋ぎを長時間・不安定回線でも保つ**」のが目的。現状の耐障害層(v0.2.0)はHTTPのみ＝WSとエンドポイント運用が手薄。

---

## 反復契約（1項目＝1反復＝1コミット。Phase 1と同じ10点）

1. **確認**: SDKの該当実装(WebsocketManager / Info / API)を実コードで確認（推測で書かない）。
2. **lib実装**: `hl_read/info.py`。HTTPは既存 `_call()` を通す。後方互換を壊さない(既存メソッドの戻り型/署名を維持、追加は任意引数で)。
3. **CLI**: 必要なら `hl_read/cli.py` にサブコマンド/フラグ。
4. **MCP**: 運用系で意味があれば `hl_read/mcp_server.py` にツール追加（health等）。
5. **オフラインテスト**: `tests/` にSDKモックで挙動テスト（WS再接続は切断をシミュレートして検証）。
6. **全テスト緑**: venvで全件パス（落ちたら直す。赤のまま次へ進まない）。
7. **不変条件チェック**: `Exchange`/`private_key`/`sign`/`wallet` を新規に持ち込んでいないことを grep 確認。
8. **ライブ検証**: mainnet 読み取りのみ。**取引は絶対にしない**。WS系は切断を強制できないので「正常ストリームが動く＋オフラインで再接続検証」を以て可とする(その旨ログに明記)。
9. **README更新**: 新機能を反映。
10. **コミット&push**: 著者 `akagifreeez / akagifreeezworks@gmail.com`、フッタ2行（Co-Authored-By / Claude-Session）。→ チェックを付け「進捗ログ」に1行追記。

### 環境（毎回固定）
- venv: `D:\working\hl-mini\.venv\Scripts\python.exe` ＋ `PYTHONPATH=D:\working\hl-read`。**venvは作り直さない**。
- Bashは `cd /d/working/hl-read` を明示。
- ライブ検証アドレス例: `0x010461c14e146ac35fe42271bdc1134ee31c703a`。

---

## バックログ（上から順に。各行1反復）

- [x] **1. health() / `--check`** — 軽い読み取りで疎通・往復レイテンシ・APIエンドポイント・(取れれば)サーバ応答性を返す。lib `health()` + CLI `health`(or グローバル`--check`) + MCP `get_health` + test + ライブ + commit/push ✅
- [ ] **2. WS自動再接続** — ストリーム(`stream_book/stream_trades/stream_user_events/open_stream`)が切断時にバックオフ付きで再接続＆再購読。**後方互換維持**(既存の戻り/使い方を壊さない、再接続は既定ON or 任意フラグ)。SDK WebsocketManagerの挙動を確認した上でラップ。切断シミュレートのオフラインtest + 正常ストリームのライブ確認 + commit/push
- [ ] **3. エンドポイント設定可能化＋任意フォールバック** — `HLRead(api_url=...)` で接続先を上書き可能化＋任意 `fallback_urls` リスト(設定時のみ・持続的接続失敗で順次切替、既定は単一で無変化＝HLは公式単一ホストなのでspeculativeな多重化はしない/正直スコープ)。test + commit/push
- [ ] **4. wrap-up** — version 0.4.0 / README更新 / 全テスト緑の最終確認 / memory(hl-read-toolkit.md + MEMORY.md) 更新 / 最終 commit/push → **ループ停止**

---

## 進捗ログ（反復ごとに1行追記）
- 反復1 ✅ health: lib `health()`(単発・リトライしない=真の往復測定・never raise) + CLI `health`(downでexit 1・main戻り値尊重に変更) + MCP `get_health`(→16ツール) + test(39緑、ok/down/no-retryの3本)。ライブ=mainnet OK/23ms/866 markets/exit 0。不変条件grep OK。
