/* ===== Refined chart renderer (area + bar, target line, gradient) ===== */
  (function(){
    const NS = "http://www.w3.org/2000/svg";
    let uid = 0;
    const W = 340, H = 150, pL = 12, pR = 14, pT = 20, pB = 26;
    const plotW = W - pL - pR, plotH = H - pT - pB, baseY = pT + plotH;
    const fmt = (v, d) => (v == null ? "" : v.toLocaleString("en-US", {minimumFractionDigits:d, maximumFractionDigits:d}));

    function smooth(pts){
      if (pts.length < 2) return "";
      let d = `M${pts[0].x},${pts[0].y}`;
      for (let i = 0; i < pts.length - 1; i++){
        const p0 = pts[i-1] || pts[i], p1 = pts[i], p2 = pts[i+1], p3 = pts[i+2] || p2;
        const c1x = p1.x + (p2.x - p0.x)/6, c1y = p1.y + (p2.y - p0.y)/6;
        const c2x = p2.x - (p3.x - p1.x)/6, c2y = p2.y - (p3.y - p1.y)/6;
        d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
      }
      return d;
    }

    function scaleY(vmin, vmax){ return v => pT + plotH * (vmax - v) / (vmax - vmin || 1); }

    function grid(y){ return `<line class="g-grid" x1="${pL}" y1="${y.toFixed(1)}" x2="${pL+plotW}" y2="${y.toFixed(1)}"/>`; }

    // scalar or FY-stepped target line (+ optional FY divider). xc = x-center fn per index.
    function drawTarget(cfg, y, xc, n){
      if (cfg.targets){
        const t = cfg.targets;
        const segs = []; let start = 0;
        for (let i = 1; i <= n; i++){ if (i === n || t[i] !== t[start]){ segs.push({a:start, b:i-1, v:t[start]}); start = i; } }
        let d = "", labs = "";
        segs.forEach((s, si) => {
          if (s.v == null) return;
          const x1 = si === 0 ? pL : (xc(s.a-1) + xc(s.a)) / 2;
          const x2 = si === segs.length-1 ? pL+plotW : (xc(s.b) + xc(s.b+1)) / 2;
          const yv = y(s.v);
          d += `M${x1.toFixed(1)},${yv.toFixed(1)}L${x2.toFixed(1)},${yv.toFixed(1)}`;
          if (si < segs.length-1){ const yn = y(segs[si+1].v); d += `M${x2.toFixed(1)},${yv.toFixed(1)}L${x2.toFixed(1)},${yn.toFixed(1)}`; }
          labs += `<text class="g-tlab" x="${((x1+x2)/2).toFixed(1)}" y="${(yv-4).toFixed(1)}" text-anchor="middle">목표 ${fmt(s.v,cfg.dec)}${cfg.unit}</text>`;
        });
        let div = "";
        if (cfg.fydiv != null){
          const xb = (xc(cfg.fydiv-1) + xc(cfg.fydiv)) / 2;
          div = `<line x1="${xb.toFixed(1)}" y1="${pT-2}" x2="${xb.toFixed(1)}" y2="${baseY}" stroke="var(--bd)" stroke-width="1" stroke-dasharray="2 3"/>`;
          if (cfg.fyLabels){
            div += `<text class="g-ax" x="${(xb-4).toFixed(1)}" y="${pT-8}" text-anchor="end">${cfg.fyLabels[0]}</text>`;
            div += `<text class="g-ax cur" x="${(xb+4).toFixed(1)}" y="${pT-8}" text-anchor="start">${cfg.fyLabels[1]}</text>`;
          }
        }
        return div + `<path class="g-tline" d="${d}"/>` + labs;
      }
      if (cfg.target != null){
        const yt = y(cfg.target);
        return `<line class="g-tline" x1="${pL}" y1="${yt.toFixed(1)}" x2="${pL+plotW}" y2="${yt.toFixed(1)}"/>`
             + `<text class="g-tlab" x="${pL+plotW}" y="${(yt-4).toFixed(1)}" text-anchor="end">목표 ${fmt(cfg.target,cfg.dec)}${cfg.unit}</text>`;
      }
      return "";
    }

    function xLabels(labels, cur){
      const n = labels.length;
      return labels.map((l,i) => {
        const x = pL + plotW * i / (n-1 || 1);
        const c = (cur != null && i === +cur) ? " cur" : "";
        return `<text class="g-ax${c}" x="${x.toFixed(1)}" y="${H-6}" text-anchor="middle">${l}</text>`;
      }).join("");
    }

    function renderArea(cfg){
      const id = "ga" + (++uid);
      const vs = cfg.values, n = vs.length;
      const present = []; vs.forEach((v,i) => { if (v != null) present.push({v, i}); });
      const nums = present.map(o => o.v);
      let all = nums.slice();
      if (cfg.targets) all = all.concat(cfg.targets.filter(v => v != null));
      else if (cfg.target != null) all.push(cfg.target);
      let vmax = Math.max(...all), vmin = Math.min(...all);
      const span = (vmax - vmin) || vmax || 1;
      vmax += span * 0.22; vmin -= span * 0.22; if (vmin < 0) vmin = 0;
      const y = scaleY(vmin, vmax);
      const xs = i => pL + plotW * i / (n-1 || 1);
      const pts = present.map(o => ({x: xs(o.i), y: y(o.v)}));
      const line = smooth(pts);
      const area = pts.length > 1
        ? line + ` L${pts[pts.length-1].x.toFixed(1)},${baseY} L${pts[0].x.toFixed(1)},${baseY} Z` : "";
      const dots = pts.map((p,idx) => {
        const last = idx === pts.length-1;
        return `<circle class="g-dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${last?4:3}" `
             + `style="stroke:${cfg.color}${last?';fill:'+cfg.color:''}"/>`;
      }).join("");
      const lp = pts[pts.length-1], lv = present[present.length-1];
      const vlabLast = lp ? `<text class="g-vlab" x="${lp.x.toFixed(1)}" y="${(lp.y-9).toFixed(1)}" text-anchor="end">${fmt(lv.v,cfg.dec)}${cfg.unit}</text>` : "";
      return `<svg viewBox="0 0 ${W} ${H}" role="img">`
        + `<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
        + `<stop offset="0" stop-color="${cfg.color}" stop-opacity="0.26"/>`
        + `<stop offset="1" stop-color="${cfg.color}" stop-opacity="0.02"/></linearGradient></defs>`
        + grid(pT + plotH*0.33) + grid(pT + plotH*0.66)
        + `<path d="${area}" fill="url(#${id})"/>`
        + `<path d="${line}" fill="none" stroke="${cfg.color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>`
        + drawTarget(cfg, y, xs, n)
        + dots + vlabLast + xLabels(cfg.labels, cfg.cur)
        + `</svg>`;
    }

    function renderBar(cfg){
      const id = "gb" + (++uid);
      const vs = cfg.values, n = vs.length;
      const nums = vs.filter(v => v != null);
      const tvals = cfg.targets ? cfg.targets.filter(v => v != null) : (cfg.target != null ? [cfg.target] : []);
      const vmax = Math.max(...nums, ...tvals, 0) * 1.30 || 1;   // 값 라벨 자리 확보
      const y = scaleY(0, vmax);
      const slot = plotW / n, bw = Math.min(42, slot * 0.52);
      const mark = (cfg.mark || "").split(",").filter(s=>s!=="").map(Number);
      // 값 라벨: 자릿수가 많으면 만/천 단위로 축약해 슬롯 안에 들어가게 한다.
      const vabs = Math.max(...nums.map(v => Math.abs(v)), 0);
      let sc = 1, suf = "";
      if (cfg.dec === 0 && vabs >= 1e7){ sc = 1e4; suf = "만"; }
      else if (cfg.dec === 0 && vabs >= 1e5){ sc = 1e3; suf = "천"; }
      const vlab = v => {
        if (sc === 1) return fmt(v, cfg.dec) + cfg.unit;
        const s = v / sc;
        return s.toLocaleString("en-US", {maximumFractionDigits: s >= 100 ? 0 : 1}) + suf + cfg.unit;
      };
      // 글자 폭 추정: 숫자/기호는 폰트크기의 약 0.60배, 한글(만/천/원)은 1.0배
      const labW = (s, size) => [...s].reduce((w,ch) => w + size * (/[가-힣]/.test(ch) ? 1.0 : 0.60), 0);
      const maxLab = nums.reduce((a,v) => { const s = vlab(v); return s.length > a.length ? s : a; }, "");
      const fs = labW(maxLab, 9) > slot ? 8 : 9;
      // 라벨이 슬롯보다 넓으면, 앞 라벨과 세로로 겹칠 때 위로 한 칸 올려 어긋나게 둔다.
      const wide = labW(maxLab, fs) > slot * 0.92;
      // 수치 라벨: 해당월(cur)만 항상 표시. 나머지는 마우스오버/클릭 시에만 보인다(g-vlab-alt).
      const curIdx = cfg.cur !== undefined && cfg.cur !== null && cfg.cur !== "" ? Number(cfg.cur) : null;
      let bars = "", prevY = null;
      vs.forEach((v,i) => {
        if (v == null) return;
        const cx = pL + slot*i + slot/2, x = cx - bw/2, yt = y(v), h = baseY - yt;
        const over = cfg.over && cfg.target != null && v > cfg.target;
        const isMark = mark.includes(i);
        const fill = isMark ? "var(--hold)" : (over ? "var(--warn)" : `url(#${id})`);
        const isCur = curIdx != null && i === curIdx;
        bars += `<g class="g-bw${isCur ? " g-cur" : ""}" tabindex="0">`;
        bars += `<rect class="g-bar" x="${x.toFixed(1)}" y="${yt.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="4" fill="${fill}"${over?' fill-opacity="0.9"':''}/>`;
        let ly = yt - 6;
        if (wide && prevY != null && Math.abs(ly - prevY) < fs + 2) ly = prevY - (fs + 3);
        prevY = ly;
        bars += `<text class="g-vlab${isCur ? "" : " g-vlab-alt"}" x="${cx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="middle" style="font-size:${fs}px">${vlab(v)}</text>`;
        if (isMark) bars += `<text class="g-tag" x="${cx.toFixed(1)}" y="${baseY+14}" text-anchor="middle">제외</text>`;
        bars += `</g>`;
      });
      return `<svg viewBox="0 0 ${W} ${H}" role="img">`
        + `<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
        + `<stop offset="0" stop-color="${cfg.color}" stop-opacity="1"/>`
        + `<stop offset="1" stop-color="${cfg.color}" stop-opacity="0.55"/></linearGradient></defs>`
        + grid(pT + plotH*0.33) + grid(pT + plotH*0.66)
        + bars
        + drawTarget(cfg, y, i => pL + slot*i + slot/2, n)
        + xLabels(cfg.labels, cfg.cur)
        + `</svg>`;
    }

    // 다중 시리즈 꺾은선 (세부지표현황: TM-NO별·불량유형별 추이 등)
    const MULTI = ["--p-500", "--sec", "#3b82f6", "#10b981", "#f59e0b", "#ef4444",
                   "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3", "#0ea5e9"];

    // 패널 폭(≈1200px)에 맞춘 넓은 캔버스. 좁은 카드용 W/H를 쓰면 글자가 3배 이상 확대된다.
    const PW = 900, PH = 300, pPR = 20, pPT = 18, pPB = 30;
    const pPlotH = PH - pPT - pPB;

    function renderMulti(cfg){
      const sets = cfg.sets || [], n = cfg.labels.length;
      const flat = [];
      sets.forEach(s => s.forEach(v => { if (v != null) flat.push(v); }));
      if (!flat.length) return `<svg viewBox="0 0 ${PW} ${PH}"><text class="g-ax" x="${PW/2}" y="${PH/2}" text-anchor="middle">데이터 없음</text></svg>`;
      let vmax = Math.max(...flat), vmin = Math.min(...flat);
      const span = (vmax - vmin) || vmax || 1;
      vmax += span * 0.18; vmin -= span * 0.12; if (vmin < 0) vmin = 0;
      const ticks = [0, 0.5, 1].map(f => ({f, v: Math.round(vmax - (vmax - vmin) * f)}));
      // y축 라벨 글자수에 맞춰 좌측 여백을 잡는다(고정폭이면 큰 값에서 잘림)
      const pPL = Math.max(...ticks.map(t => fmt(t.v, 0).length)) * 5.4 + 10;
      const pPlotW = PW - pPL - pPR;
      const y = v => pPT + pPlotH * (vmax - v) / (vmax - vmin || 1);
      const xs = i => pPL + pPlotW * i / (n - 1 || 1);
      let paths = "", dots = "";
      sets.forEach((s, si) => {
        const col = MULTI[si % MULTI.length];
        const c = col.startsWith("--") ? `var(${col})` : col;
        const pts = [];
        s.forEach((v, i) => { if (v != null) pts.push({x: xs(i), y: y(v)}); });
        if (!pts.length) return;
        let d = `M${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
        for (let i = 1; i < pts.length; i++) d += ` L${pts[i].x.toFixed(1)},${pts[i].y.toFixed(1)}`;
        paths += `<path d="${d}" fill="none" stroke="${c}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>`;
        pts.forEach(p => { dots += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.2" fill="${c}"/>`; });
      });
      // y축 눈금 3단
      let gr = "", ylab = "";
      ticks.forEach(t => {
        const yy = pPT + pPlotH * t.f;
        gr += `<line class="g-grid" x1="${pPL}" y1="${yy.toFixed(1)}" x2="${pPL+pPlotW}" y2="${yy.toFixed(1)}"/>`;
        ylab += `<text class="g-ax" x="${(pPL-5).toFixed(1)}" y="${(yy+3).toFixed(1)}" text-anchor="end" style="font-size:9px">${fmt(t.v, 0)}</text>`;
      });
      // 라벨이 많으면(일 단위 등) 겹치지 않도록 최대 12개 정도만 골라서 표시
      const xlStep = Math.max(1, Math.ceil(n / 12));
      const xl = cfg.labels.map((l, i) => (i % xlStep !== 0 && i !== n - 1) ? "" :
        `<text class="g-ax" x="${xs(i).toFixed(1)}" y="${PH-9}" text-anchor="middle" style="font-size:10px">${l}</text>`).join("");
      return `<svg viewBox="0 0 ${PW} ${PH}" role="img">` + gr + ylab + paths + dots + xl + `</svg>`;
    }

    // 가로 막대(비중/순위): 항목이 많아도 이름이 안 겹친다
    function renderHBar(cfg){
      const id = "gh" + (++uid);
      const names = cfg.names || [], vs = cfg.values;
      const n = vs.length, rowH = 26, padT = 10;
      const h = padT * 2 + n * rowH;
      const total = vs.reduce((a, b) => a + (b || 0), 0) || 1;
      const FS = 11;
      const txtW = (s, size) => [...s].reduce((w,ch) => w + size * (/[가-힣]/.test(ch) ? 1.0 : 0.60), 0);
      const vlab = v => `${fmt(v, cfg.dec)}${cfg.unit} (${(100 * (v || 0) / total).toFixed(1)}%)`;
      const shorten = nm => nm.length > 18 ? nm.slice(0, 17) + "…" : nm;
      // 이름·값 라벨이 잘리지 않도록 실제 글자폭으로 좌우 여백을 잡는다
      const labW = Math.min(220, Math.max(...names.map(nm => txtW(shorten(nm), FS)), 60) + 6);
      const rightPad = Math.max(...vs.map(v => txtW(vlab(v), FS))) + 10;
      const barL = labW + 8, barW = Math.max(40, PW - barL - rightPad);
      const vmax = Math.max(...vs.filter(v => v != null), 0) || 1;
      let out = "";
      vs.forEach((v, i) => {
        const yy = padT + i * rowH, bw = barW * (v || 0) / vmax;
        out += `<text class="g-ax" x="${labW}" y="${(yy + 16).toFixed(1)}" text-anchor="end" style="font-size:${FS}px">${shorten(names[i] || "")}</text>`;
        out += `<rect x="${barL}" y="${(yy + 5).toFixed(1)}" width="${bw.toFixed(1)}" height="15" rx="3" fill="url(#${id})"/>`;
        out += `<text class="g-vlab" x="${(barL + bw + 6).toFixed(1)}" y="${(yy + 17).toFixed(1)}" text-anchor="start" style="font-size:${FS}px">`
             + `${vlab(v)}</text>`;
      });
      return `<svg viewBox="0 0 ${PW} ${h}" role="img">`
        + `<defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="0">`
        + `<stop offset="0" stop-color="${cfg.color}" stop-opacity="1"/>`
        + `<stop offset="1" stop-color="${cfg.color}" stop-opacity="0.55"/></linearGradient></defs>`
        + out + `</svg>`;
    }

    function draw(node){
      const d = node.dataset;
      const cfg = {
        values: (d.values || "").split(",").map(s => { s = s.trim(); return (s === "" || s === "-") ? null : Number(s); }),
        labels: (d.labels || "").split(","),   // 가로막대(hbar)는 labels가 없다
        target: d.target !== undefined ? Number(d.target) : null,
        targets: d.targets ? d.targets.split(",").map(s => { s = s.trim(); return (s === "" || s === "-") ? null : Number(s); }) : null,
        fydiv: d.fydiv !== undefined ? Number(d.fydiv) : null,
        fyLabels: d.fylabels ? d.fylabels.split(",") : null,
        color: `var(${d.color || "--p-500"})`,
        unit: d.unit || "",
        dec: d.dec !== undefined ? Number(d.dec) : (d.unit === "%" ? 2 : 0),
        cur: d.cur,
        over: d.over === "1",
        mark: d.mark,
        names: d.names ? d.names.split("|") : null,
        sets: d.sets ? d.sets.split("|").map(row =>
                row.split(",").map(s => { s = s.trim(); return (s === "" || s === "-") ? null : Number(s); })) : null
      };
      node.innerHTML = d.type === "bar" ? renderBar(cfg)
                     : d.type === "multi" ? renderMulti(cfg)
                     : d.type === "hbar" ? renderHBar(cfg)
                     : renderArea(cfg);
      // 이전월 수치 라벨: 터치기기는 hover가 없으므로 클릭/탭으로 토글한다(PC는 CSS hover로 표시).
      if (d.type === "bar" && !node._bwBound) {
        node._bwBound = true;
        node.addEventListener("click", function (e) {
          const g = e.target.closest(".g-bw");
          node.querySelectorAll(".g-bw.show").forEach(el => { if (el !== g) el.classList.remove("show"); });
          if (g) g.classList.toggle("show");
        });
      }
    }

    // 지표 전환 등으로 data-* 를 바꾼 뒤 다시 그릴 수 있게 노출
    window.renderChart = draw;
    document.querySelectorAll(".jchart").forEach(draw);
  })();
