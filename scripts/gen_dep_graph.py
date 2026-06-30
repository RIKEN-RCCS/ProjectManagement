"""
dependency_graph.svg ジェネレータ
scripts/ 配下の Python ファイルを解析して import 関係を SVG で可視化する。
"""
import ast
import sys
from collections import defaultdict
from pathlib import Path

SCRIPTS = Path("/lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts")
OUT_SVG = SCRIPTS / "dependency_graph.svg"

# ------------------------------------------------------------------ #
# 1. ファイル収集：正規パス（サブディレクトリ版）を使う
# ------------------------------------------------------------------ #
SKIP_DIRS = {"archive", "__pycache__", "static", "data"}

def collect_modules():
    """stem → relative_path のマッピングを返す。シンボリックリンクは除外。"""
    modules: dict[str, str] = {}  # stem → rel path
    for path in sorted(SCRIPTS.rglob("*.py")):
        if path.is_symlink():
            continue
        rel = path.relative_to(SCRIPTS)
        parts = rel.parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        if any(p.startswith("__") and p != "__init__.py" for p in parts):
            continue
        stem = path.stem
        if stem == "__init__":
            # パッケージ名をキーにする（例: argus/__init__.py → "argus"）
            stem = parts[0] if len(parts) == 2 else "/".join(parts[:-1]).replace("/", ".")
        rel_str = str(rel)
        modules[stem] = rel_str
    return modules


# ------------------------------------------------------------------ #
# 2. import 解析
# ------------------------------------------------------------------ #
STDLIB_PREFIXES = {
    "os", "sys", "re", "io", "ast", "abc", "copy", "json", "yaml",
    "math", "time", "uuid", "enum", "typing", "types", "pathlib",
    "logging", "argparse", "datetime", "functools", "itertools",
    "collections", "contextlib", "dataclasses", "threading", "asyncio",
    "concurrent", "subprocess", "tempfile", "shutil", "hashlib",
    "sqlite3", "csv", "textwrap", "unicodedata", "struct", "gzip",
    "zipfile", "base64", "urllib", "http", "socket", "ssl",
    "signal", "traceback", "inspect", "importlib", "pkgutil",
    "multiprocessing", "queue", "weakref", "array", "heapq", "bisect",
    "platform", "stat", "fcntl", "termios",
    # third-party (non-local)
    "fastapi", "pydantic", "starlette", "uvicorn",
    "slack_sdk", "openai", "anthropic",
    "numpy", "scipy", "sklearn", "torch", "transformers", "tokenizers",
    "PIL", "cv2", "moviepy",
    "boto3", "botocore", "requests", "httpx", "aiohttp",
    "sqlcipher3", "alembic",
    "sudachipy", "fugashi", "MeCab",
    "docx", "openpyxl", "pptx",
    "boxsdk", "boxsdk_jwt",
    "dotenv",
    "__future__",
}

def is_local(name: str, modules: dict[str, str]) -> bool:
    top = name.split(".")[0]
    return top in modules or top in {"argus", "patrol", "ingest", "enrich",
                                      "recording", "utils", "web", "reporting",
                                      "minutes", "quality", "data_pipeline"}

def resolve_import(name: str, modules: dict[str, str]) -> list[str]:
    """import name → 解決された stem リストを返す。"""
    parts = name.split(".")
    # 完全一致
    if name in modules:
        return [name]
    # stem だけ
    stem = parts[-1]
    if stem in modules:
        return [stem]
    # パッケージ (argus.patrol.state → state)
    for p in parts:
        if p in modules:
            return [p]
    return []

def parse_imports(filepath: Path, modules: dict[str, str]) -> set[str]:
    """filepath が依存する local モジュール stem のセット。トップレベル import のみ対象。"""
    deps: set[str] = set()
    try:
        src = filepath.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src, filename=str(filepath))
    except Exception:
        return deps

    # tree.body のみ（モジュールトップレベル）を走査し、関数内 lazy import を除外
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in STDLIB_PREFIXES:
                    continue
                for s in resolve_import(alias.name, modules):
                    deps.add(s)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            top = node.module.split(".")[0]
            if top in STDLIB_PREFIXES:
                continue
            for s in resolve_import(node.module, modules):
                deps.add(s)
        elif isinstance(node, (ast.If, ast.Try)):
            # TYPE_CHECKING ブロックや try/except import も対象に
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        top = alias.name.split(".")[0]
                        if top in STDLIB_PREFIXES:
                            continue
                        for s in resolve_import(alias.name, modules):
                            deps.add(s)
                elif isinstance(child, ast.ImportFrom):
                    if child.module is None:
                        continue
                    top = child.module.split(".")[0]
                    if top in STDLIB_PREFIXES:
                        continue
                    for s in resolve_import(child.module, modules):
                        deps.add(s)
    return deps


