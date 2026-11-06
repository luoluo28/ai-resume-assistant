"""飞书多维表格导出。

密钥从环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 读取（绝不硬编码、绝不提交）。
字段名与 JSON 契约对齐：岗位名 / 结论 / 分数 / 理由 / 差距 / 建议 / 打招呼。
"""
from __future__ import annotations

import os

import requests

BASE_URL = "https://open.feishu.cn/open-apis"


def _creds() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError(
            "缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请在本地 .env 填（绝不提交）"
        )
    return app_id, app_secret


def _tenant_token() -> str:
    app_id, app_secret = _creds()
    resp = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书获取 token 失败: {data.get('msg')}")
    return data["tenant_access_token"]


def to_feishu(advices: list[dict], app_token: str, table_id: str) -> dict:
    """把多条投递建议写入飞书多维表格（batch_create）。"""
    from .export import advice_to_row

    token = _tenant_token()
    records = [{"fields": advice_to_row(a)} for a in advices]
    resp = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
        headers={"Authorization": f"Bearer {token}"},
        json={"records": records},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书写入失败: {data.get('msg')}")
    return data.get("data", {})
