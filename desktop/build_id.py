"""壳指纹 —— **由 scripts/shell_id.py 生成，别手改**。

壳（desktop/ + 入口 + spec + Python 版本）的内容哈希。载荷只对**同一个壳**
有效，应用拿它和更新清单里的值比对，决定「只换载荷」还是「下整包」。
改了壳就跑 `python scripts/shell_id.py --write`，CI 会校验它没跑偏。
"""

SHELL_ID = "764dc2e4a08af661"
