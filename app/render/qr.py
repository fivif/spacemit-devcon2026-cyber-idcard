# -*- coding: utf-8 -*-
"""纯 Python 二维码编码器（字节模式，EC 等级 M，版本 1–10）。

为什么要自己写：现场**必须离线**，不能引 CDN 上的 JS 库；K3 上也不适合再装
第三方包。这个实现只用标准库，任何地方都能跑。

能力范围（够用即可，不做全版本）:
    版本 1–10，纠错等级 M，字节模式 → 最多 **213** 字节。
    取图 URL 形如 ``http://192.0.2.10:8090/p/PH-0123``（约 30 字节），余量充足。

    字节容量 = (数据码字数 × 8 − 模式头位数) ÷ 8，向下取整：
    v1→14, v2→26, v3→42, v4→62, v5→84, v6→106, v7→122, v8→152, v9→180, v10→213。

验证状态:
    - 已用独立解码器（OpenCV QRCodeDetector）做**真解码回环**，覆盖 v1/v3/v7
      与多种长度，全部通过。
    - ``self_test()`` 校验结构不变量，并对照标准规定的 (M,0) = 0x5412 这一
      **外部真值**，而不是只做自我一致性。
    - 仍未做的：真手机扫码（彩排必做，见 docs/03-现场SOP与彩排.md）。
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# 规格表（版本 1–10，EC 等级 M）
# --------------------------------------------------------------------------

# version -> (每块纠错码字数, [(块数, 每块数据码字数), ...])
EC_BLOCKS_M: dict[int, tuple[int, list[tuple[int, int]]]] = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}

# version -> 对齐图形中心坐标（版本 1 没有对齐图形）
ALIGN_CENTERS: dict[int, list[int]] = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

# version -> 总码字数（由上面的表推导，用来自检）
TOTAL_CODEWORDS = {v: ec * sum(n for n, _ in groups) + sum(n * d for n, d in groups)
                   for v, (ec, groups) in EC_BLOCKS_M.items()}

EC_LEVEL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}


class QRError(ValueError):
    pass


# --------------------------------------------------------------------------
# GF(256) 与 Reed–Solomon
# --------------------------------------------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D  # 本原多项式 x^8+x^4+x^3+x^2+1
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n: int) -> list[int]:
    poly = [1]
    for i in range(n):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c
            nxt[j + 1] ^= _gf_mul(c, _EXP[i])
        poly = nxt
    return poly


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator(ec_len)
    res = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            res[i + j] ^= _gf_mul(g, coef)
    return res[len(data):]


# --------------------------------------------------------------------------
# 位流构造
# --------------------------------------------------------------------------


def byte_capacity(version: int) -> int:
    """该版本字节模式的实际可编码字节数。"""
    data_codewords = sum(n * d for n, d in EC_BLOCKS_M[version][1])
    header = 4 + (8 if version <= 9 else 16)
    return (data_codewords * 8 - header) // 8


def _pick_version(nbytes: int) -> int:
    for v in sorted(EC_BLOCKS_M):
        if nbytes <= byte_capacity(v):
            return v
    raise QRError(f"数据过长（{nbytes} 字节）；本实现上限 {byte_capacity(10)} 字节（版本 10-M 字节模式）")


def _build_codewords(data: bytes, version: int) -> list[int]:
    ec_len, groups = EC_BLOCKS_M[version]
    total_data = sum(n * d for n, d in groups)

    bits: list[int] = []

    def push(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                       # 字节模式
    push(len(data), 8 if version <= 9 else 16)
    for byte in data:
        push(byte, 8)

    # 终止符 + 补齐到字节边界
    push(0, min(4, total_data * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    i = 0
    while len(codewords) < total_data:
        codewords.append(pad[i % 2])
        i += 1

    # 分块 → RS → 交织
    blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for count, dlen in groups:
        for _ in range(count):
            chunk = codewords[pos:pos + dlen]
            pos += dlen
            blocks.append(chunk)
            ec_blocks.append(_rs_encode(chunk, ec_len))

    out: list[int] = []
    max_dlen = max(len(b) for b in blocks)
    for i in range(max_dlen):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_len):
        for b in ec_blocks:
            out.append(b[i])
    return out


# --------------------------------------------------------------------------
# 矩阵构造
# --------------------------------------------------------------------------


def _new_matrix(size: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    m: list[list[int | None]] = [[None] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]
    return m, reserved


def _place_finder(m, res, row: int, col: int) -> None:
    for dy in range(-1, 8):
        for dx in range(-1, 8):
            y, x = row + dy, col + dx
            if not (0 <= y < len(m) and 0 <= x < len(m)):
                continue
            res[y][x] = True
            if not (0 <= dy <= 6 and 0 <= dx <= 6):
                m[y][x] = 0  # 分隔符：固定白，不承载数据
                continue
            edge = dy in (0, 6) or dx in (0, 6)
            inner = 2 <= dy <= 4 and 2 <= dx <= 4
            m[y][x] = 1 if (edge or inner) else 0


def _place_alignment(m, res, version: int) -> None:
    size = len(m)
    centers = ALIGN_CENTERS[version]
    for cy in centers:
        for cx in centers:
            # 与定位图形重叠处跳过
            if (cy <= 8 and cx <= 8) or (cy <= 8 and cx >= size - 9) or (cy >= size - 9 and cx <= 8):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    y, x = cy + dy, cx + dx
                    res[y][x] = True
                    m[y][x] = 1 if (max(abs(dy), abs(dx)) != 1) else 0


def _place_timing(m, res) -> None:
    size = len(m)
    for i in range(8, size - 8):
        value = 1 if i % 2 == 0 else 0
        for y, x in ((6, i), (i, 6)):
            if m[y][x] is None:
                m[y][x] = value
                res[y][x] = True


def _reserve(m, res, cells) -> None:
    """占位：既标记保留（掩码不碰），也写入占位值（数据填充跳过）。

    ⚠️ 这里必须**同时**写 m —— 只标 res 的话，`_place_data` 看到的仍是 None，
    会把数据位流写进格式/版本信息格并消耗比特，后面所有模块整体错位。
    这是本实现早期最隐蔽的一个 bug。
    """
    size = len(m)
    for y, x in cells:
        if 0 <= y < size and 0 <= x < size:
            res[y][x] = True
            if m[y][x] is None:
                m[y][x] = 0


def _reserve_format(m, res) -> None:
    size = len(m)
    cells = [(8, i) for i in range(9)] + [(i, 8) for i in range(9)]
    cells += [(8, size - 1 - i) for i in range(8)]
    cells += [(size - 1 - i, 8) for i in range(8)]
    cells.append((size - 8, 8))  # 固定暗模块
    _reserve(m, res, cells)


def _reserve_version(m, res, version: int) -> None:
    """版本 ≥ 7 的两个 3×6 版本信息块必须在填数据之前占位。"""
    if version < 7:
        return
    size = len(m)
    cells = []
    for i in range(18):
        cells.append((size - 11 + i % 3, i // 3))
        cells.append((i // 3, size - 11 + i % 3))
    _reserve(m, res, cells)


def _place_data(m, res, codewords: list[int]) -> None:
    size = len(m)
    bits: list[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # 跳过时序列
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                # 判据必须是**保留图 res**，不能是 `m is None`：
                # 定位图形的分隔符（每个 15 格 × 3 个 = 45 格）只标了 res、
                # 没有值，用 `m is None` 会把 45 个数据比特写进分隔符，
                # 导致其后所有模块整体错位。这是本实现最隐蔽的一个 bug。
                if not res[row][c]:
                    m[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


def _mask_fn(pattern: int):
    return {
        0: lambda y, x: (y + x) % 2 == 0,
        1: lambda y, x: y % 2 == 0,
        2: lambda y, x: x % 3 == 0,
        3: lambda y, x: (y + x) % 3 == 0,
        4: lambda y, x: (y // 2 + x // 3) % 2 == 0,
        5: lambda y, x: (y * x) % 2 + (y * x) % 3 == 0,
        6: lambda y, x: ((y * x) % 2 + (y * x) % 3) % 2 == 0,
        7: lambda y, x: ((y + x) % 2 + (y * x) % 3) % 2 == 0,
    }[pattern]


def _apply_mask(m, res, pattern: int) -> list[list[int]]:
    fn = _mask_fn(pattern)
    out = [[0] * len(m) for _ in range(len(m))]
    for y in range(len(m)):
        for x in range(len(m)):
            v = m[y][x] or 0
            out[y][x] = (v ^ 1) if (not res[y][x] and fn(y, x)) else v
    return out


# -- 格式信息 ---------------------------------------------------------------

_FORMAT_GEN = 0b10100110111
_FORMAT_MASK = 0b101010000010010


def _format_bits(ec_level: str, mask: int) -> int:
    data = (EC_LEVEL_BITS[ec_level] << 3) | mask
    value = data << 10
    for i in range(4, -1, -1):
        if value & (1 << (i + 10)):
            value ^= _FORMAT_GEN << i
    return ((data << 10) | value) ^ _FORMAT_MASK


def _place_format(m: list[list[int]], ec_level: str, mask: int) -> None:
    """放置 15 位格式信息（两份副本）。

    ⚠️ **位序陷阱**：格式信息是 **MSB 在先** —— bit14 在 (8,0)，
    沿第 8 行向右，再沿第 8 列向下，bit0 落在 (0,8)。
    版本信息（v≥7）则相反，是 **LSB 在先**。两处约定不同，写反了
    会出现「数据全都对、就是扫不出来」——因为真解码器读到的掩码号
    与你实际施加的掩码不一致。此约定由已知可解的参考矩阵反推确认，
    不是凭记忆。
    """
    size = len(m)
    bits = _format_bits(ec_level, mask)

    def bit(i: int) -> int:
        return (bits >> i) & 1

    # 副本 1：左上
    for i in range(6):
        m[8][i] = bit(14 - i)
    m[8][7] = bit(8)
    m[8][8] = bit(7)
    m[7][8] = bit(6)
    for j in range(6):
        m[j][8] = bit(j)

    # 副本 2：左下 + 右上
    for i in range(7):
        m[size - 1 - i][8] = bit(14 - i)
    m[8][size - 8] = bit(7)
    for i in range(7):
        m[8][size - 1 - i] = bit(i)
    m[size - 8][8] = 1  # 暗模块


# -- 版本信息（版本 ≥ 7） ---------------------------------------------------

_VERSION_GEN = 0b1111100100101


def _version_bits(version: int) -> int:
    value = version << 12
    for i in range(5, -1, -1):
        if value & (1 << (i + 12)):
            value ^= _VERSION_GEN << i
    return (version << 12) | value


def _place_version(m: list[list[int]], version: int) -> None:
    if version < 7:
        return
    size = len(m)
    bits = _version_bits(version)
    for i in range(18):
        b = (bits >> i) & 1
        m[size - 11 + i % 3][i // 3] = b
        m[i // 3][size - 11 + i % 3] = b


# -- 掩码选择（罚分） -------------------------------------------------------


def _penalty(grid: list[list[int]]) -> int:
    size = len(grid)
    score = 0

    # 规则 1：同色连续 5 个以上
    for line in list(grid) + [list(col) for col in zip(*grid)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # 规则 2：2×2 同色块
    for y in range(size - 1):
        for x in range(size - 1):
            v = grid[y][x]
            if v == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                score += 3

    # 规则 3：类定位图形序列
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(grid) + [list(col) for col in zip(*grid)]:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40

    # 规则 4：暗模块比例偏离 50%
    dark = sum(sum(row) for row in grid)
    ratio = dark * 100.0 / (size * size)
    score += int(abs(ratio - 50) / 5) * 10
    return score


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------


def encode_matrix(text: str, ec_level: str = "M") -> list[list[int]]:
    """文本 → 0/1 矩阵（含定位/时序/对齐/格式/版本信息与数据）。"""
    if ec_level != "M":
        raise QRError("本实现只支持 EC 等级 M")
    data = text.encode("utf-8")
    version = _pick_version(len(data))
    size = version * 4 + 17
    codewords = _build_codewords(data, version)

    m, res = _new_matrix(size)
    _place_finder(m, res, 0, 0)
    _place_finder(m, res, 0, size - 7)
    _place_finder(m, res, size - 7, 0)
    _place_alignment(m, res, version)
    _place_timing(m, res)
    _reserve_format(m, res)
    _reserve_version(m, res, version)
    _place_data(m, res, codewords)
    _place_version(m, version)

    best: list[list[int]] | None = None
    best_score = None
    for pattern in range(8):
        candidate = _apply_mask(m, res, pattern)
        _place_format(candidate, ec_level, pattern)
        _place_version(candidate, version)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    assert best is not None
    return best


def to_png_bytes(matrix: list[list[int]], scale: int = 8, border: int = 4) -> bytes:
    """0/1 矩阵 → PNG 字节（黑白，带静默区）。"""
    import io
    from PIL import Image

    size = len(matrix)
    dim = (size + border * 2) * scale
    img = Image.new("L", (dim, dim), 255)
    px = img.load()
    for y in range(size):
        for x in range(size):
            if not matrix[y][x]:
                continue
            for dy in range(scale):
                for dx in range(scale):
                    px[(x + border) * scale + dx, (y + border) * scale + dy] = 0
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def to_svg(matrix: list[list[int]], border: int = 4, module: int = 4) -> str:
    """0/1 矩阵 → SVG（印刷物料放大不糊）。"""
    size = len(matrix)
    dim = (size + border * 2) * module
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" '
        f'viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">',
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>',
        '<path fill="#000000" d="',
    ]
    for y in range(size):
        x = 0
        while x < size:
            if matrix[y][x]:
                run = 1
                while x + run < size and matrix[y][x + run]:
                    run += 1
                parts.append(f"M{(x + border) * module} {(y + border) * module}h{run * module}v{module}h{-run * module}z")
                x += run
            else:
                x += 1
    parts.append('"/></svg>')
    return "".join(parts)


# --------------------------------------------------------------------------
# 便捷封装（T-4：本文件是早期原型 qr.py 的原样副本，仅新增此函数）
# --------------------------------------------------------------------------


def make_qr_png(text: str, scale: int = 8) -> bytes:
    """文本 -> 二维码 PNG 字节（编码 + 渲染一步到位）。

    取图短链直接调它：``make_qr_png("http://192.0.2.10:8090/p/PH-0123")``。
    静默区固定 4 模块（标准要求 >= 4，少了会影响识别）。
    """
    return to_png_bytes(encode_matrix(text), scale=scale, border=4)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------


def self_test() -> dict:
    """结构不变量自检。

    注意：**这不能证明二维码可被扫出**。它验证的是矩阵构造层面的一致性
    （尺寸、定位图形、时序、暗模块、格式信息能被独立回读）。
    真机扫码必须在彩排时做一次。
    """
    results: dict[str, object] = {}

    # 表自洽：总码字数 == 数据 + 纠错
    table_ok = True
    for v, (ec, groups) in EC_BLOCKS_M.items():
        blocks = sum(n for n, _ in groups)
        total = ec * blocks + sum(n * d for n, d in groups)
        if total != TOTAL_CODEWORDS[v]:
            table_ok = False
    results["codeword_table_consistent"] = table_ok

    # 已知总码字数（QR 标准的 version 1–10 总码字数）
    expected_total = [26, 44, 70, 100, 134, 172, 196, 242, 292, 346]
    results["codeword_totals_match_standard"] = all(
        TOTAL_CODEWORDS[v] == expected_total[v - 1] for v in range(1, 11)
    )

    # 字节容量对照标准表：v1-M=14, v2-M=26, v3-M=42, v4-M=62, v5-M=84,
    # v6-M=106, v7-M=122, v8-M=152, v9-M=180, v10-M=213
    expected_capacity = [14, 26, 42, 62, 84, 106, 122, 152, 180, 213]
    results["byte_capacity_matches_standard"] = all(
        byte_capacity(v) == expected_capacity[v - 1] for v in range(1, 11)
    )

    # 外部真值：标准规定 EC 等级 M、掩码 0 的格式信息为 0x5412
    results["format_M0_is_standard_value"] = _format_bits("M", 0) == 0x5412
    # 外部真值：版本 7 的版本信息为 0x07C94
    results["version_info_v7_is_standard_value"] = _version_bits(7) == 0x07C94

    # 功能图形占位必须恰好留下 8×总码字 + 余位 个格子
    # 余位：v1 → 0，v2–6 → 7，v7–13 → 0
    expected_remainder = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}
    reservation_ok = True
    for v in range(1, 11):
        size_v = v * 4 + 17
        mv, resv = _new_matrix(size_v)
        _place_finder(mv, resv, 0, 0)
        _place_finder(mv, resv, 0, size_v - 7)
        _place_finder(mv, resv, size_v - 7, 0)
        _place_alignment(mv, resv, v)
        _place_timing(mv, resv)
        _reserve_format(mv, resv)
        _reserve_version(mv, resv, v)
        free = sum(1 for y in range(size_v) for x in range(size_v) if not resv[y][x])
        if free != TOTAL_CODEWORDS[v] * 8 + expected_remainder[v]:
            reservation_ok = False
        # 已保留但无值的格子必须为 0：这类格子会被数据填充误写（曾出过这个 bug）
        if any(mv[y][x] is None and resv[y][x] for y in range(size_v) for x in range(size_v)):
            reservation_ok = False
    results["function_pattern_reservation_ok"] = reservation_ok

    m = encode_matrix("http://192.0.2.10:8090/p/PH-0123")
    size = len(m)
    results["size"] = size
    results["size_formula_ok"] = size in {v * 4 + 17 for v in EC_BLOCKS_M}

    # 三个定位图形
    def finder_ok(r0: int, c0: int) -> bool:
        expected = [
            [1, 1, 1, 1, 1, 1, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 1, 1, 1, 1, 1, 1],
        ]
        return all(m[r0 + y][c0 + x] == expected[y][x] for y in range(7) for x in range(7))

    results["finder_tl"] = finder_ok(0, 0)
    results["finder_tr"] = finder_ok(0, size - 7)
    results["finder_bl"] = finder_ok(size - 7, 0)

    # 时序图形
    results["timing_row_ok"] = all(m[6][i] == (1 if i % 2 == 0 else 0) for i in range(8, size - 8))
    results["timing_col_ok"] = all(m[i][6] == (1 if i % 2 == 0 else 0) for i in range(8, size - 8))

    # 暗模块
    results["dark_module"] = m[size - 8][8] == 1

    # 格式信息回读：两份副本必须一致，且与 _format_bits 相符，并落在标准表内
    def _read_format_copy1(mm: list[list[int]]) -> int:
        value = 0
        for i in range(6):
            value |= mm[8][i] << (14 - i)
        value |= mm[8][7] << 8
        value |= mm[8][8] << 7
        value |= mm[7][8] << 6
        for j in range(6):
            value |= mm[j][8] << j
        return value

    def _read_format_copy2(mm: list[list[int]]) -> int:
        value = 0
        for i in range(7):
            value |= mm[size - 1 - i][8] << (14 - i)
        value |= mm[8][size - 8] << 7
        for i in range(7):
            value |= mm[8][size - 1 - i] << i
        return value

    read1 = _read_format_copy1(m)
    read2 = _read_format_copy2(m)
    results["format_copies_agree"] = read1 == read2
    declared_mask = None
    for cand in range(8):
        if read1 == _format_bits("M", cand):
            declared_mask = cand
            break
    results["format_matches_placed"] = declared_mask is not None
    results["format_declared_mask"] = declared_mask

    results["ok"] = all(
        v is True for k, v in results.items()
        if isinstance(v, bool)
    )
    return results


if __name__ == "__main__":
    import json

    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
