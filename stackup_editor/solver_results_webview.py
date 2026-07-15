"""solver_results_webview.py
Replaces the hand-rolled Tkinter canvas results window with a PySide6 QDialog that
hosts a QWebEngineView.  The view renders a self-contained HTML page that uses a
bundled local Plotly.js copy first, with a CDN fallback only when the local bundle
is unavailable.

The page receives one JSON blob – the same dict that field_solver_bridge.run_solver_request()
already returns – via page().runJavaScript().  No changes to the bridge or the Node
subprocess are required.

Dependencies
------------
    pip install PySide6 PySide6-WebEngine          (Qt 6.x)
    # or on conda:  conda install -c conda-forge pyside6

Usage
-----
    from stackup_editor.solver_results_webview import FieldSolverResultsDialog

    dlg = FieldSolverResultsDialog(parent_qwidget_or_none)
    dlg.show()                          # non-blocking
    # later:
    dlg.load_result(result_dict, display_unit="mm")
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from stackup_editor.field_solver_bridge import (
    build_impedance_profile_plot_request,
    run_impedance_profile_plot_request,
)

# ---------------------------------------------------------------------------
# PySide6 imports – grouped so the whole module fails fast with a clear error
# if the package is missing, rather than at the first .show() call.
# ---------------------------------------------------------------------------
try:
    from PySide6.QtCore import (
        QEventLoop,
        QMarginsF,
        QObject,
        QRect,
        QRectF,
        QSize,
        QThread,
        QTimer,
        QUrl,
        Qt,
        Signal,
        Slot,
    )
    from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPageLayout, QPageSize, QPdfWriter, QPixmap
    from PySide6.QtWidgets import (
        QDialog,
        QFileDialog,
        QHBoxLayout,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QVBoxLayout,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtCore import QStandardPaths
    _PYSIDE6_OK = True
except ImportError as _e:
    _PYSIDE6_OK = False
    _PYSIDE6_IMPORT_ERROR = _e


# ---------------------------------------------------------------------------
# Offline Plotly bundle path (relative to this file).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_PLOTLY_LOCAL = _HERE.parent / "js_2d_fields-master" / "src" / "plotly-3.3.0.min.js"
_LOCAL_BASE_URL = QUrl.fromLocalFile(str(_HERE.parent) + os.sep) if _PYSIDE6_OK else None
logger = logging.getLogger(__name__)


def _result_summary(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "result=<none>"
    selected = payload.get("selected_copper")
    if not isinstance(selected, dict):
        selected = {}
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        geometry = {}
    return (
        f"layer={selected.get('label', '<unknown>')} "
        f"tl_type={payload.get('tl_type', '<unknown>')} "
        f"width_mm={geometry.get('trace_width_mm')!r} "
        f"spacing_mm={geometry.get('trace_spacing_mm')!r}"
    )


def _plotly_src_tag() -> str:
    """Return Plotly loader tags that prefer the bundled local copy."""
    cdn = "https://cdn.plot.ly/plotly-2.32.0.min.js"
    if _PLOTLY_LOCAL.exists():
        local_src = _PLOTLY_LOCAL.relative_to(_HERE.parent).as_posix()
        return f"""
