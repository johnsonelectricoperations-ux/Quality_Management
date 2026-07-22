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
      const vmax = Math.max(...nums, ...tvals, 0) * 1.18 || 1;
      const y = scaleY(0, vmax);
      const slot = plotW / n, bw = Math.min(42, slot * 0.52);
      const mark = (cfg.mark || "").split(",").filter(s=>s!=="").map(Number);
      let bars = "";
      vs.forEach((v,i) => {
        if (v == null) return;
        const cx = pL + slot*i + slot/2, x = cx - bw/2, yt = y(v), h = baseY - yt;
        const over = cfg.over && cfg.target != null && v > cfg.target;
        const isMark = mark.includes(i);
        const fill = isMark ? "var(--hold)" : (over ? "var(--warn)" : `url(#${id})`);
        bars += `<rect class="g-bar" x="${x.toFixed(1)}" y="${yt.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="4" fill="${fill}"${over?' fill-opacity="0.9"':''}/>`;
        bars += `<text class="g-vlab" x="${cx.toFixed(1)}" y="${(yt-6).toFixed(1)}" text-anchor="middle">${fmt(v,cfg.dec)}${cfg.unit}</text>`;
        if (isMark) bars += `<text class="g-tag" x="${cx.toFixed(1)}" y="${baseY+14}" text-anchor="middle">제외</text>`;
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

    document.querySelectorAll(".jchart").forEach(node => {
      const d = node.dataset;
      const cfg = {
        values: d.values.split(",").map(s => { s = s.trim(); return (s === "" || s === "-") ? null : Number(s); }),
        labels: d.labels.split(","),
        target: d.target !== undefined ? Number(d.target) : null,
        targets: d.targets ? d.targets.split(",").map(s => { s = s.trim(); return (s === "" || s === "-") ? null : Number(s); }) : null,
        fydiv: d.fydiv !== undefined ? Number(d.fydiv) : null,
        fyLabels: d.fylabels ? d.fylabels.split(",") : null,
        color: `var(${d.color || "--p-500"})`,
        unit: d.unit || "",
        dec: d.dec !== undefined ? Number(d.dec) : (d.unit === "%" ? 2 : 0),
        cur: d.cur,
        over: d.over === "1",
        mark: d.mark
      };
      node.innerHTML = d.type === "bar" ? renderBar(cfg) : renderArea(cfg);
    });
  })();
