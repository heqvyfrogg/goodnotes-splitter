# GoodNotes 5MB Splitter

GoodNotes 6 / 5 形式のアーカイブ（`.goodnotes`）を、5MBのアップロード制限に合わせて無劣化で自動分割するツールキットです。
Python CLI（標準ライブラリのみ）およびブラウザ完結型Webアプリケーション（HTML5 / JSZip / PHP）を提供します。

- **Web公開URL**: [https://pdevhub.xyz/good5mb/](https://pdevhub.xyz/good5mb/)
- **完全技術仕様書**: [GOODNOTES_SPLITTER_SPEC.md](GOODNOTES_SPLITTER_SPEC.md)

---

## 主な機能と解決された技術的課題

### 1. ページ表示順序の厳密な維持（F55 移動追跡）
GoodNotes はインポート時に `index.notes.pb` の行順を使用せず、イベントジャーナル内の最新オーダーキー（F54 または F55 フィールド）の ASCII 辞書順でページをソートします。本ツールは各ページに更新された最新キーを追跡し、実機通りの表示順序を復元した上で各パーツに単調増加キー（`a0001`, `a0002`...）を再付与します。

### 2. 削除済みページの自動除外（F56 削除追跡）
アプリ上で削除されたページは `index.notes.pb` に履歴として残存しますが、実機では非表示となります。本ツールは F56（ページ削除）レコードを検知して不要ページを確実に除外します（本番78行中、アクティブな62ページのみを抽出）。

### 3. アタッチメント・用紙・ストロークの完全保持
- 用紙定義（F2）、アタッチメント登録（F6）、手書きストローク（F102）、ページバインド（F105）をすべて過不足なく保持し、インポート後の白紙化を防止。
- `index.attachments.pb` のキーUUIDと実際のファイルパス（`attachments/<UUID>`）の不一致（168件中54件）を正確にマッピングし、用紙や画像の欠落をゼロに抑制。

---

## 構成

- `goodnotes_splitter/`: Python 3.13 CLI スプリッタ（外部依存なし）
  - `reader.py`: ジャーナル解析・表示順復元・削除除外
  - `partitioner.py`: 5MB制限に基づく貪欲分割
  - `writer.py`: イベント再構築・単調増加オーダーキー付与・ZIP生成
  - `validator.py`: 16項目の整合性自動検証
- `web/`: Webアプリケーション
  - `index.html`: 高密度スリムUI（Zero Emoji、モバイル/iPad Safari最適化）
  - `splitter.js`: ゼロ依存のJavaScript版Protobuf＆分割エンジン
  - `app.js`: クライアントサイド完全完結コントローラ
  - `api.php`: PHP 8.0 バックエンド
- `GOODNOTES_SPLITTER_SPEC.md`: 内部仕様・不具合解析・実機検証の完全技術報告書

---

## 使用方法

### CLI（Python）
```bash
# カレントディレクトリ内の .goodnotes を自動検出して分割
python -m goodnotes_splitter

# 明示的な指定
python -m goodnotes_splitter "input.goodnotes" -o output_dir --target-mb 4.5
```

### Web
ブラウザで `https://pdevhub.xyz/good5mb/` にアクセスし、ファイルをドラッグ＆ドロップするだけでブラウザ内（クライアント端末）で即座に分割が完了します。
