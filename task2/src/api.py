#!/usr/bin/env python3
"""
==============================================================================
api.py - CPU Profiling 工具 HTTP API 服务
==============================================================================
提供 RESTful API 接口，支持健康检查、文件查询、火焰图生成、系统状态。

用法：
    python api.py
    python api.py --port 9090
    python api.py --port 8080 --data-dir /data/perf_raw --svg-dir /data/perf_svg

接口列表：
    GET /api/health       - 健康状态
    GET /api/status       - 系统概览
    GET /api/files        - 按时间查询文件列表
    GET /api/flamegraph   - 生成火焰图（返回 SVG）
==============================================================================
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_from_directory

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_PORT = 8080
DEFAULT_DATA_DIR = os.environ.get("DATA_DIR", "/data/perf_raw")
DEFAULT_SVG_DIR = os.environ.get("SVG_DIR", "/data/perf_svg")

# 脚本目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# web/ 静态文件目录（与 api.py 同级的 web/ 文件夹）
WEB_DIR = os.path.join(SCRIPT_DIR, "web")

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

# 全局配置，在 main() 中设置
config = {
    "data_dir": DEFAULT_DATA_DIR,
    "svg_dir": DEFAULT_SVG_DIR,
    "port": DEFAULT_PORT,
}


# ======================================================================
# 辅助函数
# ======================================================================

def get_query_script() -> str:
    """返回 query.py 的路径"""
    return os.path.join(SCRIPT_DIR, "query.py")


def get_flamegraph_script() -> str:
    """返回 flamegraph.py 的路径"""
    return os.path.join(SCRIPT_DIR, "flamegraph.py")


def get_cleaner_script() -> str:
    """返回 cleaner.sh 的路径"""
    return os.path.join(SCRIPT_DIR, "cleaner.sh")


def check_collector_running() -> bool:
    """检查 collector.sh 采集进程是否在运行。

    通过检查是否存在 perf record 进程来判断。
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "perf record"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def get_disk_usage(directory: str) -> str:
    """获取目录磁盘占用（人类可读格式）。"""
    try:
        result = subprocess.run(
            ["du", "-sh", directory],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.split()[0]
    except Exception:
        pass
    return "N/A"


def count_perf_files(data_dir: str) -> int:
    """统计 perf_*.data 文件数量。"""
    try:
        pattern = os.path.join(data_dir, "perf_*.data")
        return len(glob.glob(pattern))
    except Exception:
        return 0


def get_file_timestamps(data_dir: str) -> list[dict]:
    """获取所有 perf 文件的文件名和时间戳摘要。"""
    files_info = []
    pattern = os.path.join(data_dir, "perf_*.data")
    for f in sorted(glob.glob(pattern)):
        try:
            stat = os.stat(f)
            files_info.append({
                "name": os.path.basename(f),
                "path": f,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            })
        except OSError:
            continue
    return files_info


# ======================================================================
# 前端页面 & 静态文件
# ======================================================================

@app.route("/", methods=["GET"])
def index():
    """返回 Web 前端首页。"""
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:path>", methods=["GET"])
def serve_static(path):
    """提供 web/ 目录下的静态文件（备用）。"""
    file_path = os.path.join(WEB_DIR, path)
    if os.path.isfile(file_path):
        return send_from_directory(WEB_DIR, path)
    return jsonify({"error": "Not Found"}), 404


# ======================================================================
# API 路由
# ======================================================================

@app.route("/api/health", methods=["GET"])
def api_health():
    """健康状态检查。

    返回服务状态和采集器运行状态。

    GET /api/health
    """
    collector_running = check_collector_running()

    return jsonify({
        "status": "ok",
        "collector_running": collector_running,
        "data_dir": config["data_dir"],
        "svg_dir": config["svg_dir"],
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/api/status", methods=["GET"])
def api_status():
    """系统概览：磁盘占用、文件数量、采集状态。

    GET /api/status
    """
    collector_running = check_collector_running()
    file_count = count_perf_files(config["data_dir"])
    disk_usage = get_disk_usage(config["data_dir"])
    svg_disk_usage = get_disk_usage(config["svg_dir"])
    recent_files = get_file_timestamps(config["data_dir"])[-5:]  # 最近5个文件

    return jsonify({
        "collector_running": collector_running,
        "data_dir": config["data_dir"],
        "disk_usage": disk_usage,
        "file_count": file_count,
        "svg_dir": config["svg_dir"],
        "svg_disk_usage": svg_disk_usage,
        "recent_files": recent_files,
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/api/files", methods=["GET"])
def api_files():
    """按时间范围查询采样文件列表。

    调用 query.py 子进程完成查询。

    参数:
        start (str): 起始时间，格式 "YYYY-MM-DDTHH:MM" 或 "YYYY-MM-DD HH:MM"
        end   (str): 结束时间，格式同上，不传则默认当前时间

    GET /api/files?start=2026-06-19T03:00&end=2026-06-19T03:05
    """
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()

    if not start:
        return jsonify({"error": "缺少参数: start"}), 400

    # 规范化时间格式：将 T 分隔符转为空格
    start = start.replace("T", " ")
    if end:
        end = end.replace("T", " ")

    # 构建 query.py 命令
    query_script = get_query_script()
    cmd = [
        sys.executable, query_script,
        "--start", start,
        "--data-dir", config["data_dir"],
    ]
    if end:
        cmd.extend(["--end", end])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return jsonify({
                "error": f"query.py 执行失败",
                "detail": result.stderr.strip(),
            }), 500

        # 解析 query.py 的输出
        output = result.stdout.strip()
        files = []
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("/") and ".data" in line:
                # 去掉行首缩进和可能的列表符号
                path = line.lstrip()
                files.append(path)

        return jsonify({
            "count": len(files),
            "files": files,
            "start": start,
            "end": end or "now",
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "查询超时（30秒）"}), 504
    except FileNotFoundError:
        return jsonify({"error": "query.py 未找到"}), 500


@app.route("/api/flamegraph", methods=["GET"])
def api_flamegraph():
    """生成火焰图并返回 SVG。

    调用 flamegraph.py 生成火焰图，直接返回 SVG 内容。

    参数:
        file  (str): perf.data 文件路径（必填）
        width (int): SVG 宽度，默认 1200

    GET /api/flamegraph?file=/data/perf_raw/perf_20260619_030000.data
    GET /api/flamegraph?file=/data/perf_raw/perf_20260619_030000.data&width=1600
    """
    input_file = request.args.get("file", "").strip()
    width = request.args.get("width", "1200").strip()

    if not input_file:
        return jsonify({"error": "缺少参数: file"}), 400

    if not os.path.isfile(input_file):
        return jsonify({
            "error": f"文件不存在: {input_file}"
        }), 404

    # 验证宽度参数
    try:
        width_int = int(width)
        if width_int < 100 or width_int > 10000:
            return jsonify({"error": "width 必须在 100 ~ 10000 之间"}), 400
    except ValueError:
        return jsonify({"error": "width 必须是整数"}), 400

    # 构建 flamegraph.py 命令
    flamegraph_script = get_flamegraph_script()
    output_svg = os.path.join(
        config["svg_dir"],
        os.path.basename(input_file).replace(".data", ".svg").replace("perf_", "flame_"),
    )

    cmd = [
        sys.executable, flamegraph_script,
        "--input", input_file,
        "--output", output_svg,
        "--width", width,
        "--svg-dir", config["svg_dir"],
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 火焰图生成可能较慢，给 10 分钟
        )

        if result.returncode != 0:
            return jsonify({
                "error": "火焰图生成失败",
                "detail": result.stderr.strip() or result.stdout.strip(),
            }), 500

        # 读取生成的 SVG 文件
        if not os.path.isfile(output_svg):
            return jsonify({
                "error": "火焰图生成完成但未找到输出文件",
                "expected_path": output_svg,
            }), 500

        with open(output_svg, "r", encoding="utf-8") as f:
            svg_content = f.read()

        return Response(
            svg_content,
            mimetype="image/svg+xml",
            headers={
                "Content-Disposition": f'inline; filename="{os.path.basename(output_svg)}"',
            },
        )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "火焰图生成超时（600秒）"}), 504
    except FileNotFoundError:
        return jsonify({"error": "flamegraph.py 未找到"}), 500


# ======================================================================
# 全局错误处理
# ======================================================================

@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "接口不存在", "available": [
        "/  (Web 前端)",
        "/api/health",
        "/api/status",
        "/api/files",
        "/api/flamegraph",
    ]}), 404


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "服务器内部错误"}), 500


