# -*- coding: utf-8 -*-
"""T-4 二维码回环测试（纯标准库 + 独立解码器）。

为什么不能只靠 ``qr.self_test()``：它只验证**自我一致性** —— 而本实现历史上
确实出现过「自检全绿、真机扫不出」（格式信息位序写反，见 qr.py 说明）。
自己证明不了自己可扫，所以这里自带一个**最小独立解码器**：

  1. 直接解析 PNG（zlib 解压 + 反滤波），从像素读回模块矩阵；
  2. 独立校验结构：模块块内一致、静默区全白、定位/时序图形、格式信息 BCH、
     版本信息 BCH（v>=7）；
  3. 读格式信息得到 EC 等级与掩码 -> 解掩码 -> 按标准之字形读回码字；
  4. 按标准分块表解交织、剥掉模式头与纠错码字，还原 UTF-8 原文。

解码器只用标准库，**不 import qr.py 的任何内部函数**，分块表与对齐图形表
按 QR 标准独立写死。K3 上若存在 cv2，再叠一层 ``cv2.QRCodeDetector``
（第三方实现）交叉验证。

运行：``python3 app/tests/test_qr.py``（也可 ``python3 -m pytest``，但不需要）。
"""

from __future__ import annotations

import io
import os
import struct
import sys
import zlib

try:  # K3 控制台是 UTF-8；Windows 控制台是 GBK —— 中文输出不能把测试挡在门外
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "render"))

import qr  # noqa: E402


# ==========================================================================
# 独立最小解码器
# ==========================================================================


class DecodeError(Exception):
    pass


# ---- 标准表（独立于 qr.py，按 QR 标准手写） -------------------------------

# version -> (每块纠错码字数, [每块数据码字数, ...])
STANDARD_BLOCKS_M = {
    1: (10, [16]),
    2: (16, [28]),
    3: (26, [44]),
    4: (18, [32, 32]),
    5: (24, [43, 43]),
    6: (16, [27, 27, 27, 27]),
    7: (18, [31, 31, 31, 31]),
    8: (22, [38, 38, 39, 39]),
    9: (22, [36, 36, 36, 37, 37]),
    10: (26, [43, 43, 43, 43, 44]),
}

# version -> 总码字数（标准值）
STANDARD_TOTAL = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134,
                  6: 172, 7: 196, 8: 242, 9: 292, 10: 346}

# version -> 对齐图形中心（标准表）
STANDARD_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}

EC_BITS_TO_NAME = {0b01: "L", 0b00: "M", 0b11: "Q", 0b10: "H"}


def _bch_format(bits5: int) -> int:
    """15 位格式信息：5 位数据 + BCH(15,5)，最后异或 0x5412。"""
    poly = 0b10100110111
    value = bits5 << 10
    for i in range(4, -1, -1):
        if value & (1 << (i + 10)):
            value ^= poly << i
    return ((bits5 << 10) | value) ^ 0b101010000010010


FORMAT_TABLE = {
    _bch_format((ec << 3) | mask): (EC_BITS_TO_NAME[ec], mask)
    for ec in (0b01, 0b00, 0b11, 0b10)
    for mask in range(8)
}


def _bch_version(version: int) -> int:
    poly = 0b1111100100101
    value = version << 12
    for i in range(5, -1, -1):
        if value & (1 << (i + 12)):
            value ^= poly << i
    return (version << 12) | value


