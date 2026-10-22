"""拍摄位 v3 的静态界面契约。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "static/js/booth.js").read_text(encoding="utf-8")
    css = (ROOT / "static/css/booth.css").read_text(encoding="utf-8")
    server = (ROOT / "booth_server.py").read_text(encoding="utf-8")
    checks = []

    def check(value, label):
        ok = bool(value)
        checks.append(ok)
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))

    check('id="orientations"' in html and 'data-orientation="wide"' in html,
          "参与者页提供横竖版选择")
    check('id="styles"' in html and 'id="style-note"' in html,
          "参与者页提供风格选择和服务状态提示")
    check('id="name"' in html and 'id="handle"' in html and 'details class="optional-info"' in html,
          "个人信息为可选折叠区")
    check('state.orientation' in js and 'orientation: state.orientation' in js,
          "拍摄请求携带版式方向")
    check("正在抽取一张匹配背景" in js, "生成等待态包含动态背景反馈")
    check("现场原片回退" in server and "compose_generated" in server,
          "AIGC 失败不再回退固定背景")
    check('/api/aigc/styles' in js and 'style-card' in js,
          "风格卡使用公开目录和本地预览")
    check('生成服务未开启 · 暂不可生成' in js and 'aigc_enabled' in js and '原片' not in js,
          "AIGC 未开启时不显示原片选项")
    check('个人信息不会发送给风格生成服务' in html,
          "页面声明个人信息不进入生成请求")
    check('该步骤可能由第三方模型完成' in html and '告知您第三方的名称及处理方式' in html,
          "拍摄告知包含第三方生成及处理方式说明")
    check('#SpacemiTDevCon' in html and '#SpacemiTDevCon' not in html.replace('#SpacemiTDevCon', '', 1),
          "页面标签只有一个规范写法")
    check('.style-gallery' in css and '.segmented' in css and '.optional-info' in css,
          "浅色照相馆布局样式已落地")

    print("%d/%d PASS" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
