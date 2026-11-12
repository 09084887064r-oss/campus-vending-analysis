/* ------------------------------------------------------------------
   交互脚本：内联 SVG 图表（bar / hbar / line / donut）、表格排序、
   CSV 导出、滚动高亮。零外部依赖，可离线打开。
   ------------------------------------------------------------------ */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var PALETTE = ["#2f5597", "#0f766e", "#b45309", "#7c3aed", "#be123c",
                 "#0369a1", "#4d7c0f", "#a16207", "#5b21b6", "#155e75"];

  function el(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    }
    return node;
  }

  function fmtNumber(value, unit) {
    if (value === null || value === undefined || isNaN(value)) return "N/A";
    var num = Number(value);
    var text = Math.abs(num) >= 1000
      ? num.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
      : (Math.round(num * 100) / 100).toString();
    if (unit === "yuan") return "¥" + text;
    if (unit === "pct") return text + "%";
    return text;
  }

  function truncate(text, max) {
    text = String(text);
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // ---------------------------------------------------------------- tooltip
  var tip = document.querySelector(".tooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "tooltip";
    document.body.appendChild(tip);
  }

  function showTip(html, event) {
    tip.innerHTML = html;
    tip.classList.add("show");
    moveTip(event);
  }
  function moveTip(event) {
    var x = event.clientX + 14;
    var y = event.clientY - 10;
    var rect = tip.getBoundingClientRect();
    if (x + rect.width > window.innerWidth - 12) x = event.clientX - rect.width - 14;
    if (y < 8) y = 8;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function hideTip() { tip.classList.remove("show"); }

  function bindHover(node, html) {
    node.addEventListener("mouseenter", function (event) { showTip(html, event); });
    node.addEventListener("mousemove", moveTip);
    node.addEventListener("mouseleave", hideTip);
  }

  // ---------------------------------------------------------------- 图表分发
  function renderChart(holder) {
    var raw = holder.getAttribute("data-chart");
    if (!raw) return;
    var spec;
    try { spec = JSON.parse(raw); } catch (err) { return; }
    holder.innerHTML = "";
    var unit = spec.unit || "count";
    var draw = {
      bar: drawBar,
      hbar: drawHBar,
      line: drawLine,
      donut: drawDonut
    }[spec.kind] || drawBar;
    draw(holder, spec, unit);
  }

  function axesFrame(width, height, margins) {
    var svg = el("svg", {
      viewBox: "0 0 " + width + " " + height,
      preserveAspectRatio: "xMidYMid meet",
      role: "img"
    });
    return svg;
  }

  function niceMax(value) {
    if (!value || value <= 0) return 1;
    var exponent = Math.floor(Math.log10(value));
    var base = Math.pow(10, exponent);
    var scaled = value / base;
    var step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return step * base;
  }

  function gridLines(svg, area, max, unit) {
    var ticks = 4;
    for (var i = 0; i <= ticks; i++) {
      var y = area.y + area.h - (area.h * i) / ticks;
      svg.appendChild(el("line", {
        x1: area.x, y1: y, x2: area.x + area.w, y2: y,
        stroke: i === 0 ? "#c7cddb" : "#eef1f6", "stroke-width": 1
      }));
      var label = el("text", {
        x: area.x - 8, y: y + 4, "text-anchor": "end",
        "font-size": 11, fill: "#667085"
      });
      label.textContent = fmtNumber((max * i) / ticks, unit === "yuan" ? "yuan" : "count");
      svg.appendChild(label);
    }
  }

  // ---------------------------------------------------------------- 柱状图
  function drawBar(holder, spec, unit) {
    var cats = spec.categories || [];
    var values = (spec.series && spec.series[0] && spec.series[0].values) || [];
    if (!cats.length) { holder.textContent = "无数据"; return; }
    var width = 920, height = 360;
    var margins = { l: 68, r: 16, t: 18, b: 62 };
    var area = {
      x: margins.l, y: margins.t,
      w: width - margins.l - margins.r,
      h: height - margins.t - margins.b
    };
    var max = niceMax(Math.max.apply(null, values.filter(function (v) { return typeof v === "number"; })));
    var svg = axesFrame(width, height);
    gridLines(svg, area, max, unit);

    var band = area.w / cats.length;
    var barW = Math.min(band * 0.58, 74);
    cats.forEach(function (cat, index) {
      var value = values[index];
      var numeric = typeof value === "number" ? value : 0;
      var h = max ? (numeric / max) * area.h : 0;
      var x = area.x + band * index + (band - barW) / 2;
      var y = area.y + area.h - h;
      var color = (spec.highlight && spec.highlight === cat) ? "#b45309" : PALETTE[0];
      var rect = el("rect", {
        x: x, y: y, width: barW, height: Math.max(h, 1), rx: 4, fill: color, opacity: 0.92
      });
      svg.appendChild(rect);
      bindHover(rect, "<b>" + cat + "</b><br>" + fmtNumber(value, unit));

      var label = el("text", {
        x: area.x + band * index + band / 2,
        y: height - margins.b + 18,
        "text-anchor": "middle", "font-size": 11, fill: "#475467"
      });
      label.textContent = truncate(cat, band < 60 ? 6 : 10);
      if (band < 46) {
        label.setAttribute("transform",
          "rotate(-32 " + (area.x + band * index + band / 2) + " " + (height - margins.b + 18) + ")");
        label.setAttribute("text-anchor", "end");
      }
      svg.appendChild(label);

      if (unit === "yuan" && numeric >= max * 0.18) {
        var val = el("text", {
          x: x + barW / 2, y: y - 6, "text-anchor": "middle",
          "font-size": 10.5, fill: "#344054"
        });
        val.textContent = fmtNumber(numeric, unit);
        svg.appendChild(val);
      }
    });
    holder.appendChild(svg);
  }

  // ---------------------------------------------------------------- 横向条形图
  function drawHBar(holder, spec, unit) {
    var cats = spec.categories || [];
    var values = (spec.series && spec.series[0] && spec.series[0].values) || [];
    if (!cats.length) { holder.textContent = "无数据"; return; }
    var rowH = 30;
    var width = 920;
    var height = Math.max(cats.length * rowH + 42, 160);
    var margins = { l: 96, r: 78, t: 10, b: 20 };
    var area = { x: margins.l, y: margins.t, w: width - margins.l - margins.r,
                 h: cats.length * rowH };
    var max = niceMax(Math.max.apply(null, values.filter(function (v) { return typeof v === "number"; })));
    var svg = axesFrame(width, height);

    cats.forEach(function (cat, index) {
      var value = values[index];
      var numeric = typeof value === "number" ? value : 0;
      var w = max ? (numeric / max) * area.w : 0;
      var y = area.y + index * rowH + 5;
      var bar = el("rect", {
        x: area.x, y: y, width: Math.max(w, 1), height: rowH - 12, rx: 4,
        fill: (spec.highlight && spec.highlight === cat) ? "#b45309" : PALETTE[1],
        opacity: 0.9
      });
      svg.appendChild(bar);
      bindHover(bar, "<b>" + cat + "</b><br>" + fmtNumber(value, unit));

      var name = el("text", {
        x: area.x - 10, y: y + (rowH - 12) / 2 + 4,
        "text-anchor": "end", "font-size": 11.5, fill: "#344054"
      });
      name.textContent = truncate(cat, 12);
      svg.appendChild(name);

      var val = el("text", {
        x: area.x + w + 8, y: y + (rowH - 12) / 2 + 4,
        "font-size": 11, fill: "#475467"
      });
      val.textContent = fmtNumber(value, unit);
      svg.appendChild(val);
    });
    holder.appendChild(svg);
  }

  // ---------------------------------------------------------------- 折线图
  function drawLine(holder, spec, unit) {
    var cats = spec.categories || [];
    var values = (spec.series && spec.series[0] && spec.series[0].values) || [];
    if (!cats.length) { holder.textContent = "无数据"; return; }
    var width = 920, height = 340;
    var margins = { l: 62, r: 18, t: 18, b: 46 };
    var area = { x: margins.l, y: margins.t,
                 w: width - margins.l - margins.r, h: height - margins.t - margins.b };
    var numericValues = values.map(function (v) { return typeof v === "number" ? v : 0; });
    var max = niceMax(Math.max.apply(null, numericValues));
    var svg = axesFrame(width, height);
    gridLines(svg, area, max, unit);

    var step = cats.length > 1 ? area.w / (cats.length - 1) : area.w;
    var points = numericValues.map(function (value, index) {
      return {
        x: area.x + step * index,
        y: area.y + area.h - (max ? (value / max) * area.h : 0),
        value: values[index], cat: cats[index]
      };
    });

    var linePath = points.map(function (p, i) {
      return (i === 0 ? "M" : "L") + p.x.toFixed(1) + " " + p.y.toFixed(1);
    }).join(" ");
    var areaPath = linePath + " L" + points[points.length - 1].x.toFixed(1) + " " +
      (area.y + area.h) + " L" + points[0].x.toFixed(1) + " " + (area.y + area.h) + " Z";

    var gradient = el("linearGradient", { id: "lineFill", x1: 0, y1: 0, x2: 0, y2: 1 });
    gradient.appendChild(el("stop", { offset: "0%", "stop-color": PALETTE[0], "stop-opacity": 0.26 }));
    gradient.appendChild(el("stop", { offset: "100%", "stop-color": PALETTE[0], "stop-opacity": 0.02 }));
    var defs = el("defs");
    defs.appendChild(gradient);
    svg.appendChild(defs);
    svg.appendChild(el("path", { d: areaPath, fill: "url(#lineFill)" }));
    svg.appendChild(el("path", { d: linePath, fill: "none", stroke: PALETTE[0], "stroke-width": 2.2,
                                  "stroke-linejoin": "round" }));

    points.forEach(function (p, index) {
      var dot = el("circle", { cx: p.x, cy: p.y, r: 4.2, fill: "#fff",
                               stroke: PALETTE[0], "stroke-width": 2 });
      bindHover(dot, "<b>" + p.cat + "</b><br>" + fmtNumber(p.value, unit));
      svg.appendChild(dot);
      if (index % Math.ceil(cats.length / 12) === 0 || cats.length <= 12) {
        var label = el("text", { x: p.x, y: height - margins.b + 18, "text-anchor": "middle",
                                 "font-size": 10.5, fill: "#667085" });
        label.textContent = p.cat;
        svg.appendChild(label);
      }
    });
    holder.appendChild(svg);
  }

  // ---------------------------------------------------------------- 环形图
  function drawDonut(holder, spec, unit) {
    var cats = spec.categories || [];
    var values = (spec.series && spec.series[0] && spec.series[0].values) || [];
    var numeric = values.map(function (v) { return typeof v === "number" ? v : 0; });
    var total = numeric.reduce(function (a, b) { return a + b; }, 0);
    if (!cats.length || total <= 0) { holder.textContent = "无数据"; return; }

    var size = 340, cx = 170, cy = 168, rOuter = 128, rInner = 78;
    var svg = el("svg", { viewBox: "0 0 " + size + " " + size, role: "img" });
    var angle = -Math.PI / 2;
    cats.forEach(function (cat, index) {
      var share = numeric[index] / total;
      var end = angle + share * Math.PI * 2;
      var large = share > 0.5 ? 1 : 0;
      var x1 = cx + rOuter * Math.cos(angle), y1 = cy + rOuter * Math.sin(angle);
      var x2 = cx + rOuter * Math.cos(end), y2 = cy + rOuter * Math.sin(end);
      var x3 = cx + rInner * Math.cos(end), y3 = cy + rInner * Math.sin(end);
      var x4 = cx + rInner * Math.cos(angle), y4 = cy + rInner * Math.sin(angle);
      var d = "M" + x1 + " " + y1 + " A" + rOuter + " " + rOuter + " 0 " + large + " 1 " +
        x2 + " " + y2 + " L" + x3 + " " + y3 + " A" + rInner + " " + rInner + " 0 " + large +
        " 0 " + x4 + " " + y4 + " Z";
      var slice = el("path", { d: d, fill: PALETTE[index % PALETTE.length], opacity: 0.92 });
      bindHover(slice, "<b>" + cat + "</b><br>" + fmtNumber(numeric[index], unit) +
        " · " + (share * 100).toFixed(1) + "%");
      svg.appendChild(slice);
      angle = end;
    });
    var centerValue = el("text", { x: cx, y: cy - 2, "text-anchor": "middle",
                                   "font-size": 17, "font-weight": 700, fill: "#1f3864" });
    centerValue.textContent = fmtNumber(total, unit);
    svg.appendChild(centerValue);
    var centerLabel = el("text", { x: cx, y: cy + 18, "text-anchor": "middle",
                                   "font-size": 11, fill: "#667085" });
    centerLabel.textContent = "合计";
    svg.appendChild(centerLabel);
    holder.appendChild(svg);

    var legend = document.createElement("div");
    legend.className = "legend";
    legend.style.cssText = "display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:10px;font-size:12.5px";
    cats.forEach(function (cat, index) {
      var item = document.createElement("span");
      item.style.cssText = "display:inline-flex;align-items:center;gap:6px;color:#344054";
      var dot = document.createElement("i");
      dot.style.cssText = "width:9px;height:9px;border-radius:3px;display:inline-block;background:" +
        PALETTE[index % PALETTE.length];
      item.appendChild(dot);
      var textNode = document.createElement("span");
      textNode.textContent = cat + " " + (numeric[index] / total * 100).toFixed(1) + "%";
      item.appendChild(textNode);
      legend.appendChild(item);
    });
    holder.appendChild(legend);
  }

  // ---------------------------------------------------------------- 表格排序
  function parseCell(text) {
    var cleaned = String(text).replace(/[¥￥,\s%]/g, "");
    if (cleaned === "" || cleaned === "N/A" || cleaned === "-") return null;
    var num = parseFloat(cleaned);
    return isNaN(num) ? null : num;
  }

  function attachSort(table) {
    var headers = table.querySelectorAll("thead th");
    var body = table.querySelector("tbody");
    if (!body) return;
    headers.forEach(function (th, index) {
      th.classList.add("sortable");
      th.addEventListener("click", function () {
        var direction = th.classList.contains("sorted-asc") ? -1 : 1;
        headers.forEach(function (other) {
          other.classList.remove("sorted-asc", "sorted-desc");
        });
        th.classList.add(direction === 1 ? "sorted-asc" : "sorted-desc");
        var rows = Array.prototype.slice.call(body.querySelectorAll("tr"));
        rows.sort(function (a, b) {
          var va = parseCell((a.children[index] || {}).textContent || "");
          var vb = parseCell((b.children[index] || {}).textContent || "");
          if (va === null && vb === null) return 0;
          if (va === null) return 1;
          if (vb === null) return -1;
          return (va - vb) * direction;
        });
        rows.forEach(function (row) { body.appendChild(row); });
      });
    });
  }

  // ---------------------------------------------------------------- CSV 导出
  function tableToCsv(table) {
    var lines = [];
    table.querySelectorAll("tr").forEach(function (tr) {
      var cells = [];
      tr.querySelectorAll("th, td").forEach(function (cell) {
        var text = String(cell.textContent).replace(/\s+/g, " ").trim().replace(/"/g, '""');
        cells.push('"' + text + '"');
      });
      if (cells.length) lines.push(cells.join(","));
    });
    return lines.join("\r\n");
  }

  function download(name, content) {
    var blob = new Blob(["\ufeff" + content], { type: "text/csv;charset=utf-8" });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.appendChild(link);
    link.click();
    setTimeout(function () {
      URL.revokeObjectURL(link.href);
      link.remove();
    }, 1200);
  }

  function attachTools() {
    document.querySelectorAll("[data-csv]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = document.getElementById(button.getAttribute("data-csv"));
        if (!target) return;
        var title = target.getAttribute("data-title") || "table";
        download(title + ".csv", tableToCsv(target));
      });
    });
    document.querySelectorAll("table[data-sortable]").forEach(attachSort);
  }

  // ---------------------------------------------------------------- 导航高亮
  function attachScrollSpy() {
    var links = Array.prototype.slice.call(document.querySelectorAll(".nav a[href^='#']"));
    if (!links.length || !("IntersectionObserver" in window)) return;
    var map = {};
    links.forEach(function (link) {
      map[link.getAttribute("href").slice(1)] = link;
    });
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        links.forEach(function (link) { link.classList.remove("active"); });
        var link = map[entry.target.id];
        if (link) link.classList.add("active");
      });
    }, { rootMargin: "-70px 0px -70% 0px", threshold: 0 });
    Object.keys(map).forEach(function (id) {
      var section = document.getElementById(id);
      if (section) observer.observe(section);
    });
  }

  // ---------------------------------------------------------------- 启动
  function boot() {
    document.querySelectorAll(".chart-holder").forEach(renderChart);
    attachTools();
    attachScrollSpy();
    var year = document.getElementById("build-year");
    if (year) year.textContent = new Date().getFullYear();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
