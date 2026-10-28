# 使用しているデータ

- `site/recipes.json` は [dimkk/aion2-market-stack](https://github.com/dimkk/aion2-market-stack) の
  `exports/economy/recipes.json`（Apache License 2.0）を `tools/build_recipes.py` で軽量化したものです。
  変更点：使用するフィールドだけに絞り、コンボ時の製品IDを解決して埋め込みました。
  Apache License 2.0 の全文：https://www.apache.org/licenses/LICENSE-2.0
- 価格データは [aion2.exchange](https://aion2.exchange) の公開APIから取得しています。
- 日本語のアイテム名は NC 公式サイトのアイテム情報から取得しています。

AION2 は NCSOFT の商標です。本ツールは上記のいずれとも無関係です。
