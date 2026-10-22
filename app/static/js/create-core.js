/* create-core.js —— 竖屏「生成之核」等待动效（/vertical 专用）
 *
 * 为什么存在：AIGC 生图实测 30~150 秒，是全流程最长的一段。原来的
 * 「K3 流水线」步骤清单把这段时间表现成一个工程进度条 —— 看得懂，但不好看，
 * 而且参与者站在一两米外盯着它等两分钟。
 *
 * 现在改成：流水线清单整块撤掉，改用一块全画布动效把同一件事演出来。
 * 关键约束 —— **动效是真实的**，不是空转：
 *   - 抠像粒子云：形状直接取自后端 /api/cutout/<record> 的真实 alpha 通道，
 *     不是画的通用人形剪影（见 sampleParticles）；
 *   - 阶段推进：由 active_job.snapshot() 的 steps（后端真实记录的
 *     {key,label,ms,detail,at}）驱动，done 的事件线亮起，当前阶段在呼吸；
 *   - 进度条：steps 里每步的 ms 累加成真实时间轴；渲染阶段（只有开始时间、
 *     没有结束时间）用 elapsed 走一条不会到头的尾巴，不假装知道百分比；
 *   - 精抠读数：matte_ms / matte_source 原样显示；
 *   - 风格化：style_preview_url 出来后，真实成片从粒子里浮出来接管画面。
 * 编数字是这套东西最容易犯的错，所以上面每一条都留了真实来源。
 *
 * 调用方（vertical.js）每次轮询喂一次 state；本模块自己跑 rAF 渲染。
 */
