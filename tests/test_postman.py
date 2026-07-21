"""Postman Collection 结构测试。"""

import json
from pathlib import Path


def test_postman_collection_contains_fourtech_examples():
    collection_path = (
        Path(__file__).parents[1]
        / "postman"
        / "Baidu_Finance_API.postman_collection.json"
    )

    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    names = {item["name"] for item in collection["item"]}
    serialized = json.dumps(collection, ensure_ascii=False)

    assert "四方科技-按名称查询历史日K" in names
    assert "四方科技-按名称查询最新日线" in names
    assert "四方科技" in serialized
    assert "603339" in serialized
    assert "ab_sr" not in serialized