<script src="{local_src}"></script>
<script>
(function(){{
    var fallbackStarted = false;

    function signalReady() {{
        window.dispatchEvent(new Event('plotly-loaded'));
    }}

    function signalFailure(source) {{
        window.dispatchEvent(new CustomEvent('plotly-load-failed', {{
            detail: source
        }}));
    }}

    function loadFallback() {{
        if (fallbackStarted) {{
            return;
        }}
        fallbackStarted = true;
        var f = document.createElement('script');
        f.src = '{cdn}';
        f.onload = signalReady;
        f.onerror = function() {{
            signalFailure('cdn');
        }};
        document.head.appendChild(f);
    }}

    if (window.Plotly) {{
        signalReady();
    }} else {{
        window.addEventListener('load', function() {{
            if (window.Plotly) {{
                signalReady();
            }} else {{
                loadFallback();
            }}
        }}, {{ once: true }});
    }}
}})();
</script>"""
    return (
        f'<script src="{cdn}" '
        f'onload="window.dispatchEvent(new Event(\'plotly-loaded\'))" '
        f'onerror="window.dispatchEvent(new CustomEvent(\'plotly-load-failed\', {{detail: \'cdn\'}}))">'
        f'</script>'
    )


if _PYSIDE6_OK:
    class _ImpedanceProfilePlotWorker(QObject):
        finished = Signal(int, object)
        failed = Signal(int, str)

        def __init__(self, *, token: int, root_path: Path, plot_request: dict[str, Any]) -> None:
            super().__init__()
            self._token = token
            self._root_path = root_path
            self._plot_request = plot_request

        @Slot()
        def run(self) -> None:
            logger.info("Impedance profile plot worker started token=%s", self._token)
            try:
                result = run_impedance_profile_plot_request(self._root_path, self._plot_request)
            except Exception as exc:  # pragma: no cover - UI-thread surface only
                logger.exception("Impedance profile plot worker failed token=%s", self._token)
                self.failed.emit(self._token, str(exc))
                return
            logger.info("Impedance profile plot worker finished token=%s", self._token)
            self.finished.emit(self._token, result)


    class _PlotResultRelay(QObject):
        def __init__(self, owner) -> None:
            super().__init__()
            self._owner = owner

        @Slot(int, object)
        def deliver_finished(self, token: int, plot_state: object) -> None:
            self._owner._on_plot_job_finished(token, plot_state)

        @Slot(int, str)
        def deliver_failed(self, token: int, message: str) -> None:
            self._owner._on_plot_job_failed(token, message)
else:  # pragma: no cover - only used when Qt dependencies are unavailable
    class _ImpedanceProfilePlotWorker:
        def __init__(self, *args, **kwargs) -> None:
            raise ImportError(
                "PySide6 and PySide6-WebEngine are required for the Qt results window.\n"
                f"Original error: {_PYSIDE6_IMPORT_ERROR}"
            )


    class _PlotResultRelay:
        def __init__(self, *args, **kwargs) -> None:
            raise ImportError(
                "PySide6 and PySide6-WebEngine are required for the Qt results window.\n"
                f"Original error: {_PYSIDE6_IMPORT_ERROR}"
            )


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Field Solver Results</title>
{plotly_script}
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#1e1e1e;color:#d4d4d4;font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;height:100vh;overflow:hidden}}
#app-shell{{height:100vh;display:flex;flex-direction:column}}
#header{{padding:10px 16px 6px;border-bottom:1px solid #333;flex-shrink:0}}
#header h1{{font-size:15px;font-weight:600;color:#e8e8e8}}
#header p{{font-size:11px;color:#888;margin-top:2px}}
#tabs{{display:flex;gap:0;border-bottom:1px solid #333;flex-shrink:0;background:#252526}}
.tab-btn{{padding:7px 18px;border:none;background:none;color:#888;cursor:pointer;font-size:12px;border-bottom:2px solid transparent;transition:color .15s}}
.tab-btn:hover{{color:#ccc}}
.tab-btn.active{{color:#4ec9b0;border-bottom-color:#4ec9b0}}
#content{{flex:1;overflow:hidden;position:relative}}
.tab-pane{{display:none;position:absolute;inset:0;overflow:auto;padding:12px 14px}}
.tab-pane.active{{display:block}}
/* Summary */
#summary-text{{background:#1a1a1a;border:1px solid #333;border-radius:4px;padding:12px;font-family:'Consolas','Courier New',monospace;font-size:12px;white-space:pre;color:#c8c8c8;overflow:auto;height:100%}}
/* Plot panes */
#plot-controls{{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:0 0 10px;flex-wrap:wrap}}
#plot-controls-left{{display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
#plot-controls label{{color:#9d9d9d;font-size:12px}}
#plot-controls select{{background:#2d2d2d;color:#ccc;border:1px solid #444;border-radius:3px;padding:3px 6px;font-size:12px}}
#plot-note{{color:#7fa3c5;font-size:11px}}
#plots-grid{{display:flex;align-items:stretch;gap:0;height:calc(100% - 48px);min-height:0}}
.plot-panel{{min-width:0;min-height:0;height:100%;background:#181818;border:1px solid #303030;border-radius:6px;padding:8px}}
#plot-panel-left{{flex:0 0 calc(50% - 6px)}}
#plot-panel-right{{flex:1 1 calc(50% - 6px)}}
#plot-splitter{{flex:0 0 12px;position:relative;cursor:col-resize;display:flex;align-items:stretch;justify-content:center}}
#plot-splitter::before{{content:'';width:4px;border-radius:999px;background:#2a3b4a;transition:background .15s ease}}
#plot-splitter:hover::before,#plot-splitter.dragging::before{{background:#4ec9b0}}
.plot-canvas{{width:100%;height:100%}}
/* Field controls */
#field-controls{{display:flex;align-items:center;gap:14px;padding:0 0 10px;flex-shrink:0}}
#field-controls label{{color:#9d9d9d;font-size:12px}}
#field-controls select{{background:#2d2d2d;color:#ccc;border:1px solid #444;border-radius:3px;padding:3px 6px;font-size:12px}}
#field-wrap{{height:calc(100% - 38px)}}
/* State message */
#state-msg{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#555;font-size:14px;pointer-events:none}}
#print-report{{display:none}}
.report-header h1{{font-size:20px;color:#17222b;margin-bottom:4px}}
.report-header p{{font-size:10px;color:#51616f}}
.report-summary{{margin-top:18px}}
.report-summary h2,.report-graphics h2{{font-size:14px;color:#17222b;margin-bottom:8px}}
.report-summary pre{{background:#f5f6f7;border:1px solid #d6dbe1;border-radius:6px;padding:12px;font-family:'Consolas','Courier New',monospace;font-size:10px;white-space:pre-wrap;color:#1e252b}}
.report-graphics{{margin-top:18px}}
.report-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.report-card{{break-inside:avoid-page;background:#fff}}
.report-card.wide{{grid-column:1 / -1}}
.report-card h3{{font-size:12px;color:#17222b;margin-bottom:6px}}
.report-card img{{display:block;width:100%;border:1px solid #d6dbe1;border-radius:6px;background:#101010}}
body.printing{{background:#fff;color:#111;overflow:visible;height:auto}}
body.printing #app-shell{{display:none}}
body.printing #print-report{{display:block;padding:20px 18px}}
@page{{size:A4 portrait;margin:12mm}}
</style>
</head>
<body>
<div id="app-shell">
<div id="header">
  <h1 id="hdr-title">Field Solver Results</h1>
  <p  id="hdr-sub">Waiting for result…</p>
</div>
<div id="tabs">
  <button class="tab-btn active" data-tab="summary">Summary</button>
  <button class="tab-btn"        data-tab="plots">Plots</button>
  <button class="tab-btn"        data-tab="geometry">Geometry</button>
  <button class="tab-btn"        data-tab="field">Field View</button>
</div>
<div id="content">
  <div id="state-msg">No result loaded yet.</div>

  <div class="tab-pane active" id="pane-summary">
    <div id="summary-text"></div>
  </div>

  <div class="tab-pane" id="pane-plots">
    <div id="plot-controls">
      <div id="plot-controls-left">
        <label>Loss unit
          <select id="sel-loss-unit">
            <option value="db_per_mil" selected>dB/mil</option>
            <option value="db_per_mm">dB/mm</option>
            <option value="db_per_inch">dB/inch</option>
          </select>
        </label>
      </div>
      <div id="plot-note"></div>
    </div>
    <div id="plots-grid">
      <div class="plot-panel" id="plot-panel-left">
        <div id="plot-impedance" class="plot-canvas"></div>
      </div>
      <div id="plot-splitter" aria-label="Resize plots"></div>
      <div class="plot-panel" id="plot-panel-right">
        <div id="plot-loss" class="plot-canvas"></div>
      </div>
    </div>
  </div>

  <div class="tab-pane" id="pane-geometry">
    <div id="plot-geometry" style="height:100%"></div>
  </div>

  <div class="tab-pane" id="pane-field">
    <div id="field-controls">
      <label>Quantity
        <select id="sel-quantity">
          <option value="field_magnitude">|E| Field</option>
          <option value="potential">Potential</option>
        </select>
      </label>
      <label>Mode
        <select id="sel-mode"></select>
      </label>
      <label style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="chk-contours" checked> Contour lines
      </label>
    </div>
    <div id="field-wrap">
      <div id="plot-field" style="height:100%"></div>
    </div>
  </div>
</div>
<div id="print-report"></div>
</div>

<script>
// ── Tab switching ────────────────────────────────────────────────────────────
function activateTab(tabName) {{
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  }});
  document.querySelectorAll('.tab-pane').forEach(pane => {{
    pane.classList.toggle('active', pane.id === 'pane-' + tabName);
  }});
  setTimeout(() => window.dispatchEvent(new Event('resize')), 50);
}}

function currentTab() {{
  const active = document.querySelector('.tab-btn.active');
  return active ? active.dataset.tab : 'summary';
}}

document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
}});

// ── Dark layout defaults for Plotly ─────────────────────────────────────────
const DARK = {{
  paper_bgcolor: '#1e1e1e',
  plot_bgcolor:  '#141414',
  font:          {{ color: '#ccc', size: 11 }},
  xaxis: {{ color:'#777', gridcolor:'#2e2e2e', zerolinecolor:'#333' }},
  yaxis: {{ color:'#777', gridcolor:'#2e2e2e', zerolinecolor:'#333' }},
  margin: {{ l:60, r:24, t:36, b:48 }},
}};
const CFG = {{
  responsive:true,
  displayModeBar:true,
  scrollZoom:true,
  doubleClick:'reset',
  modeBarButtonsToRemove:['autoScale2d'],
  toImageButtonOptions: {{
    format:'png',
    filename:'field_solver_plot',
    scale:2
  }}
}};

// Fire colorscale (log-normalised)
const FIRE_CS = [
  [0.00,'#0d0221'],[0.06,'#160b35'],[0.13,'#2a1267'],
  [0.21,'#47108a'],[0.29,'#6c16a3'],[0.37,'#9627b0'],
  [0.45,'#c03baf'],[0.53,'#e0579f'],[0.61,'#f57c6b'],
  [0.70,'#faa94d'],[0.80,'#fdd26a'],[0.89,'#fef0a0'],
  [1.00,'#ffffff']
];

// ── State ────────────────────────────────────────────────────────────────────
let _result = null;
let _unit   = 'mm';
let _lossUnit = 'db_per_mil';
let _plotSplitterReady = false;
let _plotLeftRatio = 0.5;
window._reportReady = false;
window._reportError = '';

const LOSS_UNITS = {{
  db_per_mm:   {{ label:'dB/mm', factor:1 / 1000 }},
  db_per_mil:  {{ label:'dB/mil', factor:0.0254 / 1000 }},
  db_per_inch: {{ label:'dB/inch', factor:0.0254 }},
}};

function showStateMessage(message) {{
  const state = document.getElementById('state-msg');
  state.textContent = message;
  state.style.display = 'flex';
}}

function hideStateMessage() {{
  document.getElementById('state-msg').style.display = 'none';
}}

function convertLossFromDbPerM(value, unitKey) {{
  const meta = LOSS_UNITS[unitKey] || LOSS_UNITS.db_per_mil;
  return (value || 0) * meta.factor;
}}

function fmtLossSummary(value) {{
  const dbPerMil = convertLossFromDbPerM(value, 'db_per_mil');
  const dbPerMm = convertLossFromDbPerM(value, 'db_per_mm');
  return dbPerMil.toFixed(6) + ' dB/mil (' + dbPerMm.toFixed(4) + ' dB/mm)';
}}

function fmtFreqGhz(value) {{
  const num = Number(value || 0);
  if (!Number.isFinite(num) || num <= 0) {{
    return '0 GHz';
  }}
  if (num >= 10) {{
    return num.toFixed(2) + ' GHz';
  }}
  if (num >= 1) {{
    return num.toFixed(3) + ' GHz';
  }}
  return num.toFixed(4) + ' GHz';
}}

function lossSeriesMap() {{
  const sweep = (_result && _result.sweep) || {{}};
  const plotData = sweep.plot_data || {{}};
  const series = plotData.loss || [];
  const map = {{}};
  (series || []).forEach(item => {{
    if (item && item.label) {{
      map[item.label] = item.values || [];
    }}
  }});
  return map;
}}

function lossValueAt(label, index) {{
  const values = lossSeriesMap()[label] || [];
  const value = values[index];
  return fmtLossSummary(value || 0);
}}

function finiteValues(values) {{
  return (values || []).filter(value => Number.isFinite(value));
}}

function numericAxisRange(values, options = {{}}) {{
  const includeZero = Boolean(options.includeZero);
  const padRatio = options.padRatio ?? 0.06;
  const minSpan = options.minSpan ?? 1e-6;
  const numbers = finiteValues(values);
  if (!numbers.length) {{
    return null;
  }}

  let min = Math.min(...numbers);
  let max = Math.max(...numbers);
  if (includeZero) {{
    min = Math.min(0, min);
    max = Math.max(0, max);
  }}

  let span = max - min;
  if (span < minSpan) {{
    const center = (min + max) / 2;
    const half = Math.max(minSpan / 2, Math.abs(center) * padRatio, 1e-6);
    return [center - half, center + half];
  }}

  const pad = span * padRatio;
  return [min - pad, max + pad];
}}

function impedanceAxisRange(targetValue, selectedValue) {{
  const target = Number(targetValue || 0);
  if (Number.isFinite(target) && target > 0) {{
    const band = Math.max(4.0, target * 0.18);
    return [Math.max(0, target - band), target];
  }}

  const selected = Number(selectedValue || 0);
  if (Number.isFinite(selected) && selected > 0) {{
    const band = Math.max(5.0, selected * 0.12);
    return [selected - band, selected + band];
  }}

  return [40, 120];
}}

function differentialWidthAxisMax(profilePlot, selectedWidthValue) {{
  const effectiveMax = Number((profilePlot && profilePlot.effective_max_width_mil) || 0);
  const selected = Number(selectedWidthValue || 0);
  const widths = (profilePlot && profilePlot.widths_mil) || [];
  const lastWidth = widths.length ? Number(widths[widths.length - 1] || 0) : 0;
  const upper = Math.max(2, effectiveMax, lastWidth, selected);
  return Math.min(30, upper);
}}

function differentialContourWidthMax(targetValue, widths, matrix) {{
  const target = Number(targetValue || 0);
  if (!Number.isFinite(target) || target <= 0 || !Array.isArray(widths) || widths.length < 2 || !Array.isArray(matrix)) {{
    return null;
  }}

  let maxCrossingWidth = null;
  const columnCount = Math.max(0, ...matrix.map((row) => Array.isArray(row) ? row.length : 0));
  for (let columnIndex = 0; columnIndex < columnCount; columnIndex += 1) {{
    for (let rowIndex = 0; rowIndex < widths.length - 1; rowIndex += 1) {{
      const z0 = Number(matrix?.[rowIndex]?.[columnIndex]);
      const z1 = Number(matrix?.[rowIndex + 1]?.[columnIndex]);
      const w0 = Number(widths[rowIndex]);
      const w1 = Number(widths[rowIndex + 1]);
      if (!Number.isFinite(z0) || !Number.isFinite(z1) || !Number.isFinite(w0) || !Number.isFinite(w1)) {{
        continue;
      }}
      if (z0 === target) {{
        maxCrossingWidth = maxCrossingWidth === null ? w0 : Math.max(maxCrossingWidth, w0);
        continue;
      }}
      if (z1 === target) {{
        maxCrossingWidth = maxCrossingWidth === null ? w1 : Math.max(maxCrossingWidth, w1);
        continue;
      }}
      const crosses = (z0 - target) * (z1 - target) < 0;
      if (!crosses || z1 === z0) {{
        continue;
      }}
      const t = (target - z0) / (z1 - z0);
      const crossingWidth = w0 + ((w1 - w0) * t);
      if (Number.isFinite(crossingWidth)) {{
        maxCrossingWidth = maxCrossingWidth === null ? crossingWidth : Math.max(maxCrossingWidth, crossingWidth);
      }}
    }}
  }}
  return maxCrossingWidth;
}}

function differentialWidthAxisRange(profilePlot, targetValue, selectedWidthValue) {{
  const selected = Number(selectedWidthValue || 0);
  const widths = (profilePlot && profilePlot.widths_mil) || [];
  const matrix = (profilePlot && profilePlot.impedance_matrix_ohm) || [];
  const contourMax = differentialContourWidthMax(targetValue, widths, matrix);
  if (Number.isFinite(contourMax)) {{
    const paddedMax = Math.ceil(Math.max(selected, contourMax) + 0.75);
    return [2, Math.max(3, Math.min(30, paddedMax))];
  }}
  return [2, differentialWidthAxisMax(profilePlot, selected)];
}}

function linspace(start, stop, count) {{
  if (count <= 1) {{
    return [start];
  }}
  const values = [];
  for (let index = 0; index < count; index += 1) {{
    values.push(start + ((stop - start) * index) / (count - 1));
  }}
  return values;
}}

function interpolateLinearSeries(xValues, yValues, outCount = 81) {{
  const xs = (xValues || []).map(Number);
  const ys = (yValues || []).map(Number);
  if (xs.length !== ys.length || xs.length < 2) {{
    return {{ x: xs, y: ys }};
  }}
  const denseX = linspace(xs[0], xs[xs.length - 1], outCount);
  const denseY = denseX.map((x) => {{
    let index = 0;
    while (index < xs.length - 2 && x > xs[index + 1]) {{
      index += 1;
    }}
    const x0 = xs[index];
    const x1 = xs[index + 1];
    const y0 = ys[index];
    const y1 = ys[index + 1];
    if (!Number.isFinite(x0) || !Number.isFinite(x1) || !Number.isFinite(y0) || !Number.isFinite(y1) || x1 === x0) {{
      return Number.isFinite(y0) ? y0 : y1;
    }}
    const t = (x - x0) / (x1 - x0);
    return y0 + ((y1 - y0) * t);
  }});
  return {{ x: denseX, y: denseY }};
}}

function locateInterval(values, target) {{
  if (!Array.isArray(values) || values.length < 2) {{
    return {{ index: 0, t: 0 }};
  }}
  if (target <= values[0]) {{
    return {{ index: 0, t: 0 }};
  }}
  if (target >= values[values.length - 1]) {{
    return {{ index: values.length - 2, t: 1 }};
  }}
  let index = 0;
  while (index < values.length - 2 && target > values[index + 1]) {{
    index += 1;
  }}
  const v0 = values[index];
  const v1 = values[index + 1];
  if (!Number.isFinite(v0) || !Number.isFinite(v1) || v1 === v0) {{
    return {{ index, t: 0 }};
  }}
  return {{ index, t: (target - v0) / (v1 - v0) }};
}}

function weightedCornerValue(z00, z10, z01, z11, tx, ty) {{
  const corners = [
    {{ value: z00, weight: (1 - tx) * (1 - ty) }},
    {{ value: z10, weight: tx * (1 - ty) }},
    {{ value: z01, weight: (1 - tx) * ty }},
    {{ value: z11, weight: tx * ty }},
  ];
  let weightedSum = 0;
  let weightSum = 0;
  corners.forEach((corner) => {{
    if (Number.isFinite(corner.value) && corner.weight > 0) {{
      weightedSum += corner.value * corner.weight;
      weightSum += corner.weight;
    }}
  }});
  if (weightSum > 0) {{
    return weightedSum / weightSum;
  }}
  const finite = corners.map((corner) => corner.value).filter((value) => Number.isFinite(value));
  if (!finite.length) {{
    return null;
  }}
  return finite.reduce((sum, value) => sum + value, 0) / finite.length;
}}

function interpolateBilinearGrid(xValues, yValues, zMatrix, outXCount = 41, outYCount = 41) {{
  const xs = (xValues || []).map(Number);
  const ys = (yValues || []).map(Number);
  if (xs.length < 2 || ys.length < 2) {{
    return {{ x: xs, y: ys, z: zMatrix || [] }};
  }}
  const denseX = linspace(xs[0], xs[xs.length - 1], outXCount);
  const denseY = linspace(ys[0], ys[ys.length - 1], outYCount);
  const denseZ = denseY.map((y) => {{
    const yLoc = locateInterval(ys, y);
    return denseX.map((x) => {{
      const xLoc = locateInterval(xs, x);
      const z00 = zMatrix?.[yLoc.index]?.[xLoc.index];
      const z10 = zMatrix?.[yLoc.index]?.[xLoc.index + 1];
      const z01 = zMatrix?.[yLoc.index + 1]?.[xLoc.index];
      const z11 = zMatrix?.[yLoc.index + 1]?.[xLoc.index + 1];
      return weightedCornerValue(z00, z10, z01, z11, xLoc.t, yLoc.t);
    }});
  }});
  return {{ x: denseX, y: denseY, z: denseZ }};
}}

function edgeAxisRange(minValue, maxValue, options = {{}}) {{
  if (!Number.isFinite(minValue) || !Number.isFinite(maxValue)) {{
    return null;
  }}
  const padRatio = options.padRatio ?? 0.05;
  const minSpan = options.minSpan ?? 1e-3;
  let span = maxValue - minValue;
  if (span < minSpan) {{
    const center = (minValue + maxValue) / 2;
    const half = minSpan / 2;
    return [center - half, center + half];
  }}
  const pad = Math.max(span * padRatio, minSpan * 0.5);
  return [minValue - pad, maxValue + pad];
}}

function logFrequencyRange(freqs) {{
  const numbers = finiteValues(freqs).filter(value => value > 0);
  if (!numbers.length) {{
    return null;
  }}
  const min = Math.min(...numbers);
  const max = Math.max(...numbers);
  if (min === max) {{
    const center = Math.log10(min);
    return [center - 0.25, center + 0.25];
  }}
  return [Math.log10(min), Math.log10(max)];
}}

function waitMs(ms) {{
  return new Promise(resolve => setTimeout(resolve, ms));
}}

function resizePlotCanvases() {{
  if (!window.Plotly) {{
    return;
  }}
  ['plot-impedance', 'plot-loss'].forEach((id) => {{
    const node = document.getElementById(id);
    if (node) {{
      Plotly.Plots.resize(node);
    }}
  }});
}}

function applyPlotSplitterRatio(ratio) {{
  const leftPanel = document.getElementById('plot-panel-left');
  const rightPanel = document.getElementById('plot-panel-right');
  if (!leftPanel || !rightPanel) {{
    return;
  }}
  _plotLeftRatio = Math.max(0.30, Math.min(0.70, Number(ratio) || 0.5));
  const leftPercent = (_plotLeftRatio * 100).toFixed(3);
  const rightPercent = ((1 - _plotLeftRatio) * 100).toFixed(3);
  leftPanel.style.flex = '0 0 calc(' + leftPercent + '% - 6px)';
  rightPanel.style.flex = '1 1 calc(' + rightPercent + '% - 6px)';
  window.requestAnimationFrame(resizePlotCanvases);
}}

function setupPlotSplitter() {{
  if (_plotSplitterReady) {{
    return;
  }}
  const grid = document.getElementById('plots-grid');
  const splitter = document.getElementById('plot-splitter');
  if (!grid || !splitter) {{
    return;
  }}

  let dragging = false;

  function updateFromClientX(clientX) {{
    const rect = grid.getBoundingClientRect();
    const splitterWidth = splitter.getBoundingClientRect().width || 12;
    const usableWidth = rect.width - splitterWidth;
    if (usableWidth <= 60) {{
      return;
    }}
    const leftWidth = clientX - rect.left - (splitterWidth / 2);
    applyPlotSplitterRatio(leftWidth / usableWidth);
  }}

  splitter.addEventListener('pointerdown', (event) => {{
    dragging = true;
    splitter.classList.add('dragging');
    splitter.setPointerCapture(event.pointerId);
    updateFromClientX(event.clientX);
    event.preventDefault();
  }});

  splitter.addEventListener('pointermove', (event) => {{
    if (!dragging) {{
      return;
    }}
    updateFromClientX(event.clientX);
    event.preventDefault();
  }});

  function stopDragging(event) {{
    if (!dragging) {{
      return;
    }}
    dragging = false;
    splitter.classList.remove('dragging');
    try {{
      splitter.releasePointerCapture(event.pointerId);
    }} catch (_error) {{}}
    window.requestAnimationFrame(resizePlotCanvases);
  }}

  splitter.addEventListener('pointerup', stopDragging);
  splitter.addEventListener('pointercancel', stopDragging);
  window.addEventListener('resize', resizePlotCanvases);

  _plotSplitterReady = true;
  applyPlotSplitterRatio(0.5);
}}

function setPlotPaneMode(isDifferential) {{
  const leftPanel = document.getElementById('plot-panel-left');
  const rightPanel = document.getElementById('plot-panel-right');
  const splitter = document.getElementById('plot-splitter');
  if (!leftPanel || !rightPanel || !splitter) {{
    return;
  }}

  if (isDifferential) {{
    leftPanel.style.display = '';
    splitter.style.display = 'flex';
    applyPlotSplitterRatio(_plotLeftRatio);
  }} else {{
    leftPanel.style.display = 'none';
    splitter.style.display = 'none';
    rightPanel.style.flex = '1 1 100%';
    window.requestAnimationFrame(resizePlotCanvases);
  }}
}}

function escapeHtml(text) {{
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}}

// ── Entry point called from Python via runJavaScript ─────────────────────────
window.loadResult = function(resultJson, unit) {{
  _result = JSON.parse(resultJson);
  _unit   = unit || 'mm';
  window._reportReady = false;
  window._reportError = '';
  setupPlotSplitter();

  const lossSelect = document.getElementById('sel-loss-unit');
  if (lossSelect) {{
    lossSelect.value = _lossUnit;
    lossSelect.onchange = () => {{
      _lossUnit = lossSelect.value || 'db_per_mil';
      buildPlots();
    }};
  }}

  // Header
  const copper = _result.selected_copper;
  const model  = (_result.tl_type || '').replace(/_/g,' ');
  document.getElementById('hdr-title').textContent =
    'Field Solver Results | ' + copper.label + ' | ' + model;
  const ref = _result.reference || {{}};
  const mesh = _result.mesh || {{}};
  document.getElementById('hdr-sub').textContent =
    copper.label + ' ' + copper.copper_type +
    ' | Ref ' + (ref.freq_ghz||0).toFixed(3) + ' GHz' +
    ' | Mesh ' + (mesh.nx||0) + 'x' + (mesh.ny||0);

  if (!window.Plotly) {{
    showStateMessage('Loading plots...');
  }} else {{
    hideStateMessage();
  }}

  buildSummary();
  buildPlots();
  buildGeometry();
  buildFieldControls();
  buildField();
}};

window.updateImpedanceProfilePlot = function(plotJson) {{
  if (!_result) {{
    return;
  }}
  _result.impedance_profile_plot = JSON.parse(plotJson);
  buildPlots();
}};

// ── Summary ──────────────────────────────────────────────────────────────────
function buildSummary() {{
  const r = _result;
  const geo   = r.geometry || {{}};
  const solved = r.solved  || {{}};
  const ref    = r.reference || {{}};
  const sweep  = r.sweep  || {{}};
  const vis    = r.visualization || {{}};
  const domain = vis.domain || {{}};
  const mesh   = r.mesh  || {{}};
  const copper = r.selected_copper;

  function fmtLen(mm) {{
    if (_unit === 'mil') return (mm / 0.0254).toFixed(2) + ' mil';
    if (_unit === 'um')  return (mm * 1000).toFixed(1) + ' um';
    if (_unit === 'inch') return (mm / 25.4).toFixed(4) + ' in';
    return mm.toFixed(4) + ' mm';
  }}

  let lines = [
    'FIELD SOLVER RESULT',
    '',
    'Selected copper : ' + copper.label,
    'Model           : ' + (r.tl_type||'').replace(/_/g,' '),
    'Copper type     : ' + copper.copper_type,
    'Roughness       : ' + (copper.roughness_um||0).toFixed(2) + ' um',
    'Trace width     : ' + fmtLen(geo.trace_width_mm||0),
    'Trace spacing   : ' + fmtLen(geo.trace_spacing_mm||0),
    'Copper thickness: ' + fmtLen(copper.thickness_mm||0),
    'Reference freq  : ' + (ref.freq_ghz||0).toFixed(3) + ' GHz',
  ];

  function addSide(title, side) {{
    lines.push('', title,
      'Thickness       : ' + fmtLen(side.total_thickness_mm||0),
      'Effective Dk    : ' + (side.effective_dk||0).toFixed(4),
      'Average Df      : ' + (side.average_df||0).toFixed(5),
      'Average freq    : ' + (side.average_freq_ghz||0).toFixed(3) + ' GHz',
      'Layer count     : ' + (side.layer_count||0)
    );
  }}
  if (geo.substrate) addSide('BOTTOM SIDE', geo.substrate);
  if (geo.top_side)  addSide('TOP SIDE',    geo.top_side);

  lines.push('', 'REFERENCE RESULT');
  if (r.is_differential) {{
    lines.push(
      'Zdiff           : ' + (solved.z_diff_ohm||0).toFixed(2) + ' ohm',
      'Zcommon         : ' + (solved.z_common_ohm||0).toFixed(2) + ' ohm',
      'Zodd            : ' + (solved.z_odd_ohm||0).toFixed(2) + ' ohm',
      'Zeven           : ' + (solved.z_even_ohm||0).toFixed(2) + ' ohm',
      'Eps_eff odd     : ' + (solved.eps_eff_odd||0).toFixed(4),
      'Eps_eff even    : ' + (solved.eps_eff_even||0).toFixed(4)
    );
  }} else {{
    lines.push(
      'Z0              : ' + (solved.z0_ohm||0).toFixed(2) + ' ohm',
      'Eps_eff         : ' + (solved.eps_eff||0).toFixed(4),
      '', 'RLGC', fmtRLGC(solved.rlgc||{{}})
    );
  }}

  lines.push(
    '', 'LOSS @ REFERENCE',
    'Conductor loss  : ' + fmtLossSummary(solved.alpha_c_db_per_m||0),
    'Dielectric loss : ' + fmtLossSummary(solved.alpha_d_db_per_m||0),
    'Total loss      : ' + fmtLossSummary(solved.alpha_total_db_per_m||0)
  );

  if (sweep.exact_samples) {{
    const st = sweep.start||{{}}, sp = sweep.stop||{{}};
    lines.push('', 'SWEEP',
      'Exact samples   : ' + sweep.exact_samples,
      'Start           : ' + fmtLossSummary(st.alpha_total_db_per_m||0) + ' @ ' + (st.freq_ghz||0).toFixed(2) + ' GHz',
      'Stop            : ' + fmtLossSummary(sp.alpha_total_db_per_m||0) + ' @ ' + (sp.freq_ghz||0).toFixed(2) + ' GHz'
    );
    const plotData = (sweep.plot_data || {{}}).loss || [];
    if (plotData.length) {{
      const lastIndex = Math.max(0, (((sweep.plot_data || {{}}).frequencies_ghz || []).length || 1) - 1);
      lines.push('', 'SWEEP COMPONENTS @ STOP');
      (plotData || []).forEach((series) => {{
        lines.push((series.label || 'Series') + ' : ' + lossValueAt(series.label || '', lastIndex));
      }});
    }}
  }}

  if (domain.width_m) {{
    lines.push('', 'VISUALIZATION',
      'Domain width    : ' + fmtLen((domain.width_m||0)*1000),
      'Domain height   : ' + fmtLen((domain.height_m||0)*1000),
      'Mesh            : ' + (mesh.nx||0) + 'x' + (mesh.ny||0)
    );
  }}

  document.getElementById('summary-text').textContent = lines.join('\\n');
}}

function fmtRLGC(rlgc) {{
  return [
    'R = ' + (rlgc.R||0).toFixed(3) + ' ohm/m',
    'L = ' + (rlgc.L||0).toExponential(3) + ' H/m',
    'G = ' + (rlgc.G||0).toExponential(3) + ' S/m',
    'C = ' + (rlgc.C||0).toExponential(3) + ' F/m'
  ].join('\\n');
}}

// ── Frequency sweep plots ────────────────────────────────────────────────────
function buildPlots() {{
  if (!window.Plotly) {{ setTimeout(buildPlots, 150); return; }}
  const pd = (_result.sweep||{{}}).plot_data || {{}};
  const freqs = pd.frequencies_ghz || [];
  if (!freqs.length) return;
  const isDifferential = Boolean(_result.is_differential);
  setPlotPaneMode(isDifferential);
  const lossMeta = LOSS_UNITS[_lossUnit] || LOSS_UNITS.db_per_mil;
  const configuredLossAxisDbPerM = (((_result.ui || {{}}).loss_axis_max_db_per_m) || 0);
  const profilePlot = _result.impedance_profile_plot || null;
  const targetImpedance = Number((_result.target_impedance_ohm || 0));
  const plotNote = document.getElementById('plot-note');
  const xRange = logFrequencyRange(freqs);
  const lossRange = configuredLossAxisDbPerM > 0
    ? [0, convertLossFromDbPerM(configuredLossAxisDbPerM, _lossUnit)]
    : numericAxisRange((pd.loss || []).flatMap(series => (series.values || []).map(value => convertLossFromDbPerM(value, _lossUnit))), {{
        includeZero: true,
        padRatio: 0.05,
        minSpan: 1e-6,
      }});

  const COLORS = ['#4ec9b0','#ce9178','#9cdcfe','#c586c0','#dcdcaa'];
  function makeSeries(seriesList, quantity) {{
    return (seriesList||[]).map((s,i) => ({{
      x: freqs, y: s.values||[],
      name: s.label||('Series '+(i+1)),
      type:'scatter', mode: freqs.length===1?'markers':'lines+markers',
      line:{{color:COLORS[i%COLORS.length], width:1.8}},
      marker:{{color:COLORS[i%COLORS.length], size:4}}
    }})).map(series => {{
      if (quantity === 'loss') {{
        series.y = (series.y || []).map(value => convertLossFromDbPerM(value, _lossUnit));
      }}
      return series;
    }});
  }}

  function layout(title, xLabel, yLabel, options = {{}}) {{
    const result = Object.assign({{}}, DARK, {{
      title:{{text:title, font:{{size:13,color:'#e8e8e8'}}}},
      xaxis: Object.assign({{}}, DARK.xaxis, {{
        title:xLabel,
        type: options.xType || 'linear'
      }}),
      yaxis: Object.assign({{}}, DARK.yaxis, {{title:yLabel}}),
      showlegend:true,
      legend:{{x:0.02,y:0.98,font:{{color:'#ccc',size:10}}}},
      annotations: options.annotations || [],
      shapes: options.shapes || []
    }});
    if (options.xRange) {{
      result.xaxis = Object.assign({{}}, result.xaxis, {{
        autorange: false,
        range: options.xRange,
      }});
    }}
    if (options.yRange) {{
      result.yaxis = Object.assign({{}}, result.yaxis, {{
        autorange: false,
        range: options.yRange,
      }});
    }}
    return result;
  }}

  Plotly.react(
    'plot-loss',
    makeSeries(pd.loss, 'loss'),
    layout('Loss (' + lossMeta.label + ')', 'Frequency', 'Loss', {{
      xType: 'log',
      xRange,
      yRange: lossRange,
    }}),
    CFG
  );

  const referenceFreqGhz = ((_result.reference || {{}}).freq_ghz) || 0;
  if (plotNote) {{
    if (!isDifferential) {{
      plotNote.textContent = '';
    }} else if (profilePlot && profilePlot.ready) {{
      if (profilePlot.kind === 'differential') {{
        const widthCount = Array.isArray(profilePlot.widths_mil) ? profilePlot.widths_mil.length : 0;
        const gapCount = Array.isArray(profilePlot.gaps_mil) ? profilePlot.gaps_mil.length : 0;
        plotNote.textContent =
          'Target contour only | ' + widthCount + ' x ' + gapCount + ' field-solver samples, interpolated display @ ' +
          fmtFreqGhz(profilePlot.target_freq_ghz || referenceFreqGhz);
      }} else {{
        const widthCount = Array.isArray(profilePlot.widths_mil) ? profilePlot.widths_mil.length : 0;
        plotNote.textContent =
          'Target-focused view | ' + widthCount + ' field-solver samples, interpolated display @ ' +
          fmtFreqGhz(profilePlot.target_freq_ghz || referenceFreqGhz);
      }}
    }} else if (profilePlot && profilePlot.status === 'failed') {{
      plotNote.textContent = profilePlot.message || 'Impedance design plot could not be built.';
    }} else {{
      plotNote.textContent = 'Building fixed-frequency impedance design plot @ ' + fmtFreqGhz(referenceFreqGhz);
    }}
  }}

  if (!isDifferential) {{
    return;
  }}

  function placeholderLayout(title, xTitle, yTitle, xPlotRange, yPlotRange, message) {{
    return layout(title, xTitle, yTitle, {{
      xRange: xPlotRange,
      yRange: yPlotRange,
      annotations: [{{
        xref: 'paper',
        yref: 'paper',
        x: 0.5,
        y: 0.5,
        text: message,
        showarrow: false,
        font: {{ color: '#8fa9bf', size: 14 }},
      }}],
    }});
  }}

  function buildSingleEndedImpedancePlot() {{
    const widths = (profilePlot && profilePlot.widths_mil) || [];
    const values = (profilePlot && profilePlot.impedances_ohm) || [];
    const interpolated = interpolateLinearSeries(widths, values, 81);
    const selectedWidth = Number((profilePlot && profilePlot.selected_width_mil) || 0);
    const selectedZ0 = Number(((_result.solved || {{}}).z0_ohm) || 0);
    const yRange = impedanceAxisRange(targetImpedance, selectedZ0);
    const traces = [{{
      x: interpolated.x,
      y: interpolated.y,
      name: 'Z0',
      type: 'scatter',
      mode: 'lines',
      line: {{ color: '#4ec9b0', width: 2 }},
      hovertemplate: 'Width %{{x:.2f}} mil<br>Z0 %{{y:.2f}} ohm<extra></extra>',
    }}];
    if (selectedWidth > 0 && Number.isFinite(selectedZ0) && selectedZ0 > 0) {{
      traces.push({{
        x: [selectedWidth],
        y: [selectedZ0],
        name: 'Selected',
        type: 'scatter',
        mode: 'markers',
        marker: {{ color: '#d06145', size: 11, line: {{ color: '#ffe8e2', width: 1.5 }} }},
        hovertemplate: 'Selected width %{{x:.2f}} mil<br>Z0 %{{y:.2f}} ohm<extra></extra>',
      }});
    }}
    Plotly.react(
      'plot-impedance',
      traces,
      layout(
        'Trace Width vs Z0 @ ' + fmtFreqGhz((profilePlot && profilePlot.target_freq_ghz) || referenceFreqGhz),
        'Trace width (mil)',
        'Ohm',
        {{
          xRange: [2, 30],
          yRange,
          annotations: Number.isFinite(targetImpedance) && targetImpedance > 0 ? [{{
            xref: 'paper',
            yref: 'y',
            x: 0.99,
            y: targetImpedance,
            text: 'Target ' + targetImpedance.toFixed(2) + ' ohm',
            showarrow: false,
            font: {{ color: '#dcdcaa', size: 11 }},
            xanchor: 'right',
            yanchor: 'bottom',
          }}] : [],
          shapes: Number.isFinite(targetImpedance) && targetImpedance > 0 ? [{{
            type: 'line',
            xref: 'x',
            yref: 'y',
            x0: 2,
            x1: 30,
            y0: targetImpedance,
            y1: targetImpedance,
            line: {{ color: '#dcdcaa', width: 2, dash: 'dash' }},
          }}] : [],
        }}
      ),
      CFG
    );
  }}

  function buildDifferentialImpedancePlot() {{
    const widths = (profilePlot && profilePlot.widths_mil) || [];
    const gaps = (profilePlot && profilePlot.gaps_mil) || [];
    const matrix = (profilePlot && profilePlot.impedance_matrix_ohm) || [];
    const interpolated = interpolateBilinearGrid(gaps, widths, matrix, 41, 41);
    const selectedWidth = Number((profilePlot && profilePlot.selected_width_mil) || 0);
    const selectedGap = Number((profilePlot && profilePlot.selected_gap_mil) || 0);
    const selectedZdiff = Number(((_result.solved || {{}}).z_diff_ohm) || 0);
    const yRange = differentialWidthAxisRange(profilePlot, targetImpedance, selectedWidth);
    const zValues = matrix.flatMap(row => row || []);
    const zRange = numericAxisRange(zValues, {{ padRatio: 0.05, minSpan: 1e-3 }});
    const traces = [];
    if (Number.isFinite(targetImpedance) && targetImpedance > 0) {{
      traces.unshift({{
        type: 'contour',
        x: interpolated.x,
        y: interpolated.y,
        z: interpolated.z,
        contours: {{
          coloring: 'none',
          showlabels: true,
          start: targetImpedance,
          end: targetImpedance,
          size: 1,
          labelfont: {{ color: '#e8e8e8', size: 11 }},
        }},
        line: {{ color: '#4ec9b0', width: 3 }},
        hovertemplate: 'Gap %{{x:.2f}} mil<br>Width %{{y:.2f}} mil<br>Zdiff %{{z:.2f}} ohm<extra></extra>',
        showscale: false,
        name: 'Target ' + targetImpedance.toFixed(2) + ' ohm',
      }});
    }} else {{
      traces.unshift({{
        type: 'contour',
        x: interpolated.x,
        y: interpolated.y,
        z: interpolated.z,
        zmin: zRange ? zRange[0] : undefined,
        zmax: zRange ? zRange[1] : undefined,
        colorscale: 'Viridis',
        contours: {{
          coloring: 'heatmap',
          showlabels: true,
          labelfont: {{ color: '#e8e8e8', size: 10 }},
        }},
        line: {{ color: '#dfe8f2', width: 1 }},
        colorbar: {{
          title: 'Ohm',
          titlefont: {{ color: '#ccc' }},
          tickfont: {{ color: '#ccc' }},
        }},
        hovertemplate: 'Gap %{{x:.2f}} mil<br>Width %{{y:.2f}} mil<br>Zdiff %{{z:.2f}} ohm<extra></extra>',
        showscale: true,
        name: 'Zdiff',
      }});
    }}
    if (selectedWidth > 0 && selectedGap > 0 && Number.isFinite(selectedZdiff) && selectedZdiff > 0) {{
      traces.push({{
        x: [selectedGap],
        y: [selectedWidth],
        text: [selectedZdiff],
        type: 'scatter',
        mode: 'markers',
        name: 'Selected',
        marker: {{ color: '#d06145', size: 11, line: {{ color: '#ffe8e2', width: 1.5 }} }},
        hovertemplate: 'Selected gap %{{x:.2f}} mil<br>Selected width %{{y:.2f}} mil<br>Zdiff %{{text:.2f}} ohm<extra></extra>',
      }});
    }}
    Plotly.react(
      'plot-impedance',
      traces,
      layout(
        'Trace Width vs Gap @ ' + fmtFreqGhz((profilePlot && profilePlot.target_freq_ghz) || referenceFreqGhz),
        'Gap',
        'Width',
        {{
          xRange: [2, 30],
          yRange,
        }}
      ),
      CFG
    );
  }}

  if (profilePlot && profilePlot.ready) {{
    if (profilePlot.kind === 'differential') {{
      buildDifferentialImpedancePlot();
    }} else {{
      buildSingleEndedImpedancePlot();
    }}
  }} else {{
    const placeholder = isDifferential
      ? placeholderLayout(
          'Trace Width vs Gap @ ' + fmtFreqGhz(referenceFreqGhz),
          'Gap',
          'Width',
          [2, 30],
          [2, 30],
          profilePlot && profilePlot.status === 'failed'
            ? (profilePlot.message || 'Impedance design plot could not be built.')
            : 'Building impedance design plot...'
        )
      : null;
    if (!placeholder) {{
      return;
    }}
    Plotly.react('plot-impedance', [], placeholder, CFG);
  }}
}}

// ── Geometry (cross-section) ──────────────────────────────────────────────────
function buildGeometry() {{
  if (!window.Plotly) {{ setTimeout(buildGeometry, 150); return; }}
  const vis = _result.visualization || {{}};
  const diels  = vis.dielectrics || [];
  const conds  = vis.conductors  || [];

  const shapes = [];

  // Dielectrics as filled rectangles
  diels.forEach(d => {{
    const er = d.epsilon_r||1;
    let fill;
    if (er<=1.01)      fill='rgba(250,250,240,0.6)';
    else if (er<2.5)   fill='rgba(180,200,140,0.7)';
    else if (er<3.4)   fill='rgba(120,190,160,0.7)';
    else if (er<4.2)   fill='rgba(80,160,190,0.7)';
    else               fill='rgba(100,120,200,0.7)';
    shapes.push({{
      type:'rect', layer:'below',
      x0:d.x_min_m*1e3, x1:d.x_max_m*1e3,
      y0:d.y_min_m*1e3, y1:d.y_max_m*1e3,
      fillcolor:fill, line:{{color:'rgba(80,80,80,0.3)',width:0.5}}
    }});
  }});

  // Conductors
  conds.forEach(c => {{
    const fill = c.is_signal ? 'rgba(214,149,67,1)' : 'rgba(126,144,157,1)';
    const line = c.is_signal ? 'rgba(111,74,32,1)'  : 'rgba(83,97,107,1)';
    shapes.push({{
      type:'rect', layer:'above',
      x0:c.x_min_m*1e3, x1:c.x_max_m*1e3,
      y0:c.y_min_m*1e3, y1:c.y_max_m*1e3,
      fillcolor:fill, line:{{color:line,width:1}}
    }});
  }});

  // Conductor labels as scatter text
  const textX=[], textY=[], textT=[], textColor=[];
  conds.forEach(c => {{
    const cx = (c.x_min_m+c.x_max_m)/2*1e3;
    const cy = (c.y_min_m+c.y_max_m)/2*1e3;
    const w  = (c.x_max_m-c.x_min_m)*1e3;
    const h  = (c.y_max_m-c.y_min_m)*1e3;
    if (w<0.02 || h<0.005) return;
    const lbl = c.is_signal ? (c.polarity>0?'SIG+':c.polarity<0?'SIG-':'SIG') : 'GND';
    textX.push(cx); textY.push(cy); textT.push(lbl);
    textColor.push(c.is_signal?'#15212a':'#15212a');
  }});

  const traces = [{{
    type:'scatter', mode:'text',
    x:textX, y:textY, text:textT,
    textfont:{{size:9, color:textColor}},
    hoverinfo:'skip', showlegend:false
  }}];

  const layout = Object.assign({{}}, DARK, {{
    title:{{text:'Cross-Section Geometry',font:{{size:13,color:'#e8e8e8'}}}},
    xaxis: Object.assign({{}},DARK.xaxis,{{
      title:'Width (mm)',
      scaleanchor:'y',
      scaleratio:1,
      autorange:false,
      range: [-0.06, 0.06]
    }}),
    yaxis: Object.assign({{}},DARK.yaxis,{{
      title:'Height (mm)',
      autorange:false,
      range: edgeAxisRange((vis.domain||{{}}).y_min_m*1e3, (vis.domain||{{}}).y_max_m*1e3, {{ padRatio: 0.06, minSpan: 0.02 }})
    }}),
    shapes
  }});
  Plotly.react('plot-geometry', traces, layout, CFG);
}}

// ── Field view ────────────────────────────────────────────────────────────────
function buildFieldControls() {{
  const fv = _result.field_view || {{}};
  const modes = fv.modes || [];
  const sel = document.getElementById('sel-mode');
  sel.innerHTML = '';
  modes.forEach((m,i) => {{
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = m.label || m.mode || ('Mode '+i);
    sel.appendChild(opt);
  }});
  document.getElementById('sel-quantity').onchange = buildField;
  sel.onchange = buildField;
  document.getElementById('chk-contours').onchange = buildField;
}}

function buildField() {{
  if (!window.Plotly) {{ setTimeout(buildField, 150); return; }}
  const fv   = _result.field_view || {{}};
  const modes = fv.modes || [];
  const modeIdx = parseInt(document.getElementById('sel-mode').value)||0;
  const qty   = document.getElementById('sel-quantity').value;
  const showContours = document.getElementById('chk-contours').checked;
  const mode  = modes[modeIdx] || {{}};

  const grid  = qty === 'field_magnitude' ? mode.field_magnitude : mode.potential;
  const range = qty === 'field_magnitude' ? mode.field_range      : mode.potential_range;
  if (!grid || !grid.length) return;

  const vis  = _result.visualization || {{}};
  const xArr = (vis.mesh_x_m||[]).map(v=>v*1e3);
  const yArr = (vis.mesh_y_m||[]).map(v=>v*1e3);

  const isEfield = qty === 'field_magnitude';
  let zData = grid;
  let zmin, zmax, colorscale, colorbarTicks;

  if (isEfield) {{
    // Log-normalise
    const flat = grid.flat().filter(v=>isFinite(v)&&v>0);
    const rawMin = Math.log10(Math.max(Math.min(...flat),1e-3));
    const rawMax = Math.log10(Math.max(...flat,1e-3));
    const span = Math.max(rawMax-rawMin, 0.1);
    zData = grid.map(row => row.map(v => {{
      const lv = Math.log10(Math.max(v,1e-3));
      return Math.max(0, Math.min(1, (lv-rawMin)/span));
    }}));
    zmin=0; zmax=1;
    colorscale = FIRE_CS;
    const n=5;
    colorbarTicks = {{
      tickvals: Array.from({{length:n}},(_,i)=>i/(n-1)),
      ticktext: Array.from({{length:n}},(_,i)=>{{
        const logV = rawMin+i/(n-1)*span;
        return (Math.pow(10,logV)).toExponential(1);
      }})
    }};
  }} else {{
    zmin = range.min; zmax = range.max;
    colorscale = 'RdBu'; colorbarTicks={{}};
  }}

  const traces = [];

  // Heatmap
  traces.push(Object.assign({{
    type:'heatmap', zsmooth:'best',
    x:xArr, y:yArr, z:zData,
    zmin, zmax, colorscale,
    reversescale: !isEfield,
    colorbar: Object.assign({{
      title: isEfield?'V/m':'V',
      titlefont:{{color:'#ccc'}}, tickfont:{{color:'#ccc'}},
      len:0.85
    }}, colorbarTicks),
    hovertemplate:'x:%{{x:.3f}}mm  y:%{{y:.3f}}mm  val:%{{z:.3e}}<extra></extra>'
  }}));

  // Contour iso-lines
  if (showContours) {{
    const n = 14;
    traces.push({{
      type:'contour', x:xArr, y:yArr, z:zData,
      zmin, zmax,
      contours:{{ coloring:'none', showlines:true,
        start:zmin, end:zmax, size:(zmax-zmin)/n }},
      line:{{smoothing:1.3, width:0.9,
        color: isEfield?'rgba(255,255,255,0.38)':'rgba(255,255,255,0.35)'}},
      showscale:false, hoverinfo:'skip'
    }});
  }}

  // Conductor outlines on top
  const vis2   = _result.visualization||{{}};
  const cshapes = (vis2.conductors||[]).map(c=>{{
    const fill = c.is_signal?'rgba(214,149,67,0.9)':'rgba(126,144,157,0.9)';
    const ln   = c.is_signal?'rgba(111,74,32,1)':'rgba(83,97,107,1)';
    return {{type:'rect',layer:'above',
      x0:c.x_min_m*1e3,x1:c.x_max_m*1e3,
      y0:c.y_min_m*1e3,y1:c.y_max_m*1e3,
      fillcolor:fill,line:{{color:ln,width:1}}}};
  }});

  const title = (mode.label||'Mode') + ' | ' + (isEfield?'|E| Field':'Potential');
  const fieldXRange = [-1, 1];
  const fieldYRange = edgeAxisRange(Math.min(...yArr), Math.max(...yArr), {{ padRatio: 0.04, minSpan: 0.02 }});
  const layout = Object.assign({{}},DARK,{{
    title:{{text:title,font:{{size:13,color:'#e8e8e8'}}}},
    xaxis:Object.assign({{}},DARK.xaxis,{{
      title:'Width (mm)',
      scaleanchor:'y',
      scaleratio:1,
      autorange:false,
      range:fieldXRange
    }}),
    yaxis:Object.assign({{}},DARK.yaxis,{{
      title:'Height (mm)',
      autorange:false,
      range:fieldYRange
    }}),
    shapes:cshapes
  }});
  Plotly.react('plot-field', traces, layout, CFG);
}}

async function capturePlotImage(divId, title) {{
  const node = document.getElementById(divId);
  if (!node) {{
    return null;
  }}
  const width = Math.max(node.clientWidth || 0, 900);
  const height = Math.max(node.clientHeight || 0, 420);
  const dataUrl = await Plotly.toImage(node, {{
    format:'png',
    width,
    height,
    scale:2
  }});
  return {{
    title,
    dataUrl,
    wide: divId === 'plot-field'
  }};
}}

async function prepareTabCapture(tabName, rebuildFn) {{
  activateTab(tabName);
  await waitMs(140);
  rebuildFn();
  window.dispatchEvent(new Event('resize'));
  await waitMs(220);
}}

window.preparePrintableReport = async function() {{
  window._reportReady = false;
  window._reportError = '';

  if (!_result) {{
    window._reportError = 'No result is loaded.';
    return false;
  }}
  if (!window.Plotly) {{
    window._reportError = 'Plot engine is not available.';
    return false;
  }}

  const previousTab = currentTab();
  try {{
    const cards = [];

    await prepareTabCapture('plots', buildPlots);
    if (_result && _result.is_differential) {{
      cards.push(await capturePlotImage('plot-impedance', 'Impedance'));
    }}
    cards.push(await capturePlotImage('plot-loss', 'Loss'));

    await prepareTabCapture('geometry', buildGeometry);
    cards.push(await capturePlotImage('plot-geometry', 'Cross-Section Geometry'));

    await prepareTabCapture('field', buildField);
    cards.push(await capturePlotImage('plot-field', 'Field View'));

    const report = document.getElementById('print-report');
    const summaryText = document.getElementById('summary-text').textContent || '';
    const graphicsHtml = cards.filter(Boolean).map(card => `
      <section class="report-card ${{card.wide ? 'wide' : ''}}">
        <h3>${{escapeHtml(card.title)}}</h3>
        <img src="${{card.dataUrl}}" alt="${{escapeHtml(card.title)}}">
      </section>
    `).join('');

    report.innerHTML = `
      <div class="report-header">
        <h1>${{escapeHtml(document.getElementById('hdr-title').textContent)}}</h1>
        <p>${{escapeHtml(document.getElementById('hdr-sub').textContent)}}</p>
      </div>
      <section class="report-summary">
        <h2>Summary</h2>
        <pre>${{escapeHtml(summaryText)}}</pre>
      </section>
      <section class="report-graphics">
        <h2>Graphics</h2>
        <div class="report-grid">${{graphicsHtml}}</div>
      </section>
    `;

    const reportImages = Array.from(report.querySelectorAll('img'));
    await Promise.all(reportImages.map(img => {{
      if (img.complete) {{
        return Promise.resolve();
      }}
      return new Promise(resolve => {{
        img.onload = () => resolve();
        img.onerror = () => resolve();
      }});
    }}));
    await waitMs(80);

    document.body.classList.add('printing');
    window._reportReady = true;
    return true;
  }} catch (error) {{
    window._reportError = String(error && error.message ? error.message : error);
    document.body.classList.remove('printing');
    return false;
  }} finally {{
    activateTab(previousTab);
  }}
}};

window.cleanupPrintableReport = function() {{
  document.body.classList.remove('printing');
  const report = document.getElementById('print-report');
  if (report) {{
    report.innerHTML = '';
  }}
  window._reportReady = false;
  window._reportError = '';
}};

// Redraw field when Plotly finally loads (lazy load)
window.addEventListener('plotly-loaded', () => {{
  hideStateMessage();
  if (_result) {{
    buildPlots(); buildGeometry(); buildField();
  }}
}});

window.addEventListener('plotly-load-failed', () => {{
  showStateMessage('Plot engine could not be loaded.');
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# The QDialog
# ---------------------------------------------------------------------------

class FieldSolverResultsDialog:
    """
    A PySide6 QDialog that wraps a QWebEngineView showing the results page.

    Parameters
    ----------
    parent : QWidget or None
        The parent Tkinter window cannot be used here; pass None or a QWidget.
        In mixed Tk+Qt apps use None and keep Qt's own event loop alive with
        QApplication.instance().exec() or by embedding via QWindow.

    Notes on mixing Tkinter and PySide6
    ------------------------------------
    Qt requires a QApplication before any QWidget.  Create one early, e.g.::

        import sys
        from PySide6.QtWidgets import QApplication
        _qt_app = QApplication.instance() or QApplication(sys.argv)

    Then run Qt's event loop alongside Tk's with a repeating Tk after() call::

        def _pump_qt():
            QApplication.instance().processEvents()
            tk_root.after(10, _pump_qt)
        _pump_qt()

    A full migration to a pure PySide6 main window avoids this complexity
    entirely (all Tkinter widgets become QWidgets).
    """

    _default_loss_axis_floor_db_per_m: float = 1e-6

    def __init__(self, parent=None, on_close=None) -> None:
        if not _PYSIDE6_OK:
            raise ImportError(
                "PySide6 and PySide6-WebEngine are required for the Qt results window.\n"
                f"Install them with:  pip install PySide6 PySide6-WebEngine\n"
                f"Original error: {_PYSIDE6_IMPORT_ERROR}"
            ) from _PYSIDE6_IMPORT_ERROR

        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            import sys
            self._app = QApplication(sys.argv)
        else:
            self._app = QApplication.instance()

        self._on_close = on_close
        self._dialog = QDialog(parent)
        self._dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._dialog.setWindowFlag(Qt.WindowType.Window, True)
        self._dialog.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self._dialog.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self._dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self._dialog.setWindowTitle("Field Solver Results")
        self._dialog.resize(1280, 860)
        self._dialog.setMinimumSize(900, 640)

        layout = QVBoxLayout(self._dialog)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        self._export_report_button = QPushButton("Export Report", self._dialog)
        self._export_report_button.clicked.connect(self._export_report)
        toolbar.addWidget(self._export_report_button)
        layout.addLayout(toolbar)

        self._view = QWebEngineView(self._dialog)
        self._view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Allow local file access (needed for the offline Plotly bundle)
        settings = self._view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)

        layout.addWidget(self._view)

        self._html = _HTML_TEMPLATE.format(plotly_script=_plotly_src_tag())
        self._pending_result: dict[str, Any] | None = None
        self._pending_unit: str = "mm"
        self._root_path: Path | None = None
        self._source_result: dict[str, Any] | None = None
        self._plot_thread: QThread | None = None
        self._plot_worker: _ImpedanceProfilePlotWorker | None = None
        self._plot_result_relay = _PlotResultRelay(self)
        self._plot_request_token = 0
        self._active_plot_request_key: str | None = None
        self._queued_plot_job: dict[str, Any] | None = None
        self._dialog_destroyed = False
        self._loaded = False

        self._view.loadFinished.connect(self._on_load_finished)
        self._view.page().profile().downloadRequested.connect(self._on_download_requested)
        self._dialog.finished.connect(self._on_dialog_finished)
        self._dialog.destroyed.connect(self._on_dialog_destroyed)
        self._view.setHtml(self._html, _LOCAL_BASE_URL)
        logger.info("FieldSolverResultsDialog created")

    # ------------------------------------------------------------------
    def show(self) -> None:
        """Show the dialog (non-blocking)."""
        logger.info("Showing FieldSolverResultsDialog")
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        if self._loaded and self._pending_result is not None:
            self._inject_result(self._pending_result, self._pending_unit)

    def exec(self) -> int:
        """Show the dialog and block until it is closed."""
        return self._dialog.exec()

    def close(self) -> None:
        logger.info("Closing FieldSolverResultsDialog")
        self._dialog.close()

    def parent_widget(self):
        try:
            return self._dialog.parentWidget()
        except RuntimeError:
            return None

    def is_visible(self) -> bool:
        try:
            return self._dialog.isVisible()
        except RuntimeError:
            return False

    def _can_use_dialog(self) -> bool:
        if self._dialog_destroyed or not self._loaded:
            return False
        try:
            return self._dialog.isVisible()
        except RuntimeError:
            return False

    def set_on_close(self, callback) -> None:
        self._on_close = callback

    # ------------------------------------------------------------------
    def load_result(
        self,
        result: dict[str, Any],
        *,
        display_unit: str = "mm",
        root_path: Path | None = None,
    ) -> None:
        """
        Push a solver result dict (as returned by field_solver_bridge.run_solver_request)
        into the web view.  Safe to call before or after the page has loaded.
        """
        logger.info("Loading solver result into report window: %s", _result_summary(result))
        self._source_result = result
        self._root_path = root_path
        self._pending_result = self._prepare_result_for_view(result)
        self._pending_unit = display_unit
        if self._loaded:
            self._inject_result(self._pending_result, display_unit)
        self._start_impedance_profile_plot_job()

    @classmethod
    def _loss_axis_max_db_per_m(cls, result: dict[str, Any]) -> float:
        sweep = result.get("sweep") or {}
        plot_data = sweep.get("plot_data") or {}
        loss_series = plot_data.get("loss") or []
        current_max = 0.0
        for series in loss_series:
            for value in series.get("values") or []:
                if isinstance(value, (int, float)):
                    current_max = max(current_max, float(value))
        return max(cls._default_loss_axis_floor_db_per_m, current_max)

    @classmethod
    def _prepare_result_for_view(cls, result: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(result)
        ui = payload.setdefault("ui", {})
        ui["loss_axis_max_db_per_m"] = cls._loss_axis_max_db_per_m(payload)
        if (
            str(payload.get("tl_type") or "").startswith("diff_")
            and "plot_request_base" in payload
            and "impedance_profile_plot" not in payload
        ):
            payload["impedance_profile_plot"] = {
                "ready": False,
                "status": "pending",
                "kind": "differential",
                "target_freq_ghz": float((payload.get("reference") or {}).get("freq_ghz") or 0.0),
                "message": "",
            }
        return payload

    def _set_impedance_profile_plot_state(self, plot_state: dict[str, Any]) -> None:
        if self._pending_result is not None:
            self._pending_result["impedance_profile_plot"] = copy.deepcopy(plot_state)
        if isinstance(self._source_result, dict):
            self._source_result["impedance_profile_plot"] = copy.deepcopy(plot_state)

    def _start_impedance_profile_plot_job(self) -> None:
        if self._root_path is None or not isinstance(self._source_result, dict):
            return
        if not str(self._source_result.get("tl_type") or "").startswith("diff_"):
            return

        ready_plot = self._source_result.get("impedance_profile_plot")
        if isinstance(ready_plot, dict) and ready_plot.get("ready"):
            if self._loaded:
                self._inject_impedance_profile_plot(ready_plot)
            return

        base_request = self._source_result.get("plot_request_base")
        if not isinstance(base_request, dict):
            return

        target_impedance = self._source_result.get("target_impedance_ohm")
        plot_request = build_impedance_profile_plot_request(
            base_request,
            target_impedance_ohm=float(target_impedance) if isinstance(target_impedance, (int, float)) else None,
        )
        request_key = json.dumps(plot_request, sort_keys=True, ensure_ascii=True)
        self._plot_request_token += 1
        token = self._plot_request_token

        pending_state = {
            "ready": False,
            "status": "pending",
            "kind": "differential" if str(base_request.get("tl_type") or "").startswith("diff_") else "single_ended",
            "target_freq_ghz": float(base_request.get("reference_freq_ghz") or 0.0),
            "message": "",
        }
        self._set_impedance_profile_plot_state(pending_state)
        if self._loaded:
            self._inject_impedance_profile_plot(pending_state)

        if self._plot_thread is not None and self._plot_thread.isRunning():
            logger.info("Queueing impedance profile plot job token=%s", token)
            self._queued_plot_job = {
                "token": token,
                "request_key": request_key,
                "plot_request": plot_request,
            }
            return

        self._queued_plot_job = None
        logger.info("Starting impedance profile plot job token=%s", token)
        self._launch_impedance_profile_plot_job(
            token=token,
            request_key=request_key,
            plot_request=plot_request,
        )

    def _launch_impedance_profile_plot_job(
        self,
        *,
        token: int,
        request_key: str,
        plot_request: dict[str, Any],
    ) -> None:
        if self._root_path is None:
            return

        self._active_plot_request_key = request_key
        logger.info("Launching impedance profile plot thread token=%s", token)
        thread = QThread()
        worker = _ImpedanceProfilePlotWorker(token=token, root_path=self._root_path, plot_request=plot_request)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(
            self._plot_result_relay.deliver_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.failed.connect(
            self._plot_result_relay.deliver_failed,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: self._on_plot_thread_finished(thread))
        thread.finished.connect(thread.deleteLater)
        self._plot_thread = thread
        self._plot_worker = worker
        thread.start()

    def _on_plot_thread_finished(self, thread: QThread) -> None:
        logger.info("Impedance profile plot thread finished")
        if self._plot_thread is thread:
            self._plot_thread = None
            self._plot_worker = None
            self._active_plot_request_key = None
        queued_job = self._queued_plot_job
        self._queued_plot_job = None
        if queued_job is not None:
            QTimer.singleShot(
                0,
                lambda job=queued_job: self._launch_impedance_profile_plot_job(
                    token=int(job["token"]),
                    request_key=str(job["request_key"]),
                    plot_request=copy.deepcopy(job["plot_request"]),
                ),
            )

    def _on_plot_job_finished(self, token: int, plot_state: object) -> None:
        if token != self._plot_request_token or not isinstance(plot_state, dict):
            return
        logger.info("Impedance profile plot job finished token=%s", token)
        plot_state = copy.deepcopy(plot_state)
        plot_state["status"] = "ready"
        plot_state["ready"] = True
        self._set_impedance_profile_plot_state(plot_state)
        if self._can_use_dialog():
            self._inject_impedance_profile_plot(plot_state)

    def _on_plot_job_failed(self, token: int, message: str) -> None:
        if token != self._plot_request_token:
            return
        logger.warning("Impedance profile plot job failed token=%s message=%s", token, message)
        failed_state = {
            "ready": False,
            "status": "failed",
            "kind": "differential" if self._pending_result and self._pending_result.get("is_differential") else "single_ended",
            "target_freq_ghz": float(((self._pending_result or {}).get("reference") or {}).get("freq_ghz") or 0.0),
            "message": message,
        }
        self._set_impedance_profile_plot_state(failed_state)
        if self._can_use_dialog():
            self._inject_impedance_profile_plot(failed_state)

    # ------------------------------------------------------------------
    def _default_save_dir(self, location: QStandardPaths.StandardLocation, fallback: str) -> Path:
        raw_path = QStandardPaths.writableLocation(location)
        if raw_path:
            return Path(raw_path)
        return Path.home() / fallback

    def _run_javascript(self, script: str, timeout_ms: int = 5000) -> object | None:
        if not self._loaded:
            return None

        loop = QEventLoop(self._dialog)
        state: dict[str, object] = {"done": False, "value": None}

        def _finished(value: object) -> None:
            state["done"] = True
            state["value"] = value
            loop.quit()

        self._view.page().runJavaScript(script, _finished)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        if not state["done"]:
            return None
        return state["value"]

    def _wait_for_render(self, wait_ms: int = 450) -> None:
        loop = QEventLoop(self._dialog)
        QTimer.singleShot(wait_ms, loop.quit)
        loop.exec()

    def _activate_result_tab(self, tab_name: str, rebuild_js: str = "") -> None:
        script = f"""
        (function() {{
            activateTab("{tab_name}");
            {rebuild_js}
            window.dispatchEvent(new Event('resize'));
            return true;
        }})();
        """
        self._run_javascript(script, timeout_ms=4000)
        self._wait_for_render(500)

    def _capture_tab_pixmap(self, tab_name: str, title: str, rebuild_js: str = "") -> tuple[str, QPixmap]:
        self._activate_result_tab(tab_name, rebuild_js)
        return title, self._view.grab()

    def _draw_text_block(
        self,
        painter: QPainter,
        rect: QRect,
        text: str,
        font: QFont,
        color: object | None = None,
    ) -> int:
        painter.save()
        painter.setFont(font)
        painter.setPen(Qt.GlobalColor.black if color is None else color)
        fm = QFontMetrics(font)
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap)
        bound = fm.boundingRect(rect, flags, text)
        painter.drawText(rect, flags, text)
        painter.restore()
        return bound.height()

    def _draw_summary_pages(
        self,
        writer: QPdfWriter,
        painter: QPainter,
        content_rect: QRect,
        title: str,
        subtitle: str,
        summary_text: str,
    ) -> None:
        title_font = QFont("Segoe UI", 16, QFont.Weight.Bold)
        subtitle_font = QFont("Segoe UI", 9)
        section_font = QFont("Segoe UI", 11, QFont.Weight.DemiBold)
        summary_font = QFont("Consolas", 8)
        summary_lines = summary_text.splitlines() or [""]

        y = content_rect.top()
        y += self._draw_text_block(painter, QRect(content_rect.left(), y, content_rect.width(), 80), title, title_font)
        y += 8
        y += self._draw_text_block(
            painter,
            QRect(content_rect.left(), y, content_rect.width(), 40),
            subtitle,
            subtitle_font,
        )
        y += 18
        y += self._draw_text_block(
            painter,
            QRect(content_rect.left(), y, content_rect.width(), 40),
            "Summary",
            section_font,
        )
        y += 10

        painter.save()
        painter.setFont(summary_font)
        painter.setPen(Qt.GlobalColor.black)
        fm = QFontMetrics(summary_font)
        line_height = fm.lineSpacing()
        bottom_limit = content_rect.bottom()
        for line in summary_lines:
            if y + line_height > bottom_limit:
                writer.newPage()
                y = content_rect.top()
                painter.setFont(summary_font)
                painter.setPen(Qt.GlobalColor.black)
            painter.drawText(content_rect.left(), y + fm.ascent(), line)
            y += line_height
        painter.restore()

    def _draw_image_page(
        self,
        writer: QPdfWriter,
        painter: QPainter,
        content_rect: QRect,
        title: str,
        pixmap: QPixmap,
    ) -> None:
        if pixmap.isNull():
            return

        writer.newPage()
        heading_font = QFont("Segoe UI", 11, QFont.Weight.DemiBold)
        y = content_rect.top()
        y += self._draw_text_block(
            painter,
            QRect(content_rect.left(), y, content_rect.width(), 40),
            title,
            heading_font,
        )
        y += 12

        available_height = max(120, content_rect.bottom() - y)
        scaled = pixmap.scaled(
            QSize(content_rect.width(), available_height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = content_rect.left() + max(0, (content_rect.width() - scaled.width()) // 2)
        target = QRect(x, y, scaled.width(), scaled.height())
        painter.drawPixmap(target, scaled)

    def _export_report(self) -> None:
        if self._pending_result is None:
            QMessageBox.warning(self._dialog, "No result", "Run a field solver calculation first.")
            return

        default_dir = self._default_save_dir(QStandardPaths.StandardLocation.DocumentsLocation, "Documents")
        copper = (self._pending_result.get("selected_copper") or {}).get("label", "field_solver")
        safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(copper)).strip("_")
        if not safe_label:
            safe_label = "field_solver"
        default_path = default_dir / f"{safe_label}_field_solver_report.pdf"
        file_path, _filter = QFileDialog.getSaveFileName(
            self._dialog,
            "Export Field Solver Report",
            str(default_path),
            "PDF Files (*.pdf)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".pdf"):
            file_path += ".pdf"

        title = str(self._run_javascript("document.getElementById('hdr-title').textContent || '';") or "Field Solver Results")
        subtitle = str(self._run_javascript("document.getElementById('hdr-sub').textContent || '';") or "")
        summary_text = str(self._run_javascript("document.getElementById('summary-text').textContent || '';") or "")
        current_tab = str(self._run_javascript("currentTab();") or "summary")

        try:
            captures = [
                self._capture_tab_pixmap("plots", "Plots", "buildPlots();"),
                self._capture_tab_pixmap("geometry", "Geometry", "buildGeometry();"),
                self._capture_tab_pixmap("field", "Field View", "buildField();"),
            ]

            writer = QPdfWriter(file_path)
            writer.setResolution(150)
            writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)

            painter = QPainter(writer)
            if not painter.isActive():
                raise RuntimeError("The PDF writer could not be started.")

            content_rect = writer.pageLayout().paintRectPixels(writer.resolution())
            self._draw_summary_pages(writer, painter, content_rect, title, subtitle, summary_text)
            for capture_title, pixmap in captures:
                self._draw_image_page(writer, painter, content_rect, capture_title, pixmap)
            painter.end()
        except Exception as exc:
            QMessageBox.warning(self._dialog, "Export failed", str(exc))
            return
        finally:
            self._activate_result_tab(current_tab)

        QMessageBox.information(self._dialog, "Report exported", f"Saved report to:\n{file_path}")

    def _on_download_requested(self, download) -> None:
        suggested_name = download.suggestedFileName() or "field_solver_plot.png"
        default_dir = self._default_save_dir(QStandardPaths.StandardLocation.DownloadLocation, "Downloads")
        default_path = default_dir / suggested_name
        file_path, _filter = QFileDialog.getSaveFileName(
            self._dialog,
            "Save Plot Image",
            str(default_path),
            "PNG Files (*.png);;All Files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not file_path:
            download.cancel()
            return

        target = Path(file_path)
        download.setDownloadDirectory(str(target.parent))
        download.setDownloadFileName(target.name)
        download.accept()

    # ------------------------------------------------------------------
    def _on_load_finished(self, ok: bool) -> None:
        self._loaded = True
        logger.info("FieldSolverResultsDialog HTML load finished ok=%s", ok)
        if self._pending_result is not None:
            self._inject_result(self._pending_result, self._pending_unit)

    def _on_dialog_finished(self, _result: int) -> None:
        logger.info("FieldSolverResultsDialog finished")
        self._plot_request_token += 1
        self._active_plot_request_key = None
        self._queued_plot_job = None
        self._loaded = False
        if callable(self._on_close):
            QTimer.singleShot(0, self._on_close)

    def _on_dialog_destroyed(self, *_args) -> None:
        self._dialog_destroyed = True
        self._loaded = False

    def _inject_impedance_profile_plot(self, plot_state: dict[str, Any]) -> None:
        if not self._can_use_dialog():
            return
        safe_json = json.dumps(plot_state, ensure_ascii=True)
        safe_json = safe_json.replace("\\", "\\\\").replace("`", "\\`")
        js = f"""
        (function() {{
            try {{
                window.updateImpedanceProfilePlot(`{safe_json}`);
            }} catch (_error) {{}}
        }})();
        """
        self._view.page().runJavaScript(js)

    def _inject_result(self, result: dict[str, Any], unit: str) -> None:
        if not self._can_use_dialog():
            return
        safe_json = json.dumps(result, ensure_ascii=True)
        # Escape backticks and backslashes to be safe inside a JS template literal
        safe_json = safe_json.replace("\\", "\\\\").replace("`", "\\`")
        safe_unit = unit.replace('"', '\\"')
        js = f"""
        (function() {{
            try {{
                const ids = ['plot-impedance', 'plot-loss', 'plot-geometry', 'plot-field'];
                ids.forEach((id) => {{
                    const element = document.getElementById(id);
                    if (!element) {{
                        return;
                    }}
                    if (window.Plotly) {{
                        try {{ Plotly.purge(element); }} catch (_error) {{}}
                    }}
                    element.innerHTML = '';
                }});
                const summary = document.getElementById('summary-text');
                if (summary) {{
                    summary.textContent = '';
                }}
                window._reportReady = false;
                window._reportError = '';
            }} catch (_error) {{}}
            window.loadResult(`{safe_json}`, "{safe_unit}");
        }})();
        """
        self._view.page().runJavaScript(js)


# ---------------------------------------------------------------------------
# Compatibility shim so existing app.py imports keep working during the
# transition.  Swap FieldSolverResultsWindow → FieldSolverResultsDialog.
# ---------------------------------------------------------------------------
FieldSolverResultsWindow = FieldSolverResultsDialog
