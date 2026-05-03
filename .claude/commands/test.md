テストを実行して結果を報告して：

1. `source .venv/bin/activate` で仮想環境を有効化
2. `pytest tests/ -v --tb=short` でテスト実行
3. 失敗したテストがあれば原因を分析して修正案を提示
4. カバレッジが気になる場合は `pytest tests/ -v --cov=. --cov-report=term-missing` も実行
