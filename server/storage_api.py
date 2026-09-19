# -*- coding: utf-8 -*-
"""存储管理接口（路线 §7 P1-2）。

  GET  /api/storage        占用明细（分类 + 逐书/逐作品/逐源）
  POST /api/storage/clear  选择性清理（必须显式给出 scope，不做隐式"清空所有"）

为什么单独一个蓝图：存储清点是**读多写少、与爬取无关**的能力，
放在 novel_api/manga_api 里会让那两个模块继续膨胀；这里只依赖 server.storage。
"""
from flask import Blueprint, jsonify, request

from server import storage

bp = Blueprint("storage", __name__)


@bp.route("/api/storage")
def api_storage_usage():
    return jsonify(storage.usage())


@bp.route("/api/backup/scope")
def api_backup_scope():
    """备份范围报告（"完整备份范围明确"）：包含什么、明确不含什么，各自带真实体积"""
    return jsonify(storage.backup_scope())


@bp.route("/api/storage/clear", methods=["POST"])
def api_storage_clear():
    data = request.get_json(silent=True) or {}
    res = storage.clear(data)
    # 参数问题 → 400（客户端要能区分"没删成"与"删了 0 字节"）
    return jsonify(res), (200 if res.get("ok") else 400)