(function () {
  "use strict";

  // HDMI 大屏的合成预算比桌面浏览器更紧。粒子数量和画布分辨率一起过高时，
  // Chromium 会丢合成瓦片，表现为背景缺块或瞬时闪烁；这里保留主体轮廓，
  // 把预算固定在 K3 能稳定输出的范围内。
  const SCENE_TARGET = 1600;
  const SCENE_MAX = 2600;
  const PAINT_INTERVAL_MS = 1000 / 30;
  const REPULSE_FORCE = 0.0016;
  const SPRING = 0.045;           // 粒子回位弹簧
  const DAMPING = 0.86;
  /* 真抠像图"长出来"的时长（毫秒）。配合后端 matting 阶段的真实耗时给出：
     实测 MODNet 精抠约 3.4~6.4 秒，取 2600ms 让生长发生在阶段中段，
     既看得见过程，又不会拖到阶段结束还没长完。 */
  const GROW_MS = 2600;

  /* 缓出：生长是"噗"地一下铺开再收住，不是匀速（匀速会显得机械）。 */
  function ease_out(x) { return 1 - Math.pow(1 - x, 3); }

  /* 四个阶段。ms 是"期望耗时"，只用来做进度条的相对配比 —— 真实耗时全部
     来自后端 steps 的 ms，这里不参与任何显示。 */
  const PHASES = [
    { key: "capture", label: "抓帧", caps: ["接住这一帧", "保住原始细节"] },
    { key: "infer", label: "人像分割", caps: ["把你从背景里请出来", "沿着轮廓走一遍"] },
    { key: "render", label: "风格重绘", caps: ["正在把你画成另一种风格", "笔触还在走"] },
    { key: "store", label: "入库编号", caps: ["盖戳编号", "马上就好"] },
  ];

  /* 当前阶段不能只靠"已完成几步"推：
     steps 里的 render 一步在后端是**发出请求时就登记**的，所以它的 key 早就
     出现在 steps 里 —— 第 20 秒时已完成 3 个 key，按计数会算成第 4 阶段
     「入库编号」，而人其实还卡在重绘里等一百秒（2026-10-15 截图抓到）。
     正确判据是 stage：它才是"机器此刻在做哪一步"。
     两者取可靠的那个：stage 认得出来就用它，认不出来退回计数。 */
  const STAGE_PHASE = {
    capturing: 0, inferring: 1, matting: 1,
    stylizing: 2, rendering: 2, storing: 3,
  };

  function createCore() {
    const stage = document.getElementById("v-core");
    const canvas = document.getElementById("v-core-fx");
    const styleTag = document.getElementById("v-core-style");
    const modeTag = document.getElementById("v-core-mode");
    const caption = document.getElementById("v-core-caption");
    const poseHint = document.getElementById("v-core-pose");
    const stepsBox = document.getElementById("v-core-steps");
    const timerBox = document.getElementById("v-core-timer");
    if (!stage || !canvas) return { update() {}, clear() {}, destroy() {} };

    const ctx = canvas.getContext("2d");
    const reduced = window.matchMedia
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let W = 0, H = 0, dpr = 1;
    /* 画布已按 dpr 缩放，而粒子的半径是按 CSS 像素给的 —— 点半径要乘回 dpr
       才和设备像素对齐，否则高分屏上点会细得看不见、低分屏上又糊成一团。 */
    let DPR = 1;
    let running = false, rafId = 0, shown = false;
    let lastPaintAt = 0;
    let t0 = performance.now();
    let phaseIndex = 0;             // 当前阶段（0..3），以 stage 为准（见 STAGE_PHASE）
    let doneKeys = new Set();       // 已出现在 steps 里的 key（后端登记即出现）
    let doneCount = 0;              // 已登记数：只在 stage 认不出来时兜底
    let elapsedMs = 0;
    let segStart = null;            // 当前阶段被首次观测到的时刻
    let burst = 0;                  // 阶段切换时的爆开强度
    let styleSeq = 0;
    let cutoutSeq = 0;             // 拿到真实抠像图的时刻（起算"生长"动画）
    let styleImg = null;
    let cutoutImg = null;
    let cutoutSurface = null;      // { cv, sw, sh }：完整抠像图的离屏缓存（见 sampleParticles）
    let segProgress = 0;            // 当前阶段内环的推进量

    /* ---- 形状采样：把真实 alpha 变成粒子坐标 ---------------------------
       只在 alpha 真的构成一个"人"的时候才采用。三条防呆，缺一条就会把
       整幅矩形当成形状（2026-10-15 实测踩到过）：
         1. 覆盖率 —— 人像大约占画面 10%~70%；>85% 说明拿到的其实是
            没有 alpha 通道的整幅图（JPEG 抠像、原片兜底都会这样）；
         2. 包围盒 —— 人像不会同时贴住左右和上下四条边；
         3. 点数下限 —— 太少说明 alpha 基本是空的，退回程序化轮廓更好看。 */
    function sampleParticles(image) {
      /* 采样宽度。★ 实测教训：K3 的抠像图是 3840×2400 的横构图，人像横跨
         88% 宽 / 100% 高，细节密度极高。此前用 104px 采样，头肩五官全被
         压成一块方砖，站在两米外看不出人形（2026-10-15 K3 实机截图确认）。
         192px 下五官与发际线能辨认，且读像素的成本仍可接受（192×120）。 */
      const w = 192;
      const h = Math.max(1, Math.round(w * (image.naturalHeight || 1) / (image.naturalWidth || 1)));
      const off = document.createElement("canvas");
      off.width = w; off.height = h;
      const octx = off.getContext("2d", { willReadFrequently: true });
      octx.drawImage(image, 0, 0, w, h);
      let data;
      try {
        data = octx.getImageData(0, 0, w, h).data;
      } catch (err) {
        return null;                       // 读不到像素就退回程序化轮廓
      }
      const hits = [];
      let minX = w, maxX = -1, minY = h, maxY = -1;
      /* 顺手把**完整分辨率的抠像图**缓存在离屏画布上：主循环每帧都要把它
         画出来（人像本体就是这张图），每帧重新解码/缩放太贵。
         尺寸封顶 1600 宽 —— K3 抠像图有 3840×2400 的，原样缓存吃内存。 */
      try {
        const cap = 1600;
        const sw = image.naturalWidth || w, sh = image.naturalHeight || h;
        const k = Math.min(1, cap / Math.max(sw, sh));
        const cw = Math.max(1, Math.round(sw * k)), chh = Math.max(1, Math.round(sh * k));
        const cv = document.createElement("canvas");
        cv.width = cw; cv.height = chh;
        cv.getContext("2d").drawImage(image, 0, 0, cw, chh);
        cutoutSurface = { cv, sw, sh };
      } catch (err) { cutoutSurface = null; }
      for (let y = 0; y < h; y += 1) {
        for (let x = 0; x < w; x += 1) {
          const o = (y * w + x) * 4;
          if (data[o + 3] > 128) {
            /* ★ 连**颜色**一起采：抠像图的 RGB 通道里是真彩内容（头发、皮肤、
               衣服各有其色）。早先只读 alpha、把整片涂成同一个绿色，于是
               人读作"一坨点"而不是"一个人"（2026-10-16 用户指出）。
               带颜色的点云才看得出五官和质感。 */
            hits.push([x / w, y / h, data[o], data[o + 1], data[o + 2]]);
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (y < minY) minY = y;
            if (y > maxY) maxY = y;
          }
        }
      }
      if (hits.length < 120) return null;
      const cover = hits.length / (w * h);
      if (cover > 0.85) return null;                     // 整幅：不是抠像图
      const bw = (maxX - minX + 1) / w, bh = (maxY - minY + 1) / h;
      if (bw > 0.96 && bh > 0.96) return null;           // 四边都贴住：同上
      if (bw < 0.08 || bh < 0.12) return null;           // 一条细线：不是人

      /* ★ 真实宽高比必须带上图像自身的长宽。
         抠像图 1920×1080（横），人占 bw=0.70 宽 / bh=1.00 高时：
           真实比例 = (0.70×1920) / (1.00×1080) = 1.24  ← 宽大于高
         早先只算 bw/bh = 0.70，方向都反了，人像被拉成窄塔
         （2026-10-15 K3 实机截图确认）。 */
      const imgW = image.naturalWidth || 1, imgH = image.naturalHeight || 1;
      const aspect = (bw * imgW) / Math.max(bh * imgH, 1e-6);
      /* 人像在抠像图里的朝向也要带上：抠像图是**横构图**，人的包围盒又常常
         顶到画面下边（脖子以下被裁）。所以形状天然是"头大、肩被切"的。
         这没法靠推算还原，只能如实呈现 —— 见 layoutPlace 里对裁切的处理。 */
      const cropped = { top: minY <= 0, bottom: maxY >= h - 1, left: minX <= 0, right: maxX >= w - 1 };

      /* 归一化到包围盒中心（hx,hy ∈ 0..1，0.5 = 中心），**不做任何拉伸** ——
         拉伸会把人变形，这是硬约束。形状的"人味"全在 hx/hy 的相对位置里。
         填充方式在 recomputeTarget 里定：按**宽度**装进成片框，头顶对齐框顶，
         肩膀由框底自然裁掉（抠像本来就是裁到肩的，正好接上）。
         这样粒子云读作"一个人站在证件照取景里"，且与后出的成片同框。 */
      const cx = (minX + maxX) / 2 / w, cy = (minY + maxY) / 2 / h;
      const sx = 1 / Math.max(bw, 0.0001), sy = 1 / Math.max(bh, 0.0001);
      const wanted = Math.min(SCENE_MAX, Math.max(900, Math.round(SCENE_TARGET * 0.35 + hits.length * 0.9)));
      const out = [];
      for (let i = 0; i < wanted; i += 1) {
        const pick = hits[Math.floor(i * hits.length / wanted)];
        // 每点带自己的颜色与亮度：点云因此能显出五官与质感，而不是一片绿
        const cr = pick[2], cg = pick[3], cb = pick[4];
        const lum = (cr * 0.299 + cg * 0.587 + cb * 0.114) / 255;
        out.push({
          hx: 0.5 + (pick[0] - cx) * sx,
          hy: 0.5 + (pick[1] - cy) * sy,
          x: 0.5, y: 0.5, vx: 0, vy: 0,
          cr, cg, cb,
          lum,
          a: 0.35 + Math.random() * 0.65,
          r: 1 + Math.random() * 1.6,
          ph: Math.random() * Math.PI * 2,
          // 深度：**每个粒子定死**（不随帧变，否则整片会闪）。
          // 下方按 z 分远/中/近三层画，做体积与景深（见 drawParticles）。
          z: Math.random(),
        });
      }
      return { parts: out, aspect, cropped, bbox: { x: minX, y: minY, w: maxX - minX + 1, h: maxY - minY + 1, sw: w, sh: h } };
    }

    /* ★ 抓帧之前**不出现人形** —— 这是纪律，不是审美选择。
       早先这里有个 proceduralParticles()，用 Math.random() 画"圆头 + 三角肩"，
       等于凭空编了一个人：那时后端还没做分割，屏幕上根本不该有人形，而且
       编出来的人怎么看怎么怪（2026-10-16 用户直接问「第一个人像哪里来的」）。
       现在改成**一团无序漂移的微尘**：它只表达"机器在等你 / 场子活着"，
       不冒充任何人。真形状一律等后端 /api/mask/*-cutout.png 到位。
       坐标口径与 sampleParticles 一致（该函数返回 same shape 的对象数组）。 */
    function idleDust() {
      const out = [];
      for (let i = 0; i < 1400; i += 1) {
        // 画布中偏上的椭圆里均匀撒点（不做任何结构 —— 有结构就有人形暗示）
        const ang = Math.random() * Math.PI * 2;
        const rad = Math.sqrt(Math.random());
        out.push({
          hx: 0.5 + Math.cos(ang) * 0.46 * rad,
          hy: 0.34 + Math.sin(ang) * 0.30 * rad,
          x: 0.5, y: 0.5, vx: 0, vy: 0,
          cr: 140, cg: 200, cb: 90, lum: 0.68,
          a: 0.16 + Math.random() * 0.42,
          r: 0.7 + Math.random() * 1.1,
          ph: Math.random() * Math.PI * 2,
          z: Math.random(),
        });
      }
      return out;
    }

    let parts = idleDust();

    /* 取景：把包围盒归一化坐标映射到画布。
       ★★ 「粒子对不上人」的根因与解法，写清楚免得再踩：

       问题有两层：
         1) 抠像图是**横**的（1920×1080，人只有头肩、肩以下被裁），成片是
            **竖**的（1024×1536）。按抠像自身比例画粒子 → 一个宽扁的大圆顶，
            读不出人；成片浮出又落进另一个竖框 → 位置尺寸全对不上。
         2) 早先的 recomputeTarget 直接拿画布 H/W 推比例，横着的人被压成竖塔。

       解法：**固定成片框，两步都对齐到它**。
         · 粒子云：按人的**真实比例**装进框的宽度，头顶对齐框顶，肩由框底
           自然裁掉（抠像本来裁到肩，正好接上）—— 于是粒子读作"一个人站在
           证件照取景里"，与后出的成片同框同尺寸，转变时不跳。
         · 成片：直接按同一个框绘制（见 drawStyle / styleBox）。
       人不能被拉伸，这是硬约束 —— 比例信息走 shapeAspect。 */
    let shapeAspect = 1.2;         // 人的真实宽高比（横构图常见的值）；有真数据后更新
    let targetRx = 0.3, targetRy = 0.3;   // 包围盒半宽/半高（都以画布宽 W 为单位）
    let layoutCx = 0.5, layoutCy = 0.40;   // 包围盒**中心**在画布上的位置（归一化）

    function recomputeTarget() {
      if (!W || !H) return;
      /* 人不铺满整屏 —— 顶上要留标签、底下要留阶段大字与 chips，中间才是人。
         FIT_W=0.86：人像宽占画布 86%，脸看得清；再宽就顶到信息条了。 */
      const FIT_W = 0.86;
      const halfWpx = (FIT_W / 2) * W;
      const halfHpx = halfWpx / Math.max(shapeAspect, 0.2);
      targetRx = halfWpx / W;
      targetRy = halfHpx / W;          // ★ 除以 W：x/y 用同一单位，比例才不歪
      layoutCx = 0.5;
      layoutCy = 0.40;
    }

    /* ---- 尺寸 ---------------------------------------------------------- */
    function resize() {
      const box = stage.getBoundingClientRect();
      if (!box.width || !box.height) return;
      // 现场显示器不需要手机高密度 DPR；限制物理画布尺寸可避免 HDMI 合成层
      // 在 AIGC 动效期间反复分配大纹理。
      dpr = Math.min(1.25, window.devicePixelRatio || 1);
      DPR = dpr;
      W = box.width; H = box.height;
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      recomputeTarget();
    }

    /* 归一化坐标 → 画布坐标。sampleParticles / idleDust 产出的 (hx,hy) 都是
       **以包围盒中心为 0.5**，所以这里必须按**中心**映射，不能按顶边 ——
       早先两侧口径不一致（产出居中、这里当顶边），人像被整体推高
       （2026-10-16 走查发现）。
       单位纪律：targetRx / targetRy 都以**画布宽 W** 为单位，x/y 同一把尺，
       转画布 y 时再乘 W/H。 */
    function toCanvas(hx, hy) {
      const nx = layoutCx + (hx - 0.5) * targetRx * 2;
      const ny = layoutCy + (hy - 0.5) * targetRy * 2 * (W / H);
      return [nx, ny];
    }

    /* ---- 粒子 ---------------------------------------------------------- */
    function stepPhysics(dt, cx, cy) {
      const dtc = Math.min(2.4, dt * 60);
      // 推开半径：以画布宽为单位；转到屏幕上要乘 W
      const rp = Math.min(targetRx * 1.6, 0.34);
      for (let i = 0; i < parts.length; i += 1) {
        const p = parts[i];
        const [nx, ny] = toCanvas(p.hx, p.hy);
        const tx = nx, ty = ny;
        p.vx += (tx - p.x) * SPRING * dtc;
        p.vy += (ty - p.y) * SPRING * dtc;
        const dx = p.x - cx, dy = (p.y - cy) * (H / W);   // 转到同一单位再比距离
        const d2 = dx * dx + dy * dy;
        if (d2 < rp * rp) {
          const d = Math.sqrt(d2) || 0.0001;
          const push = (1 - d / rp) * REPULSE_FORCE * dtc;
          p.vx += (dx / d) * push;
          p.vy += (dy / d) * push * (W / H);
        }
        p.vx *= DAMPING;
        p.vy *= DAMPING;
        p.x += p.vx;
        p.y += p.vy;
      }
    }

    /* ---- 粒子绘制：三层，做出体积与景深 --------------------------------
       旧版是"每个点画一个同尺寸实心圆"，看着平、看着脏。
       现在分三遍，且每个粒子有**固定**的深度 z（由索引决定，不随帧变，
       否则会闪）：
         1) 远层（z 小）：大而虚、暗 —— 身体后面的细密点
         2) 中层：主体
         3) 近层（z 大）：小而亮、带十字星芒 —— 悬浮在镜头前的亮点
       远近用尺寸和透明度表达（真景深要逐点模糊，canvas 2D 不划算），
       但配合加法辉光已经足够读成"有厚度的东西"，而不是一张贴纸。 */
    /* 粒子颜色：**真实颜色占主导**，只留极轻的绿偏。
       ★ 早先混了 28% 的绿，结果五官和质感全被抹掉，人又读成一片绿点
       （2026-10-16 走查发现）。现在 0.90 真实色 + 0.10 绿：看得出皮肤、
       头发、衣服各自的颜色，同时整体仍在绿色视觉语言里。
       lum 高的点会画得稍大，模拟高光，这是"像个人"的关键线索之一。 */
    function partColor(p) {
      const k = 0.90;
      const r = Math.round(p.cr * k + 159 * (1 - k));
      const g = Math.round(p.cg * k + 239 * (1 - k));
      const b = Math.round(p.cb * k + 0 * (1 - k));
      return "rgb(" + r + "," + g + "," + b + ")";
    }

    function drawParticles(t, alpha, redrawHot) {
      const scanY = ((t * 0.22) % 1.2) - 0.1;
      // 按深度分桶
      const far = [], mid = [], near = [];
      for (let i = 0; i < parts.length; i += 1) {
        const p = parts[i];
        let a = p.a * alpha;
        const near0 = 1 - Math.min(1, Math.abs(p.y - scanY) / 0.035);
        if (near0 > 0) a = Math.min(1, a + near0 * 0.85 * redrawHot);
        const rec = [p.x, p.y, a, p.z, p.r, p.ph, near0, p];
        if (p.z < 0.36) far.push(rec);
        else if (p.z < 0.80) mid.push(rec);
        else near.push(rec);
      }

      // 第 1 遍：远层 —— 大而虚，做背景密度
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      for (let i = 0; i < far.length; i += 1) {
        const [x, y, a, z] = far[i];
        ctx.globalAlpha = a * 0.16 * (0.4 + z);
        ctx.fillStyle = "#4E7A00";
        ctx.beginPath();
        ctx.arc(x * W, y * H, 3.4 * DPR, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();

      // 第 2 遍：加法辉光 —— 体积感（用粒子自己的颜色，辉光才有皮肤/头发的质感）
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      for (let i = 0; i < mid.length; i += 1) {
        const [x, y, a, z, r, ph, hot, p] = mid[i];
        const wob = 1 + Math.sin(t * 1.6 + ph) * 0.13;
        ctx.globalAlpha = Math.min(0.5, a * 0.30);
        ctx.fillStyle = hot > 0.35 ? "#FFFFFF" : partColor(p);
        ctx.beginPath();
        ctx.arc(x * W, y * H, r * wob * 2.7 * DPR, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();

      // 第 3 遍：实点 —— 清晰度来自这一遍。亮的点（lum 高）稍大，像高光
      for (let i = 0; i < mid.length; i += 1) {
        const [x, y, a, z, r, ph, hot, p] = mid[i];
        const wob = 1 + Math.sin(t * 1.6 + ph) * 0.13;
        ctx.globalAlpha = a;
        ctx.fillStyle = hot > 0.35 ? "#FFFFFF" : partColor(p);
        ctx.beginPath();
        ctx.arc(x * W, y * H, r * wob * (0.85 + p.lum * 0.5) * DPR, 0, Math.PI * 2);
        ctx.fill();
      }

      // 第 4 遍：近层 —— 悬浮亮点，带十字星芒，制造"镜头前有东西"的错觉
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      for (let i = 0; i < near.length; i += 1) {
        const [x, y, a, z, r, ph, hot, p] = near[i];
        const px = x * W, py = y * H;
        const s = r * 0.9 * DPR * (1 + (z - 0.8) * 2);
        ctx.globalAlpha = Math.min(1, a * 0.9);
        ctx.fillStyle = hot > 0.35 ? "#FFFFFF" : partColor(p);
        ctx.beginPath();
        ctx.arc(px, py, s, 0, Math.PI * 2);
        ctx.fill();
        // 十字星芒（只给最亮的一档，多了就俗）
        if (z > 0.93) {
          ctx.globalAlpha = a * 0.5;
          ctx.strokeStyle = "#E4FFB0";
          ctx.lineWidth = 0.9 * DPR;
          const L = s * 3.4;
          ctx.beginPath();
          ctx.moveTo(px - L, py); ctx.lineTo(px + L, py);
          ctx.moveTo(px, py - L); ctx.lineTo(px, py + L);
          ctx.stroke();
        }
      }
      ctx.restore();
      ctx.globalAlpha = 1;
    }

    /* ---- 阶段圆环 ------------------------------------------------------
       fade 用于成片浮出时让位（0 = 完全退场）。留在画面上会挡住成片。 */
    function drawArcs(cx, cy, radius, t, fade) {
      fade = fade === undefined ? 1 : fade;
      if (fade <= 0.01) return;
      ctx.save();
      ctx.globalAlpha = fade;
      const n = PHASES.length;
      for (let i = 0; i < n; i += 1) {
        const gap = 0.055;
        const a0 = -Math.PI / 2 + (i / n) * Math.PI * 2 + gap;
        const a1 = -Math.PI / 2 + ((i + 1) / n) * Math.PI * 2 - gap;
        ctx.lineWidth = i === phaseIndex ? 2.4 : 1.4;
        ctx.strokeStyle = i < phaseIndex ? "#9FEF0066"
          : i === phaseIndex ? "#9FEF00CC" : "#FFFFFF1F";
        ctx.beginPath();
        ctx.arc(cx, cy, radius, a0, a1);
        ctx.stroke();

        // 已完成 / 进行中：在段末点一颗珠子
        if (i <= phaseIndex) {
          const cell = (i === phaseIndex)
            ? a0 + (a1 - a0) * segProgress
            : a1;
          const bx = cx + Math.cos(cell) * radius;
          const by = cy + Math.sin(cell) * radius;
          ctx.beginPath();
          ctx.arc(bx, by, i === phaseIndex ? 3.4 : 2.2, 0, Math.PI * 2);
          ctx.fillStyle = "#9FEF00";
          ctx.fill();
          if (i === phaseIndex) {
            // 进行中的珠子：一圈扩散的涟漪（比固定大小的圈更有"在动"的感觉）
            const rip = (t * 0.9) % 1;
            ctx.beginPath();
            ctx.arc(bx, by, 4 + rip * 16, 0, Math.PI * 2);
            ctx.strokeStyle = "rgba(159,239,0," + (0.55 * (1 - rip)).toFixed(3) + ")";
            ctx.lineWidth = 1.4;
            ctx.stroke();
          }
        }
      }
      ctx.restore();
    }

    /* ---- 真实抠像图的"生长"揭示 ----------------------------------------
       ★ 这是 2026-10-16 的路线修正：**照片本身才是主角，粒子只做溶解前沿**。

       为什么不再用点云画人：点云的极限是"看得出是个人形轮廓"，而现场要的是
       "看得出是我"。试了颜色、比例、层次、景深之后天花板依旧 —— 放大看就是
       一团奶白色的光点（用户三次判定"不理想"）。而 cutout.png 本来就是一张
       1920×1080 的**真彩高清抠像**，采样只用了它 1/10 的信息。所以：
         · 人像 = 那张真实的抠像照片，按 alpha 直接画（真皮肤、真头发、真衣服）
         · 粒子 = 揭示前沿的溶解效果（照片从扫描线后长出来，线上有火花飞散）
       既酷炫，又比点云**更真实**（因为就是真图）。

       bbox 取自采样时的真实 alpha 包围盒，保证与粒子的取景**完全同框**。 */
    function cutoutBox() {
      if (!cutoutImg || !cutoutImg.naturalWidth || !shapeAspect) return null;
      const w = targetRx * 2 * W;
      const h = w / shapeAspect;
      return { x: (W - w) / 2, y: layoutCy * H - h / 2, w, h };
    }

    /* reveal ∈ 0..1：0 = 完全没出来，1 = 全部显形。前沿自上而下扫过。
       返回前沿的 y（像素），供粒子和光带定位。 */
    function drawCutoutReveal(reveal, t) {
      const b = cutoutBox();
      if (!b || reveal <= 0.001) return null;
      const src = cutoutSurface;              // 采样时已缓存的离屏画布
      if (!src) return null;

      const frontY = b.y + b.h * reveal;
      ctx.save();
      /* 只画前沿以上的部分 —— 照片像被"读"出来一样自上而下显形。
         前沿下方留 46px 的羽化带：硬切会像"贴了个矩形"，羽化才像"浮现"。
         做法是两段裁剪：主块到 frontY，再用一个线性渐变把 frontY 下方
         一小段按 alpha 渐隐（canvas 没有内置的软边裁剪）。 */
      ctx.beginPath();
      ctx.rect(b.x - 2, b.y - 2, b.w + 4, Math.max(0, frontY - b.y + 2));
      ctx.clip();
      ctx.drawImage(src.cv, b.x, b.y, b.w, b.h);
      if (reveal < 0.999) {
        const feather = 46 * DPR;
        ctx.beginPath();
        ctx.rect(b.x - 2, frontY, b.w + 4, feather);
        ctx.clip();
        const soft = ctx.createLinearGradient(0, frontY, 0, frontY + feather);
        soft.addColorStop(0, "rgba(0,0,0,1)");
        soft.addColorStop(1, "rgba(0,0,0,0)");
        // 用目的地-输入的方式按渐变"擦掉"这段，得到软边
        ctx.globalCompositeOperation = "destination-out";
        ctx.fillStyle = soft;
        ctx.fillRect(b.x - 2, frontY, b.w + 4, feather);
      }
      ctx.restore();

      // 前沿光带：一条发亮的横线 + 向下的柔光，像扫描头扫过
      if (reveal < 0.999) {
        ctx.save();
        const g = ctx.createLinearGradient(0, frontY - 26 * DPR, 0, frontY + 14 * DPR);
        g.addColorStop(0, "rgba(159,239,0,0)");
        g.addColorStop(0.65, "rgba(200,255,120,0.55)");
        g.addColorStop(1, "rgba(159,239,0,0)");
        ctx.fillStyle = g;
        ctx.fillRect(b.x - 10, frontY - 26 * DPR, b.w + 20, 40 * DPR);
        ctx.strokeStyle = "rgba(228,255,176,0.95)";
        ctx.lineWidth = 1.6 * DPR;
        ctx.beginPath();
        ctx.moveTo(b.x - 10, frontY);
        ctx.lineTo(b.x + b.w + 10, frontY);
        ctx.stroke();
        ctx.restore();
      }
      return frontY;
    }

    /* ---- 真实成片浮出 --------------------------------------------------
       ★ 用**与粒子/抠像完全相同**的框（宽度 = 粒子框宽、垂直中心 = layoutCy）。
       早先是另算一套 scale（min(W*0.62/w, H*0.6/h)），于是成片"跳"到另一个
       位置和大小，与粒子对不上（2026-10-16 用户指出）。
       现在：粒子占了多宽，成片就多宽；垂直中心取同一个中心。
       两者同框，转变读起来是"同一个主体在变化"，而不是两张图叠在一起。 */
    function styleBox() {
      const ar = (styleImg && styleImg.naturalWidth)
        ? styleImg.naturalWidth / styleImg.naturalHeight : 0.667;
      // 粒子框：宽 = 2*targetRx（画布宽比例），垂直中心就是 layoutCy
      const w = Math.min(targetRx * 2 * W, W * 0.72);
      const h = w / ar;
      const cyPx = layoutCy * H;   // 与粒子框中心一致
      const x = (W - w) / 2;
      let y = cyPx - h / 2;
      // 别顶出画面：贴边就贴边，但不许裁掉内容
      y = Math.max((H - h) / 2 - H * 0.06, Math.min(y, (H - h) / 2 + H * 0.06));
      return { x, y, w, h };
    }

    function drawStyle(alpha, tSec) {
      if (!styleImg || !styleImg.complete || !styleImg.naturalWidth) return 0;
      const b = styleBox();
      const grow = 1 - (1 - alpha) * 0.04;         // 极轻的放大入位
      const gw = b.w * grow, gh = b.h * grow;
      const gx = b.x - (gw - b.w) / 2, gy = b.y - (gh - b.h) / 2;

      ctx.save();
      ctx.globalAlpha = alpha;
      // 连续圆角矩形（squircle 观感）+ 投影
      ctx.shadowColor = "rgba(0,0,0,0.55)";
      ctx.shadowBlur = 40;
      ctx.shadowOffsetY = 14;
      roundRect(ctx, gx, gy, gw, gh, 22);
      ctx.fillStyle = "#0A0C0B";
      ctx.fill();
      ctx.restore();

      ctx.save();
      roundRect(ctx, gx, gy, gw, gh, 22);
      ctx.clip();
      ctx.globalAlpha = alpha;
      ctx.drawImage(styleImg, gx, gy, gw, gh);
      // 玻璃高光：一条缓慢横移的斜光（用帧时间，不用 t0 —— t0 是启动时刻）
      const hx = gx + gw * (0.5 + Math.sin(tSec * 0.22) * 0.35);
      const hg = ctx.createLinearGradient(hx - gw * 0.35, gy, hx + gw * 0.35, gy + gh);
      hg.addColorStop(0, "rgba(255,255,255,0)");
      hg.addColorStop(0.5, "rgba(255,255,255,0.10)");
      hg.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = hg;
      ctx.fillRect(gx, gy, gw, gh);
      ctx.restore();

      // 内描边高光
      ctx.save();
      ctx.globalAlpha = alpha * 0.9;
      roundRect(ctx, gx, gy, gw, gh, 22);
      ctx.strokeStyle = "rgba(255,255,255,0.16)";
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.restore();
      ctx.globalAlpha = 1;
      return alpha;
    }

    function roundRect(c, x, y, w, h, r) {
      const rr = Math.min(r, w / 2, h / 2);
      c.beginPath();
      c.moveTo(x + rr, y);
      c.arcTo(x + w, y, x + w, y + h, rr);
      c.arcTo(x + w, y + h, x, y + h, rr);
      c.arcTo(x, y + h, x, y, rr);
      c.arcTo(x, y, x + w, y, rr);
      c.closePath();
    }

    /* ---- 主循环 -------------------------------------------------------- */
    function frame(now) {
      if (!running) return;
      rafId = requestAnimationFrame(frame);
      if (!W || !H) { resize(); if (!W) return; }
      if (!reduced && lastPaintAt && now - lastPaintAt < PAINT_INTERVAL_MS) return;
      const t = (now - t0) / 1000;
      const previousPaintAt = lastPaintAt || now;
      const dt = Math.min(0.05, (now - previousPaintAt) / 1000);
      lastPaintAt = now;

      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      ctx.clearRect(0, 0, W, H);
      /* 画布中心（归一化）。圆环与辉光要用**像素**，粒子用**归一化** ——
         两套单位混用是这里的历史 bug（圆周曾跑到左上角，因为 0.5/0.47
         被当成像素；2026-10-16 走查截图发现）。下面见到 *W / *H 的地方
         就是"转到像素"的转换点。 */
      const cx = 0.5, cy = 0.5;

      // 底：呼吸的径向辉光（在人的躯干后面 —— 躯干占包围盒下半，所以略微下移）
      const glowY = (layoutCy + targetRy * (W / H) * 0.35) * H;
      const radius = Math.min(W, H) * 0.34;
      const breathe = 0.82 + Math.sin(t * 1.15) * 0.18;
      const glow = ctx.createRadialGradient(cx * W, glowY, 0, cx * W, glowY, radius * 2.1);
      glow.addColorStop(0, "rgba(159,239,0," + (0.20 * breathe).toFixed(3) + ")");
      glow.addColorStop(0.45, "rgba(159,239,0," + (0.06 * breathe).toFixed(3) + ")");
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, W, H);

      // 生成中：粒子云 + 圆环；成片出来后粒子退到四周
      const styleAlpha = styleImg && styleImg.complete && styleImg.naturalWidth
        ? Math.min(1, (now - styleSeq) / 900) : 0;
      /* ★ 粒子必须"退干净"：早先只降到 0.75 倍，成片浮出时粒子还压在上面，
         屏幕上出现双重影像（2026-10-16 用户指出「对不上」）。
         改成跟着 styleAlpha 一路降到 0 —— 成片接管画面时粒子层面板已经空了。 */

      /* ★★ 人像的"生长"（2026-10-16 路线修正）：
         拿到真实抠像图后，把它**当作主体画出来**，粒子退成揭示前沿的火花。
         grow 由 cutoutSeq 起算、用真实时间推进 —— 不是随机的，也不是猜的；
         成片浮出时（styleAlpha）再整块让位。 */
      let grow = 0;
      if (cutoutSurface && cutoutSeq) {
        grow = Math.min(1, (now - cutoutSeq) / GROW_MS);
        grow = ease_out(grow);
      }
      // 粒子：生长阶段只留前沿附近的一圈 + 底噪，"点云当人"那条路已废弃
      const partAlpha = (1 - styleAlpha) * (grow > 0 ? 0.16 * (1 - grow) + 0.05 : 0.30);

      if (!reduced) stepPhysics(dt, cx, cy);

      /* 画序：暗底粒子 → 真抠像图（带前沿）→ 前沿火花 → 圆环 → 成片。
         照片压在粒子之上，所以"照片长出来"的过程里粒子只在边缘露出来，
         正好读作"从粒子中凝结成真图"。 */
      drawParticles(t, partAlpha, 1 - styleAlpha);
      const frontY = drawCutoutReveal(grow, t);

      /* 光环套在人像的**头肩中心**，半径按人像大小给 ——
         它必须读作"环绕这个人"，不是画面中央的一个装饰圈。 */
      const ringY = (layoutCy + targetRy * (W / H) * 0.05) * H;
      /* 光环半径收紧到 1.02×包围盒半宽：早先 1.25× 太松，套在人外面像散架，
         现在刚好沿着肩线绕过去，"环绕这个人"才立得住。 */
      drawArcs(cx * W, ringY, Math.max(targetRx * W * 1.02, 150), t,
        (1 - styleAlpha * 0.55) * (1 - grow * 0.45));

      if (styleAlpha > 0) drawStyle(styleAlpha, t);

      // 阶段完成的爆开
      if (burst > 0) {
        burst = Math.max(0, burst - dt * 2.2);
        ctx.beginPath();
        ctx.arc(cx * W, cy * H, radius * (1 + (1 - burst) * 0.5), 0, Math.PI * 2);
        ctx.strokeStyle = "rgba(159,239,0," + (burst * 0.5).toFixed(3) + ")";
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // 运行读数（真实：elapsed_ms 来自后端 snapshot）
      if (timerBox && shown) {
        const secs = Math.max(0, Math.round(elapsedMs / 1000));
        if (timerBox.dataset.v !== String(secs)) {
          timerBox.dataset.v = String(secs);
          timerBox.textContent = secs + " 秒";
        }
      }
    }

    /* 加缓存串。★ 只对 http(s) 加：data: URI 后面接 "?v=" 会整串失效
       （换成 data:...png?v=123 就不再是合法 URI，图直接加载失败）。 */
    function bust(url) {
      return /^data:/i.test(url) ? url : url + "?v=" + Date.now();
    }

    /* ---- 对外的 update：调用方每次轮询喂一次真实 state ------------------ */
    function update(active) {
      if (!active) return;
      if (!shown) {
        shown = true;
        stage.classList.add("is-on");
        stage.setAttribute("aria-hidden", "false");
        resize();
        t0 = performance.now();
        frame.last = t0;
        if (!running) { running = true; rafId = requestAnimationFrame(frame); }
      }

      elapsedMs = Number(active.elapsed_ms) || 0;

      // 1) 真实 steps → 阶段推进 + 步骤行
      const steps = active.steps || [];
      doneKeys = new Set();
      steps.forEach((s) => doneKeys.add(s.key));
      doneCount = 0;
      PHASES.forEach((p) => { if (doneKeys.has(p.key)) doneCount += 1; });

      // stage 优先（见 STAGE_PHASE 说明）；stage 缺失或未知时才退回计数
      const stagePhase = STAGE_PHASE[active.stage];
      const nextPhase = stagePhase !== undefined
        ? stagePhase
        : Math.min(PHASES.length - 1, doneCount);
      if (nextPhase !== phaseIndex) {
        phaseIndex = nextPhase;
        segStart = performance.now();
        burst = 1;
        if (caption) {
          const caps = PHASES[phaseIndex].caps;
          caption.textContent = caps[Math.floor(Math.random() * caps.length)];
        }
        // 抓帧那一步要顺带把拍摄礼仪说清楚；一旦进入分割/重绘就该撤掉，
        // 免得人在"已经拍完了"之后还僵在原地保持姿势
        if (poseHint) poseHint.classList.toggle("is-off", phaseIndex > 0);
      }
      // 同阶段内抓到"当前阶段刚开始"的真实时刻
      if (segStart === null) segStart = performance.now();

      /* 当前阶段内环的推进量。用这个阶段的**期望耗时**做分母，并且永远
         不到头（封在 0.97）—— 风格重绘实测 30~150 秒且无法预知，画一个
         会走满的进度条就是在骗人；走到九成就慢慢挪，等真实 steps 推它。
         各阶段的期望耗时来自现场实测（抓帧 <1s、分割+精抠约 5s）。 */
      const estMs = [900, 5000, 150000, 2500][phaseIndex] || 5000;
      const within = Math.min(1, (performance.now() - segStart) / estMs);
      segProgress = 0.12 + within * 0.85;

      if (stepsBox) {
        const sig = steps.map((s) => s.key + ":" + (s.ms || 0) + ":" + (s.detail || "")).join("|")
          + "#" + (active.matte_ms || "");
        if (stepsBox.dataset.sig !== sig) {
          stepsBox.dataset.sig = sig;
          renderSteps(steps, active);
        }
      }

      if (modeTag && !modeTag.dataset.v && active.portrait_mode) {
        modeTag.dataset.v = "1";
        modeTag.textContent = active.portrait_mode === "chibi" ? "Q版大头像" : "写实版";
      }

      // 风格名由调用方在 setStylePhase 里喂（它才持有 id→中文名 的映射表），
      // 拿不到就不显示，不编一个名字上去
      if (styleTag && active.style_label && styleTag.dataset.v !== active.style_label) {
        styleTag.dataset.v = active.style_label;
        styleTag.textContent = active.style_label;
      }

      // 2) 真实抠像图 → 粒子形状取自它的 alpha
      if (active.cutout_url && active.cutout_url !== cutoutImg?.dataset.src) {
        const img = new Image();
        img.dataset.src = active.cutout_url;
        img.decoding = "async";
        img.onload = () => {
          cutoutImg = img;
          const sampled = sampleParticles(img);
          if (sampled) {
            // 保留当前速度，形状换掉 —— 粒子"接管"成这个人的轮廓
            for (let i = 0; i < Math.min(parts.length, sampled.parts.length); i += 1) {
              sampled.parts[i].vx = parts[i].vx;
              sampled.parts[i].vy = parts[i].vy;
              sampled.parts[i].x = parts[i].x;
              sampled.parts[i].y = parts[i].y;
            }
            parts = sampled.parts;
            /* 人的真实比例。抠像的包围盒是横的（肩铺得开、下半被裁），
               直接当"身高比"用会显得太扁。朝 1 收 25%：既保留"肩比头宽"的
               事实，又不把画面压成横幅。实测 1.24 → 约 1.06。
               ★ 这个值同时决定**真抠像图**的显示框（cutoutBox），所以要
               兼顾"看得清脸"和"装得下衣服"：太高会顶到文字，太低脸就小。 */
            const raw = sampled.aspect;
            shapeAspect = Math.max(0.55, 1 + (raw - 1) * 0.25);
            cutoutSeq = performance.now();      // 起算"生长"动画
            recomputeTarget();
          }
        };
        img.src = bust(active.cutout_url);
      }

      // 3) 真实成片 → 从粒子里浮出来
      if (active.style_preview_url && (!styleImg || styleImg.dataset.src !== active.style_preview_url)) {
        const img = new Image();
        img.dataset.src = active.style_preview_url;
        img.decoding = "async";
        img.onload = () => { styleImg = img; styleSeq = performance.now(); };
        img.src = bust(active.style_preview_url);
      }
    }

    /* 本模块不能依赖 window.Booth（那是 net.js 提供的）。
       虽然页面上 net.js 先加载、Booth 一定在，但"渲染到一半因为别处模块
       没就绪而抛异常"是没必要的耦合 —— 之前就直接 This 崩过
       （`Cannot read properties of undefined (reading 'fmtMs')`）。
       自己带一份，格式与 net.js 的 fmtMs 保持一致。 */
    function fmtMs(ms) {
      if (ms === null || ms === undefined || ms <= 0) return "—";
      return ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : Math.round(ms) + " ms";
    }

    /* 真实步骤行：不再是"流水线清单"，是画面下方一行轻注释 */
    function renderSteps(steps, active) {
      const parts = steps.map((s) => {
        const detail = s.detail || (s.ms ? fmtMs(s.ms) : "");
        return '<span class="vcore-chip is-done"><b>' + escapeHtml(s.label)
          + "</b>" + (detail ? "<i>" + escapeHtml(detail) + "</i>" : "") + "</span>";
      });
      if (active.matte_ms) {
        const src = active.matte_source === "modnet-roi" ? "按人框"
          : active.matte_source === "modnet" ? "整幅" : "逐像素";
        parts.splice(2, 0, '<span class="vcore-chip is-matte"><b>MODNet 精抠</b><i>'
          + src + " · " + fmtMs(active.matte_ms) + "</i></span>");
      }
      stepsBox.innerHTML = parts.join("");
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[c]));
    }

    function clear() {
      shown = false;
      stage.classList.remove("is-on");
      stage.setAttribute("aria-hidden", "true");
      styleImg = null;
      if (poseHint) poseHint.classList.remove("is-off");
      if (stepsBox) { stepsBox.innerHTML = ""; stepsBox.dataset.sig = ""; }
      if (timerBox) { timerBox.textContent = "0 秒"; timerBox.dataset.v = ""; }
    }

    function destroy() {
      running = false;
      if (rafId) cancelAnimationFrame(rafId);
    }

    window.addEventListener("resize", () => { if (shown) resize(); });

    return {
      update, clear, destroy,
      /* 调用方用它决定"要不要启用旧的退化浮层"：核在画面上时，
         抠像扫描线与轨道风格层必须让位，否则两套皮肤画同一批数据会叠字。 */
      get active() { return shown; },
    };
  }

  window.BoothCreateCore = createCore;
})();
