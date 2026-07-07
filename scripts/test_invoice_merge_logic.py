#!/usr/bin/env python3
"""Unit tests for multi-invoice merge logic (mirrors n8n Code node 02 合并多发票结果)."""

from __future__ import annotations


MERGE_HINT = "🔔发票附件中，有多张发票合并识别的，发票必须销售方和购买方一致。"


def merge_invoices(items: list[dict]) -> dict:
    if not items:
        return {"mergeStatus": "error", "失败原因": "未获取到解析结果"}

    if any(not i.get("字段完整") or i.get("解析失败") for i in items):
        reasons = [
            f"[{i.get('attachment_name', '')}]{i.get('失败原因', '识别字段不完整')}".strip("[]")
            for i in items
            if not i.get("字段完整")
        ]
        return {
            "mergeStatus": "error",
            "失败原因": "；".join(filter(None, reasons)) or "部分发票识别字段不完整",
        }

    if len(items) == 1:
        one = {k: v for k, v in items[0].items() if k not in {"attachment_index", "attachment_name", "attachment_total"}}
        return {"mergeStatus": "ok", **one}

    buyer = items[0]["购方名称"]
    seller = items[0]["销方名称"]
    if not all(i["购方名称"] == buyer and i["销方名称"] == seller for i in items):
        return {"mergeStatus": "merge_error", "购方名称": MERGE_HINT}

    total = sum(float(i["价税合计"]) for i in items)
    dates = sorted(i["开票日期"] for i in items if i.get("开票日期"))
    return {
        "mergeStatus": "ok",
        "发票号码": "，".join(i["发票号码"] for i in items),
        "开票日期": dates[0],
        "购方名称": buyer,
        "销方名称": seller,
        "价税合计": f"{total:.2f}",
        "合并张数": len(items),
    }


def base_item(**overrides):
    item = {
        "字段完整": True,
        "解析失败": False,
        "发票号码": "25312000000123456789",
        "开票日期": "2026-06-25",
        "购方名称": "甲公司有限公司",
        "销方名称": "乙餐厅",
        "价税合计": "100.00",
        "attachment_index": 0,
        "attachment_name": "a.pdf",
    }
    item.update(overrides)
    return item


def test_single_invoice():
    result = merge_invoices([base_item()])
    assert result["mergeStatus"] == "ok"
    assert result["价税合计"] == "100.00"


def test_merge_same_parties():
    items = [
        base_item(发票号码="11111111111111111111", 价税合计="100.00", 开票日期="2026-06-20", attachment_index=0),
        base_item(发票号码="22222222222222222222", 价税合计="200.50", 开票日期="2026-06-25", attachment_index=1),
    ]
    result = merge_invoices(items)
    assert result["mergeStatus"] == "ok"
    assert result["发票号码"] == "11111111111111111111，22222222222222222222"
    assert result["价税合计"] == "300.50"
    assert result["开票日期"] == "2026-06-20"


def test_merge_different_parties():
    items = [
        base_item(attachment_index=0),
        base_item(购方名称="另一家公司", attachment_index=1),
    ]
    result = merge_invoices(items)
    assert result["mergeStatus"] == "merge_error"
    assert result["购方名称"] == MERGE_HINT


def test_partial_failure():
    items = [
        base_item(attachment_index=0),
        base_item(字段完整=False, 失败原因="缺少销方名称", attachment_index=1),
    ]
    result = merge_invoices(items)
    assert result["mergeStatus"] == "error"


def test_three_invoices_same_parties():
    items = [
        base_item(发票号码=f"{i}" * 20, 价税合计=f"{i}.00", attachment_index=i)
        for i in range(1, 4)
    ]
    result = merge_invoices(items)
    assert result["mergeStatus"] == "ok"
    assert result["合并张数"] == 3
    assert result["价税合计"] == "6.00"


def main():
    test_single_invoice()
    test_merge_same_parties()
    test_merge_different_parties()
    test_partial_failure()
    test_three_invoices_same_parties()
    print("All merge logic tests passed.")


if __name__ == "__main__":
    main()