# ------------------------------------------------------------------ #
# 3. グラフ構築
# ------------------------------------------------------------------ #
def build_graph(modules: dict[str, str]):
    """edges: {src_stem: {dst_stem, ...}}"""
    edges: dict[str, set[str]] = defaultdict(set)
    for stem, rel in modules.items():
        path = SCRIPTS / rel
        deps = parse_imports(path, modules)
        for d in deps:
            if d in modules and d != stem:
                edges[stem].add(d)
    return edges


# ------------------------------------------------------------------ #
# 4. レイアウト：トポロジカルソートでレベル割り当て
# ------------------------------------------------------------------ #
def assign_levels(stems: list[str], edges: dict[str, set[str]]) -> dict[str, int]:
    """各ノードに「依存深さ」を割り当てる。依存先が多いほど右（高レベル）。"""
    in_degree: dict[str, int] = {s: 0 for s in stems}
    rev: dict[str, set[str]] = defaultdict(set)
    for src, dsts in edges.items():
        for dst in dsts:
            if dst in in_degree:
                in_degree[src] += 0  # 自分の依存先
                rev[dst].add(src)

    # レベル = 自分が依存しているノードの最大レベル + 1
    level: dict[str, int] = {s: 0 for s in stems}
    max_iters = len(stems) * 2
    for _ in range(max_iters):
        changed = False
        for src, dsts in edges.items():
            for dst in dsts:
                if dst in level and src in level:
                    new = level[dst] + 1
                    if new > level[src]:
                        level[src] = new
                        changed = True
        if not changed:
            break
    return level


# ------------------------------------------------------------------ #
# 5. グループ定義（サブディレクトリ別）
# ------------------------------------------------------------------ #
GROUP_COLORS = {
    "argus":         "#ffd0d0",
    "argus/patrol":  "#ffb0b0",
    "ingest":        "#d0e8ff",
    "data-pipeline": "#d0ffe8",
    "minutes":       "#fff0d0",
    "enrich":        "#e8d0ff",
    "recording":     "#d0f0ff",
    "quality":       "#ffe8d0",
    "reporting":     "#f0ffd0",
    "web":           "#f5d0ff",
    "utils":         "#f0f0f0",
    "bin":           "#e0e0e0",
    "root":          "#f5f5f5",
}

GROUP_LABELS = {
    "argus":         "argus (Slack Bot)",
    "argus/patrol":  "argus/patrol",
    "ingest":        "ingest",
    "data-pipeline": "data-pipeline",
    "minutes":       "minutes",
    "enrich":        "enrich",
    "recording":     "recording",
    "quality":       "quality",
    "reporting":     "reporting",
    "web":           "web (FastAPI)",
    "utils":         "utils",
    "bin":           "bin",
    "root":          "root (symlinks)",
}

def stem_to_group(stem: str, rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) == 1:
        return "root"
    pkg = parts[0]
    if pkg == "argus" and len(parts) >= 3 and parts[1] == "patrol":
        return "argus/patrol"
    return pkg


# ------------------------------------------------------------------ #
# 6. SVG 生成
# ------------------------------------------------------------------ #
NODE_W = 148
NODE_H = 20
ROW_GAP = 26
MARGIN_X = 16
MARGIN_Y = 16
GROUP_PAD = 8

# グループ列割り当て: 各 "band" は1列分、その中にグループを縦積み
# band index → グループリスト の順序
BAND_GROUPS = [
    ["argus/patrol", "argus"],          # band 0
    ["ingest", "data-pipeline"],        # band 1
    ["enrich", "minutes", "recording"], # band 2
    ["quality", "reporting"],           # band 3
    ["web", "bin", "root"],             # band 4
    ["utils"],                          # band 5
]
BAND_W = NODE_W + 20   # バンド幅