# ======================================================================
# 主入口
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="CPU Profiling 工具 HTTP API 服务",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_PORT,
        help=f"监听端口（默认: {DEFAULT_PORT}）",
    )
    parser.add_argument(
        "--data-dir", "-d",
        type=str,
        default=DEFAULT_DATA_DIR,
        help=f"perf 数据目录（默认: {DEFAULT_DATA_DIR}）",
    )
    parser.add_argument(
        "--svg-dir", "-s",
        type=str,
        default=DEFAULT_SVG_DIR,
        help=f"火焰图 SVG 输出目录（默认: {DEFAULT_SVG_DIR}）",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="监听地址（默认: 0.0.0.0）",
    )

    args = parser.parse_args()

    # 更新全局配置
    config["data_dir"] = args.data_dir
    config["svg_dir"] = args.svg_dir
    config["port"] = args.port

    # 确保目录存在
    os.makedirs(config["data_dir"], exist_ok=True)
    os.makedirs(config["svg_dir"], exist_ok=True)

    print("=" * 50)
    print("  CPU Profiling API 服务")
    print("=" * 50)
    print(f"  监听地址:    {args.host}:{args.port}")
    print(f"  数据目录:    {config['data_dir']}")
    print(f"  SVG 目录:    {config['svg_dir']}")
    print(f"  query.py:    {get_query_script()}")
    print(f"  flamegraph:  {get_flamegraph_script()}")
    print("=" * 50)
    print(f"  接口:")
    print(f"    GET /                     Web 前端")
    print(f"    GET /api/health           健康检查")
    print(f"    GET /api/status           系统概览")
    print(f"    GET /api/files?start=...&end=...  文件查询")
    print(f"    GET /api/flamegraph?file=...&width=...  火焰图生成")
    print("=" * 50)

    # 启动 Flask（生产环境建议配合 gunicorn，此处保持简单）
    app.run(
        host=args.host,
        port=args.port,
        debug=False,
    )


if __name__ == "__main__":
    main()
