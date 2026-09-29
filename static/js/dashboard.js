/* 温度記録ダッシュボード: 折れ線グラフ描画と自動更新 (fig1 のレイアウトに寄せる) */
(function () {
  "use strict";

  var script = document.currentScript;
  var initial = {};
  try {
    initial = JSON.parse(script.getAttribute("data-series") || "{}");
  } catch (e) {
    console.error("初期データの解析に失敗しました", e);
  }
  var refreshSeconds = parseInt(script.getAttribute("data-refresh") || "0", 10) || 0;

  var canvas = document.getElementById("temp-chart");
  var legend = document.getElementById("legend");
  var generatedAt = document.getElementById("generated-at");
  var reloadState = document.getElementById("reload-state");

  var COLORS = {
    /* 表の行ハイライト(CSS の --hl-* と必ず同じ値にする) */
    max: "#dfe6ee",       /* 現在の最大: 青 (fig1 実測値) */
    min: "#e0f0dc",       /* 現在の最小: 緑 */
    yearMin: "#f8dbe6",   /* 1年前の最小: ピンク */
    current: "#22262b",
    year: "#9aa0a6",
    grid: "#b9bcc0",
    markBorder: "#5a5f66",
    textMax: "#2b579a",
    textMin: "#2e7d32",
    textYearMin: "#5b7fa6",
  };

  var POINT_RADIUS = 2.2;

  var chart = null;

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /* "2026-09-29 14:16:00" -> Date(ブラウザのローカル時刻) */
  function parseDt(text) {
    var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(text || ""));
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  }

  /* 1年前の値を同じ時刻軸に載せるため年だけ進める(2/29 は 2/28) */
  function plusOneYear(date) {
    var d = new Date(date.getTime());
    var day = d.getDate();
    d.setFullYear(d.getFullYear() + 1);
    if (day === 29 && d.getMonth() === 1) d.setDate(28);
    return d;
  }

  /* fig1 と同じ時刻表記: "9/28 17:16" / "9:16" (時 はゼロ埋めしない) */
  function timeText(date, withDate) {
    var t = date.getHours() + ":" + pad(date.getMinutes());
    if (!withDate) return t;
    return date.getMonth() + 1 + "/" + date.getDate() + " " + t;
  }

  /* 現在 + 1年前(時刻軸に寄せる)を一緒に並べて X 軸の目盛りを作る。
     同じ日付になる最初の目盛りだけ "M/D" を付ける。 */
  function buildAxis(current, yearAgo) {
    var times = [];
    var i, d;
    for (i = 0; i < current.length; i++) {
      d = parseDt(current[i].dt);
      if (d) times.push(d.getTime());
    }
    for (i = 0; i < yearAgo.length; i++) {
      d = parseDt(yearAgo[i].dt);
      if (d) times.push(plusOneYear(d).getTime());
    }
    times = times.filter(function (t, idx, arr) {
      return arr.indexOf(t) === idx;
    });
    times.sort(function (a, b) {
      return a - b;
    });

    var labels = [];
    var positions = Object.create(null);
    var prevDay = null;
    for (i = 0; i < times.length; i++) {
      var date = new Date(times[i]);
      var withDate = prevDay === null || date.getDate() !== prevDay;
      prevDay = date.getDate();
      labels.push(timeText(date, withDate));
      positions[times[i]] = labels.length - 1;
    }
    return { labels: labels, times: times, positions: positions };
  }

  /* 行を共通軸上の {位置: 温度} に落とす。同じ位置に複数あれば平均。 */
  function toSeriesMap(rows, axis, shiftYear) {
    var buckets = Object.create(null);
    rows.forEach(function (row) {
      var d = parseDt(row.dt);
      if (!d) return;
      if (shiftYear) d = plusOneYear(d);
      var pos = axis.positions[d.getTime()];
      if (pos === undefined) return;
      if (!buckets[pos]) buckets[pos] = { sum: 0, n: 0 };
      buckets[pos].sum += row.temp;
      buckets[pos].n += 1;
    });
    var map = Object.create(null);
    Object.keys(buckets).forEach(function (pos) {
      map[pos] = Math.round((buckets[pos].sum / buckets[pos].n) * 10) / 10;
    });
    return map;
  }

  function rangeOf(map) {
    var max = null;
    var min = null;
    Object.keys(map).forEach(function (pos) {
      var v = map[pos];
      if (max === null || v > max) max = v;
      if (min === null || v < min) min = v;
    });
    return { max: max, min: min };
  }

  /* 最大/最小を色付きの横長四角で示すための {位置: 色} と、併記する数値 */
  function marksOf(map, range, maxColor, minColor) {
    var marks = Object.create(null);
    var texts = [];
    if (range.max === null) return { marks: marks, texts: texts };
    Object.keys(map).forEach(function (pos) {
      var v = map[pos];
      if (v === range.min && minColor) {
        marks[pos] = minColor;
        texts.push({ pos: Number(pos), value: v, color: minColor });
      } else if (v === range.max && maxColor && range.max !== range.min) {
        marks[pos] = maxColor;
        texts.push({ pos: Number(pos), value: v, color: maxColor });
      }
    });
    return { marks: marks, texts: texts };
  }

  function toData(map, axis) {
    return axis.labels.map(function (_label, pos) {
      return map[pos] === undefined ? null : map[pos];
    });
  }

  /* 凡例と同じ横長マーカー(点の形として使う) */
  var markerCache = Object.create(null);
  function markerImage(bg) {
    if (markerCache[bg]) return markerCache[bg];
    var c = document.createElement("canvas");
    c.width = 18;
    c.height = 9;
    var g = c.getContext("2d");
    g.fillStyle = bg;
    g.fillRect(0.5, 0.5, 17, 8);
    g.strokeStyle = COLORS.markBorder;
    g.lineWidth = 1;
    g.strokeRect(0.5, 0.5, 17, 8);
    markerCache[bg] = c;
    return c;
  }

  /* 強調点だけマーカーと色を上書きし、数値も併記する */
  var highlightPlugin = {
    id: "tempHighlight",
    beforeDatasetDraw: function (c, args) {
      var dataset = c.data.datasets[args.index];
      var marks = (dataset && dataset._marks) || {};
      var meta = c.getDatasetMeta(args.index);
      Object.keys(marks).forEach(function (pos) {
        var point = meta.data[Number(pos)];
        if (!point) return;
        point.options.radius = 5;
        point.options.pointStyle = markerImage(marks[pos]);
        point.options.backgroundColor = marks[pos];
        point.options.borderColor = COLORS.markBorder;
      });
    },
    afterDatasetDraw: function (c, args) {
      var dataset = c.data.datasets[args.index];
      var texts = (dataset && dataset._texts) || [];
      if (!texts.length) return;
      var meta = c.getDatasetMeta(args.index);
      var area = c.chartArea;
      var ctx = c.ctx;
      ctx.save();
      ctx.font = "bold 12px 'Hiragino Kaku Gothic ProN', 'Yu Gothic', sans-serif";
      ctx.textBaseline = "bottom";
      texts.forEach(function (t) {
        var point = meta.data[t.pos];
        if (!point) return;
        var label = t.value.toFixed(1);
        ctx.fillStyle = t.textColor || COLORS.markBorder;
        var w = ctx.measureText(label).width;
        var x = point.x + 8;
        if (x + w > area.right) x = point.x - 8 - w;
        var y = Math.max(point.y - 5, area.top + 12);
        ctx.fillText(label, x, y);
      });
      ctx.restore();
    },
  };

  /* マーカー背景色に対応する数値ラベルの色を決めておく */
  var TEXT_COLORS = Object.create(null);
  TEXT_COLORS[COLORS.max] = COLORS.textMax;
  TEXT_COLORS[COLORS.min] = COLORS.textMin;
  TEXT_COLORS[COLORS.yearMin] = COLORS.textYearMin;

  function withTexts(marks) {
    marks.texts.forEach(function (t) {
      t.textColor = TEXT_COLORS[t.color] || COLORS.markBorder;
    });
    return marks;
  }

  function buildDatasets(axis, current, yearAgo) {
    var currentMap = toSeriesMap(current, axis, false);
    var yearMap = toSeriesMap(yearAgo, axis, true);

    var currentMarks = withTexts(marksOf(currentMap, rangeOf(currentMap), COLORS.max, COLORS.min));
    var yearMarks = withTexts(marksOf(yearMap, rangeOf(yearMap), null, COLORS.yearMin));

    return [
      {
        label: "現在",
        data: toData(currentMap, axis),
        borderColor: COLORS.current,
        borderWidth: 2,
        tension: 0,
        spanGaps: true,
        pointStyle: "rect",
        pointRadius: POINT_RADIUS,
        pointHoverRadius: POINT_RADIUS * 2,
        pointBackgroundColor: "#ffffff",
        pointBorderColor: COLORS.current,
        _marks: currentMarks.marks,
        _texts: currentMarks.texts,
        order: 2,
      },
      {
        /* fig1 では 1年前 は細いグレーの実線 */
        label: "1年前",
        data: toData(yearMap, axis),
        borderColor: COLORS.year,
        borderWidth: 1.4,
        tension: 0,
        spanGaps: true,
        pointStyle: "rect",
        pointRadius: POINT_RADIUS,
        pointHoverRadius: POINT_RADIUS * 2,
        pointBackgroundColor: "#ffffff",
        pointBorderColor: COLORS.year,
        _marks: yearMarks.marks,
        _texts: yearMarks.texts,
        order: 1,
      },
    ];
  }

  function render(data) {
    if (!window.Chart || !canvas) return;
    var current = data.current || [];
    var yearAgo = data.year_ago || [];
    var axis = buildAxis(current, yearAgo);

    if (legend) legend.hidden = current.length === 0;

    var datasets = buildDatasets(axis, current, yearAgo);

    if (chart) {
      chart.data.labels = axis.labels;
      chart.data.datasets = datasets;
      chart.update("none");
      return;
    }

    chart = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      plugins: [highlightPlugin],
      data: { labels: axis.labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "nearest", intersect: true },
        layout: { padding: { top: 12, right: 14, bottom: 0, left: 0 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              label: function (item) {
                return item.dataset.label + ": " + item.formattedValue + "℃";
              },
            },
          },
        },
        scales: {
          x: {
            grid: { color: COLORS.grid },
            border: { color: COLORS.grid },
            ticks: {
              color: "#333333",
              font: { size: 11 },
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: 12,
            },
          },
          y: {
            grid: { color: COLORS.grid },
            border: { display: false },
            grace: "6%",
            ticks: {
              color: "#333333",
              font: { size: 11 },
              stepSize: 2,
              padding: 6,
            },
          },
        },
      },
    });
  }

  /* 自動更新時にテーブルも描き替える */
  function renderTable(tbodyId, rows, opts) {
    var tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="col-dt empty" colspan="2">' + opts.emptyText + "</td></tr>";
      return;
    }
    var temps = rows.map(function (r) {
      return r.temp;
    });
    var max = Math.max.apply(null, temps);
    var min = Math.min.apply(null, temps);
    tbody.innerHTML = rows
      .map(function (row) {
        var cls = "";
        if (row.temp === max) cls = "is-max";
        else if (row.temp === min) cls = opts.minClass;
        /* "2026-09-29 14:16:00" -> "26/09/29 14:16" */
        var dt = String(row.dt).slice(2, 16).replace(/-/g, "/");
        return (
          '<tr class="' + cls + '"><th class="col-dt" scope="row">' +
          dt +
          "</th><td class=\"col-temp\">" +
          row.temp.toFixed(1) +
          "</td></tr>"
        );
      })
      .join("");
  }

  function renderTables(data) {
    renderTable("table-current", data.current || [], {
      minClass: "is-min",
      emptyText: "該当する記録がありません",
    });
    renderTable("table-year-ago", data.year_ago || [], {
      minClass: "is-year-min",
      emptyText: "1年前の記録がありません",
    });
    if (generatedAt && data.generated_at) {
      generatedAt.textContent = data.generated_at;
      generatedAt.setAttribute("datetime", data.generated_at);
    }
  }

  function refresh() {
    fetch("api/dashboard", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        if (!data || data.ok === false) throw new Error((data && data.error) || "取得に失敗");
        renderTables(data);
        render(data);
        if (reloadState) reloadState.textContent = "";
      })
      .catch(function (err) {
        console.warn("自動更新に失敗しました", err);
        if (reloadState) {
          reloadState.textContent = "更新失敗 " + new Date().toLocaleTimeString("ja-JP");
        }
      });
  }

  renderTables(initial);

  function start() {
    render(initial);
    if (refreshSeconds > 0) setInterval(refresh, refreshSeconds * 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