def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def generate_svg(modules: dict[str, str], edges: dict[str, set[str]]) -> str:
    # グループ別にノードを整理
    groups: dict[str, list[str]] = defaultdict(list)
    for stem, rel in modules.items():
        g = stem_to_group(stem, rel)
        groups[g].append(stem)
    for g in groups:
        groups[g].sort()

    # バンドごとにノード位置を計算
    node_pos: dict[str, tuple[int, int]] = {}
    group_rects: list[dict] = []
    band_heights: list[int] = []

    x = MARGIN_X
    for band in BAND_GROUPS:
        y = MARGIN_Y
        for g in band:
            if g not in groups or not groups[g]:
                continue
            nodes = groups[g]
            gy = y
            gx = x - GROUP_PAD
            gh = len(nodes) * ROW_GAP + GROUP_PAD * 2 + 14
            gw = NODE_W + GROUP_PAD * 2
            group_rects.append({
                "x": gx, "y": gy, "w": gw, "h": gh,
                "color": GROUP_COLORS.get(g, "#f5f5f5"),
                "label": GROUP_LABELS.get(g, g),
            })
            for i, stem in enumerate(nodes):
                nx = x
                ny = y + 16 + i * ROW_GAP
                node_pos[stem] = (nx, ny)
            y += gh + 14

        band_heights.append(y)
        x += BAND_W + 14

    # キャンバスサイズ
    if node_pos:
        max_x = max(px + NODE_W for px, _ in node_pos.values()) + MARGIN_X
        max_y = max(py + NODE_H for _, py in node_pos.values()) + MARGIN_Y
    else:
        max_x, max_y = 800, 600

    lines = []
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {max_x} {max_y}" font-family="monospace" font-size="10">')
    lines.append('<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#888"/></marker></defs>')
    lines.append(f'<rect width="{max_x}" height="{max_y}" fill="white"/>')
    lines.append('<style>'
                 '.ea{stroke:#bbb;stroke-width:0.8;fill:none;marker-end:url(#arrow)}'
                 '.nr{fill:white;stroke:#999;stroke-width:1}'
                 '.nt{fill:#333;text-anchor:middle;font-size:9px}'
                 '.gl{font-size:9px;font-weight:bold;fill:#555}'
                 '</style>')

    # グループ背景
    for gr in group_rects:
        lines.append(f'<rect x="{gr["x"]}" y="{gr["y"]}" width="{gr["w"]}" height="{gr["h"]}" fill="{gr["color"]}" rx="4" stroke="#ccc" stroke-width="1"/>')
        lines.append(f'<text x="{gr["x"] + 4}" y="{gr["y"] + 10}" class="gl">{escape(gr["label"])}</text>')

    # ノードボックス
    for stem, (nx, ny) in sorted(node_pos.items()):
        lines.append(f'<rect x="{nx}" y="{ny}" width="{NODE_W}" height="{NODE_H}" class="nr" rx="3"/>')
        lines.append(f'<text x="{nx + NODE_W//2}" y="{ny + 14}" class="nt">{escape(stem)}</text>')

    # エッジ（S字曲線）
    drawn: set[tuple[str, str]] = set()
    for src, dsts in sorted(edges.items()):
        if src not in node_pos:
            continue
        for dst in sorted(dsts):
            if dst not in node_pos:
                continue
            key = (src, dst)
            if key in drawn:
                continue
            drawn.add(key)
            sx, sy = node_pos[src]
            dx, dy = node_pos[dst]
            cy = sy + NODE_H // 2
            dy2 = dy + NODE_H // 2
            if sx < dx:
                x1, y1 = sx + NODE_W, cy
                x2, y2 = dx, dy2
            else:
                x1, y1 = sx, cy
                x2, y2 = dx + NODE_W, dy2
            mx = (x1 + x2) / 2
            lines.append(f'<path d="M {x1} {y1} C {mx} {y1},{mx} {y2},{x2} {y2}" class="ea"/>')

    lines.append('</svg>')
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# main
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    modules = collect_modules()
    print(f"Modules found: {len(modules)}", file=sys.stderr)
    for k, v in sorted(modules.items()):
        print(f"  {k:40s} {v}", file=sys.stderr)

    edges = build_graph(modules)
    print("\nEdges:", file=sys.stderr)
    for src in sorted(edges):
        for dst in sorted(edges[src]):
            print(f"  {src} -> {dst}", file=sys.stderr)

    svg = generate_svg(modules, edges)
    OUT_SVG.write_text(svg, encoding="utf-8")
    print(f"\nWrote {OUT_SVG}", file=sys.stderr)