MASK_FUNCS = (
    lambda y, x: (y + x) % 2 == 0,
    lambda y, x: y % 2 == 0,
    lambda y, x: x % 3 == 0,
    lambda y, x: (y + x) % 3 == 0,
    lambda y, x: (y // 2 + x // 3) % 2 == 0,
    lambda y, x: (y * x) % 2 + (y * x) % 3 == 0,
    lambda y, x: ((y * x) % 2 + (y * x) % 3) % 2 == 0,
    lambda y, x: ((y + x) % 2 + (y * x) % 3) % 2 == 0,
)


# ---- PNG 读取（标准库实现，不用 PIL） -------------------------------------


def png_to_luma(png: bytes) -> tuple[list[list[int]], int, int]:
    """PNG 字节 -> (亮度二维数组, width, height)。支持 8bit 灰度/RGB/RGBA，无隔行。"""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise DecodeError("不是 PNG（签名不对）")
    pos = 8
    header = None
    idat = bytearray()
    while pos + 8 <= len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        ctype = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            header = struct.unpack(">IIBBBBB", data)
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
    if header is None:
        raise DecodeError("缺 IHDR")
    width, height, depth, color, _comp, _filt, interlace = header
    if depth != 8 or interlace != 0:
        raise DecodeError(f"只支持 8bit 非隔行 PNG（depth={depth} interlace={interlace}）")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise DecodeError(f"不支持的 PNG 颜色类型 {color}")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        raise DecodeError("IDAT 数据不完整")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise DecodeError(f"未知 PNG 滤波类型 {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    grid: list[list[int]] = []
    for y in range(height):
        row = []
        base = y * stride
        for x in range(width):
            o = base + x * channels
            if channels == 1:
                row.append(out[o])
            else:
                row.append((out[o] * 299 + out[o + 1] * 587 + out[o + 2] * 114) // 1000)
        grid.append(row)
    return grid, width, height


# ---- 结构校验 + 模块提取 --------------------------------------------------


def _finder_ok(grid, r0: int, c0: int) -> bool:
    expected = (
        (1, 1, 1, 1, 1, 1, 1),
        (1, 0, 0, 0, 0, 0, 1),
        (1, 0, 1, 1, 1, 0, 1),
        (1, 0, 1, 1, 1, 0, 1),
        (1, 0, 1, 1, 1, 0, 1),
        (1, 0, 0, 0, 0, 0, 1),
        (1, 1, 1, 1, 1, 1, 1),
    )
    return all(grid[r0 + y][c0 + x] == expected[y][x] for y in range(7) for x in range(7))


def extract_modules(luma, width, height, border: int = 4):
    """像素亮度 -> (模块矩阵, version)。要求静默区为白、模块块内颜色一致。"""
    if width != height:
        raise DecodeError(f"二维码应为正方形，实际 {width}x{height}")
    for version in sorted(STANDARD_BLOCKS_M):
        size = version * 4 + 17
        unit = size + 2 * border
        if width % unit:
            continue
        scale = width // unit
        if scale < 1:
            continue
        # 静默区必须全白
        edge = border * scale
        quiet_ok = True
        for i in range(width):
            for j in list(range(edge)) + list(range(width - edge, width)):
                if luma[j][i] < 128 or luma[i][j] < 128:
                    quiet_ok = False
                    break
            if not quiet_ok:
                break
        if not quiet_ok:
            continue
        grid = [[0] * size for _ in range(size)]
        uniform = True
        for my in range(size):
            for mx in range(size):
                y0 = (my + border) * scale
                x0 = (mx + border) * scale
                first = 1 if luma[y0][x0] < 128 else 0
                for dy in range(scale):
                    row = luma[y0 + dy]
                    for dx in range(scale):
                        if (1 if row[x0 + dx] < 128 else 0) != first:
                            uniform = False
                            break
                    if not uniform:
                        break
                if not uniform:
                    break
                grid[my][mx] = first
            if not uniform:
                break
        if not uniform:
            continue
        if not (_finder_ok(grid, 0, 0) and _finder_ok(grid, 0, size - 7)
                and _finder_ok(grid, size - 7, 0)):
            continue
        if not all(grid[6][i] == (1 if i % 2 == 0 else 0) for i in range(8, size - 8)):
            continue
        if not all(grid[i][6] == (1 if i % 2 == 0 else 0) for i in range(8, size - 8)):
            continue
        if grid[size - 8][8] != 1:
            continue
        return grid, version
    raise DecodeError(f"像素网格不匹配任何版本 1-10（尺寸 {width}，border={border}）")


# ---- 功能图形占位（独立按标准规则推导） -----------------------------------


def function_mask(version: int) -> list[list[bool]]:
    size = version * 4 + 17
    fn = [[False] * size for _ in range(size)]

    def reserve(cells):
        for y, x in cells:
            if 0 <= y < size and 0 <= x < size:
                fn[y][x] = True

    # 定位图形 + 分隔符：三个 8x8 角区
    for r in range(8):
        for c in range(8):
            reserve(((r, c), (r, size - 1 - c), (size - 1 - r, c)))
    # 时序列
    reserve(((6, i) for i in range(size)))
    reserve(((i, 6) for i in range(size)))
    # 校正图形（与定位图形重叠者不放置）
    centers = STANDARD_ALIGN[version]
    for cy in centers:
        for cx in centers:
            if (cy <= 8 and cx <= 8) or (cy <= 8 and cx >= size - 9) or (cy >= size - 9 and cx <= 8):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    reserve(((cy + dy, cx + dx),))
    # 格式信息两份 + 固定暗模块
    reserve(((8, i) for i in range(9)))
    reserve(((i, 8) for i in range(9)))
    reserve(((8, size - 1 - i) for i in range(8)))
    reserve(((size - 1 - i, 8) for i in range(8)))
    reserve(((size - 8, 8),))
    # 版本信息（v>=7）两个 3x6 块
    if version >= 7:
        for i in range(18):
            reserve(((size - 11 + i % 3, i // 3), (i // 3, size - 11 + i % 3)))
    return fn


# ---- 码字读取与还原 -------------------------------------------------------


def read_format_info(grid):
    size = len(grid)
    copy1 = 0
    for i in range(6):
        copy1 |= grid[8][i] << (14 - i)
    copy1 |= grid[8][7] << 8
    copy1 |= grid[8][8] << 7
    copy1 |= grid[7][8] << 6
    for j in range(6):
        copy1 |= grid[j][8] << j

    copy2 = 0
    for i in range(7):
        copy2 |= grid[size - 1 - i][8] << (14 - i)
    copy2 |= grid[8][size - 8] << 7
    for i in range(7):
        copy2 |= grid[8][size - 1 - i] << i

    if copy1 != copy2:
        raise DecodeError(f"格式信息两份副本不一致：{copy1:015b} vs {copy2:015b}")
    if copy1 not in FORMAT_TABLE:
        raise DecodeError(f"格式信息不是合法 BCH 码字：{copy1:015b}")
    return FORMAT_TABLE[copy1]


def read_version_info(grid, version: int) -> int:
    if version < 7:
        return -1
    size = len(grid)
    value = 0
    for i in range(18):
        value |= grid[size - 11 + i % 3][i // 3] << i
    if value != _bch_version(version):
        raise DecodeError(f"版本信息不是合法 BCH 码字：{value:018b}（期望 {_bch_version(version):018b}）")
    return value


def decode_matrix(grid, version: int) -> str:
    ec_name, mask = read_format_info(grid)
    if ec_name != "M":
        raise DecodeError(f"EC 等级不是 M 而是 {ec_name}")
    read_version_info(grid, version)

    size = len(grid)
    fn = function_mask(version)
    fncount = sum(1 for y in range(size) for x in range(size) if fn[y][x])
    total = STANDARD_TOTAL[version]
    expected_free = total * 8 + {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7,
                                 7: 0, 8: 0, 9: 0, 10: 0}[version]
    if size * size - fncount != expected_free:
        raise DecodeError(f"功能图形占位格数不符：{size * size - fncount} != {expected_free}")

    mask_fn = MASK_FUNCS[mask]
    bits: list[int] = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not fn[row][c]:
                    bit = grid[row][c]
                    if mask_fn(row, c):
                        bit ^= 1
                    bits.append(bit)
        upward = not upward
        col -= 2

    if len(bits) < total * 8:
        raise DecodeError(f"可用数据位不足：{len(bits)} < {total * 8}")
    codewords = [
        int("".join(str(b) for b in bits[i:i + 8]), 2)
        for i in range(0, total * 8, 8)
    ]

    _ec_len, data_lens = STANDARD_BLOCKS_M[version]
    blocks: list[list[int]] = [[] for _ in data_lens]
    index = 0
    for i in range(max(data_lens)):
        for b, dlen in enumerate(data_lens):
            if i < dlen:
                blocks[b].append(codewords[index])
                index += 1

    payload_bits: list[int] = []
    for block in blocks:
        for cw in block:
            for i in range(7, -1, -1):
                payload_bits.append((cw >> i) & 1)

    def take(n: int) -> int:
        nonlocal payload_bits
        value = 0
        for _ in range(n):
            value = (value << 1) | payload_bits.pop(0)
        return value

    mode = take(4)
    if mode != 0b0100:
        raise DecodeError(f"模式不是字节模式（0b{mode:04b}）")
    length = take(8 if version <= 9 else 16)
    data = bytes(take(8) for _ in range(length))
    return data.decode("utf-8")


def decode_png(png: bytes, border: int = 4) -> str:
    """独立解码：PNG 字节 -> 原文。"""
    luma, width, height = png_to_luma(png)
    grid, version = extract_modules(luma, width, height, border=border)
    return decode_matrix(grid, version)


def decode_png_with_version(png: bytes, border: int = 4):
    luma, width, height = png_to_luma(png)
    grid, version = extract_modules(luma, width, height, border=border)
    return decode_matrix(grid, version), version


# ==========================================================================
# 测试脚手架
# ==========================================================================

CHECKS = 0
FAILURES: list[str] = []
SKIPS: list[str] = []


def check(cond, name: str, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    ok = bool(cond)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def skip(name: str, detail: str = "") -> None:
    SKIPS.append(name)
    print(f"  [SKIP] {name}" + (f" | {detail}" if detail else ""))


def raises(exc_type, fn):
    try:
        fn()
    except exc_type as err:
        return err
    except Exception as err:  # noqa: BLE001
        return f"WRONG:{type(err).__name__}:{err}"
    return None


CASES = [
    ("短链（现场主用例）", "http://192.0.2.10:8090/p/PH-0123"),
    ("备用网段短链", "http://192.0.2.10:8090/p/PH-0001"),
    ("域名短链", "https://pick.example.com/p/PH-2048"),
    ("单字符", "A"),
    ("空串", ""),
    ("中文 + 话题标签", "#SpacemiTDevCon 赛博开发者证件照"),
    ("中文长句", "进迭时空 K3 · 开发者证件照 · 现场取图二维码（中文往返）"),
    ("120 字节（逼近 v7，覆盖版本信息块）", "x" * 120),
    ("URL + 长 query", "http://192.0.2.10:8090/p/PH-0001?src=devcon&ref=qr&pad=" + "z" * 58),
    ("213 字节（v10-M 字节模式容量上界）", "y" * 213),
]


def main() -> int:
    print("=== T-4 二维码：结构自检（qr.self_test） ===")
    report = qr.self_test()
    if not isinstance(report, dict):
        check(False, "self_test() 返回 dict", f"实际 {type(report).__name__}")
    else:
        for key, value in report.items():
            if isinstance(value, bool):
                check(value is True, f"self_test.{key}")
            else:
                print(f"  [info] self_test.{key} = {value}")
        check(report.get("ok") is True, "self_test() 整体 ok")

    print("\n=== T-4 独立解码回环（纯标准库解码器，从 PNG 像素读回原文） ===")
    for label, text in CASES:
        try:
            png = qr.make_qr_png(text)
            decoded, version = decode_png_with_version(png)
            check(decoded == text, f"回环 v{version} {label}",
                  f"{len(text)} 字符 / {len(png)} B -> {decoded[:36]!r}")
        except Exception as err:  # noqa: BLE001
            check(False, f"回环 {label}", f"{type(err).__name__}: {err}")

    print("\n=== T-4 PNG 渲染参数（scale / border） ===")
    text = "http://192.0.2.10:8090/p/PH-0123"
    matrix = qr.encode_matrix(text)
    for scale in (3, 8, 10):
        png = qr.to_png_bytes(matrix, scale=scale, border=4)
        size = len(matrix)
        try:
            decoded, version = decode_png_with_version(png)
            dim_ok = len(png_to_luma(png)[0]) == (size + 8) * scale
            check(decoded == text and dim_ok, f"scale={scale} 回环 + 尺寸",
                  f"{len(png)} B, 期望边长 {(size + 8) * scale}")
        except Exception as err:  # noqa: BLE001
            check(False, f"scale={scale} 回环", f"{type(err).__name__}: {err}")
    try:
        png = qr.to_png_bytes(matrix, scale=6, border=2)
        decoded = decode_png(png, border=2)
        check(decoded == text, "border=2 回环（静默区可变）")
    except Exception as err:  # noqa: BLE001
        check(False, "border=2 回环", f"{type(err).__name__}: {err}")

    print("\n=== T-4 SVG 输出 ===")
    svg = qr.to_svg(matrix, border=4, module=4)
    size = len(matrix)
    dim = (size + 8) * 4
    check(isinstance(svg, str) and svg.startswith("<svg"), "SVG 根元素")
    check(f'width="{dim}"' in svg and f'height="{dim}"' in svg, "SVG 尺寸随矩阵",
          f"{dim}x{dim}")
    check("<path" in svg and svg.endswith("</svg>"), "SVG 含路径且闭合")

    print("\n=== T-4 EC 等级边界 ===")
    check(raises(Exception, lambda: qr.encode_matrix("x", "M")) is None, "EC=M 可用")
    for bad in ("L", "Q", "H", "m", ""):
        err = raises(qr.QRError, lambda level=bad: qr.encode_matrix("x", level))
        check(isinstance(err, qr.QRError), f"EC={bad!r} 抛 QRError", str(err)[:60])
    check(issubclass(qr.QRError, ValueError), "QRError 是 ValueError 子类")
    edge = raises(qr.QRError, lambda: qr.encode_matrix("x" * 214))
    check(isinstance(edge, qr.QRError), "214 字节超长抛 QRError", str(edge)[:70])
    edge_cn = raises(qr.QRError, lambda: qr.encode_matrix("汉" * 72))
    check(isinstance(edge_cn, qr.QRError), "中文 216 字节超长抛 QRError", str(edge_cn)[:70])
    ok_cn = raises(qr.QRError, lambda: qr.encode_matrix("汉" * 71))
    check(ok_cn is None, "中文 213 字节（正好上界）可编码")
    if ok_cn is None:
        try:
            decoded = decode_png(qr.make_qr_png("汉" * 71))
            check(decoded == "汉" * 71, "中文 213 字节回环")
        except Exception as err:  # noqa: BLE001
            check(False, "中文 213 字节回环", f"{type(err).__name__}: {err}")

    print("\n=== T-4 第三方解码器交叉验证（cv2，可选） ===")
    try:
        import numpy as np
        from PIL import Image
        import cv2
    except Exception as err:  # noqa: BLE001
        skip("cv2 交叉验证", f"本机没有 cv2/PIL/numpy：{type(err).__name__}")
    else:
        detector = cv2.QRCodeDetector()
        bad = 0
        for label, text in CASES:
            if not text:
                continue
            png = qr.make_qr_png(text)
            arr = np.array(Image.open(io.BytesIO(png)).convert("L"))
            decoded, _pts, _ = detector.detectAndDecode(arr)
            if decoded != text:
                bad += 1
                check(False, f"cv2 解码 {label}", f"解出 {decoded[:40]!r}")
        check(bad == 0, "cv2.QRCodeDetector 全量解码一致",
              f"{len(CASES) - 1} 个用例")

    print("\n=== T-4 汇总 ===")
    print(f"  检查项 {CHECKS} 个，失败 {len(FAILURES)} 个，跳过 {len(SKIPS)} 个")
    for name in FAILURES:
        print(f"  - FAILED: {name}")
    if FAILURES:
        print("QR TEST: FAIL")
        return 1
    print("QR TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())