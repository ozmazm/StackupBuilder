import util from "node:util";
import readline from "node:readline";

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function logspace(startHz, stopHz, points) {
  if (points <= 1) {
    return [startHz];
  }
  const start = Math.log10(startHz);
  const stop = Math.log10(stopHz);
  const result = [];
  for (let index = 0; index < points; index += 1) {
    const t = start + ((stop - start) * index) / (points - 1);
    result.push(Math.pow(10, t));
  }
  return result;
}

function withReferenceFrequency(frequencies, referenceFreqHz) {
  const values = Array.isArray(frequencies) ? [...frequencies] : [];
  if (!Number.isFinite(referenceFreqHz) || referenceFreqHz <= 0) {
    return values;
  }
  const tolerance = Math.max(referenceFreqHz * 1e-9, 1e-6);
  const exists = values.some((value) => Math.abs(value - referenceFreqHz) <= tolerance);
  if (!exists) {
    values.push(referenceFreqHz);
    values.sort((left, right) => left - right);
  }
  return values;
}

function summarizeResult(result, freqHz) {
  const mode = result.modes[0];
  const summary = {
    freq_hz: freqHz,
    freq_ghz: freqHz / 1e9,
    alpha_c_db_per_m: mode.alpha_c,
    alpha_d_db_per_m: mode.alpha_d,
    alpha_total_db_per_m: mode.alpha_total,
  };

  if (result.modes.length > 1) {
    const odd = result.modes.find((item) => item.mode === "odd");
    const even = result.modes.find((item) => item.mode === "even");
    summary.z_diff_ohm = result.Z_diff;
    summary.z_common_ohm = result.Z_common;
    summary.z_odd_ohm = odd.Z0;
    summary.z_even_ohm = even.Z0;
    summary.eps_eff_odd = odd.eps_eff;
    summary.eps_eff_even = even.eps_eff;
    summary.rlgc_odd = odd.RLGC;
    summary.rlgc_even = even.RLGC;
  } else {
    summary.z0_ohm = mode.Z0;
    summary.eps_eff = mode.eps_eff;
    summary.rlgc = mode.RLGC;
  }

  return summary;
}

function serialize2DArray(matrix) {
  return (matrix ?? []).map((row) => Array.from(row ?? []));
}

function buildFieldMagnitude(exMatrix, eyMatrix) {
  const magnitude = [];
  for (let rowIndex = 0; rowIndex < exMatrix.length; rowIndex += 1) {
    const exRow = exMatrix[rowIndex] ?? [];
    const eyRow = eyMatrix[rowIndex] ?? [];
    const outRow = new Array(exRow.length);
    for (let columnIndex = 0; columnIndex < exRow.length; columnIndex += 1) {
      const ex = exRow[columnIndex] ?? 0;
      const ey = eyRow[columnIndex] ?? 0;
      outRow[columnIndex] = Math.hypot(ex, ey);
    }
    magnitude.push(outRow);
  }
  return magnitude;
}

function finiteRange(matrix) {
  let min = Infinity;
  let max = -Infinity;
  for (const row of matrix ?? []) {
    for (const value of row ?? []) {
      if (Number.isFinite(value)) {
        if (value < min) {
          min = value;
        }
        if (value > max) {
          max = value;
        }
      }
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return { min: 0, max: 0 };
  }
  return { min, max };
}

function serializeGeometryRegion(region) {
  return {
    x_min_m: region.x_min,
    x_max_m: region.x_max,
    y_min_m: region.y_min,
    y_max_m: region.y_max,
    width_m: region.x_max - region.x_min,
    height_m: region.y_max - region.y_min,
  };
}

function buildVisualizationData(solver) {
  const xValues = Array.from(solver.x ?? []);
  const yValues = Array.from(solver.y ?? []);
  const xMin = xValues.length ? Math.min(...xValues) : 0;
  const xMax = xValues.length ? Math.max(...xValues) : 0;
  const yMin = yValues.length ? Math.min(...yValues) : 0;
  const yMax = yValues.length ? Math.max(...yValues) : 0;

  return {
    domain: {
      x_min_m: xMin,
      x_max_m: xMax,
      y_min_m: yMin,
      y_max_m: yMax,
      width_m: xMax - xMin,
      height_m: yMax - yMin,
    },
    mesh_x_m: xValues,
    mesh_y_m: yValues,
    conductors: (solver.conductors ?? []).map((conductor) => ({
      ...serializeGeometryRegion(conductor),
      is_signal: Boolean(conductor.is_signal),
      polarity: conductor.polarity ?? 0,
    })),
    dielectrics: (solver.dielectrics ?? []).map((dielectric) => ({
      ...serializeGeometryRegion(dielectric),
      epsilon_r: dielectric.epsilon_r,
      tan_delta: dielectric.tan_delta,
    })),
  };
}

function buildFieldViewData(solver, referenceResult) {
  const conductorMask = serialize2DArray(solver.conductor_mask);
  const signalMask = serialize2DArray(solver.signal_mask);
  const groundMask = serialize2DArray(solver.ground_mask);

  return {
    conductor_mask: conductorMask,
    signal_mask: signalMask,
    ground_mask: groundMask,
    modes: referenceResult.modes.map((modeResult) => {
      const potential = serialize2DArray(modeResult.V);
      const fieldMagnitude = buildFieldMagnitude(modeResult.Ex ?? [], modeResult.Ey ?? []);
      const potentialRange = finiteRange(potential);
      const fieldRange = finiteRange(fieldMagnitude);
      return {
        mode: modeResult.mode,
        label: modeResult.mode === "single" ? "Single-ended" : `${modeResult.mode[0].toUpperCase()}${modeResult.mode.slice(1)} mode`,
        potential,
        field_magnitude: fieldMagnitude,
        potential_range: potentialRange,
        field_range: fieldRange,
      };
    }),
  };
}

function buildPlotData(results, isDifferential) {
  const plotData = {
    frequencies_ghz: results.map((item) => item.freq / 1e9),
    impedance: [],
    loss: [],
    permittivity: [],
  };

  if (isDifferential) {
    const oddValues = [];
    const evenValues = [];
    const diffValues = [];
    const commonValues = [];
    const oddLoss = [];
    const oddCondLoss = [];
    const oddDielLoss = [];
    const oddPermittivity = [];
    const evenPermittivity = [];

    for (const item of results) {
      const odd = item.result.modes.find((mode) => mode.mode === "odd");
      const even = item.result.modes.find((mode) => mode.mode === "even");
      oddValues.push(odd?.Z0 ?? null);
      evenValues.push(even?.Z0 ?? null);
      diffValues.push(item.result.Z_diff ?? null);
      commonValues.push(item.result.Z_common ?? null);
      oddLoss.push(odd?.alpha_total ?? null);
      oddCondLoss.push(odd?.alpha_c ?? null);
      oddDielLoss.push(odd?.alpha_d ?? null);
      oddPermittivity.push(odd?.eps_eff ?? null);
      evenPermittivity.push(even?.eps_eff ?? null);
    }

    plotData.impedance.push(
      { label: "Zdiff", values: diffValues },
      { label: "Zodd", values: oddValues },
      { label: "Zeven", values: evenValues },
      { label: "Zcommon", values: commonValues },
    );
    plotData.loss.push(
      { label: "Differential total", values: oddLoss },
      { label: "Differential conductor", values: oddCondLoss },
      { label: "Differential dielectric", values: oddDielLoss },
    );
    plotData.permittivity.push(
      { label: "Odd eps_eff", values: oddPermittivity },
      { label: "Even eps_eff", values: evenPermittivity },
    );
  } else {
    const z0Values = [];
    const totalLoss = [];
    const conductorLoss = [];
    const dielectricLoss = [];
    const epsEffValues = [];

    for (const item of results) {
      const mode = item.result.modes[0];
      z0Values.push(mode?.Z0 ?? null);
      totalLoss.push(mode?.alpha_total ?? null);
      conductorLoss.push(mode?.alpha_c ?? null);
      dielectricLoss.push(mode?.alpha_d ?? null);
      epsEffValues.push(mode?.eps_eff ?? null);
    }

    plotData.impedance.push({ label: "Z0", values: z0Values });
    plotData.loss.push(
      { label: "Total loss", values: totalLoss },
      { label: "Conductor loss", values: conductorLoss },
      { label: "Dielectric loss", values: dielectricLoss },
    );
    plotData.permittivity.push({ label: "eps_eff", values: epsEffValues });
  }

  return plotData;
}

function mergeExactSweepValues(targetResults, exactResults) {
  if (!Array.isArray(targetResults) || !Array.isArray(exactResults)) {
    return;
  }

  const count = Math.min(targetResults.length, exactResults.length);
  for (let index = 0; index < count; index += 1) {
    const targetModes = targetResults[index]?.result?.modes ?? [];
    const targetResult = targetResults[index]?.result ?? {};
    const exactResult = exactResults[index]?.result ?? {};
    const exactModes = exactResult.modes ?? [];
    for (const targetMode of targetModes) {
      const exactMode = exactModes.find((mode) => mode.mode === targetMode.mode);
      if (!exactMode) {
        continue;
      }
      targetMode.Z0 = exactMode.Z0;
      targetMode.eps_eff = exactMode.eps_eff;
      targetMode.alpha_c = exactMode.alpha_c;
      targetMode.alpha_d = exactMode.alpha_d;
      targetMode.alpha_total = exactMode.alpha_total;
    }
    if (Number.isFinite(exactResult.Z_diff)) {
      targetResult.Z_diff = exactResult.Z_diff;
    }
    if (Number.isFinite(exactResult.Z_common)) {
      targetResult.Z_common = exactResult.Z_common;
    }
  }
}

function milToMeters(valueMil) {
  return Number(valueMil) * 25.4e-6;
}

function extractImpedance(result, isDifferential) {
  if (isDifferential) {
    return Number.isFinite(result?.Z_diff) ? result.Z_diff : null;
  }
  const mode = result?.modes?.[0];
  return Number.isFinite(mode?.Z0) ? mode.Z0 : null;
}

async function runImpedanceProfileSweep(request) {
  const { MicrostripSolver } = await import("../js_2d_fields-master/src/microstrip.js");
  const baseRequest = request.base_request ?? {};
  const baseSolverOptions = baseRequest.solver_options ?? {};
  const baseAdaptiveOptions = baseRequest.adaptive_options ?? {};
  const isDifferential = String(baseRequest.tl_type ?? "").startsWith("diff_");
  const targetImpedanceOhm = Number(request.target_impedance_ohm ?? 0);
  const targetFreqGhz = Number(request.target_freq_ghz ?? baseRequest.reference_freq_ghz ?? 0);
  if (!Number.isFinite(targetFreqGhz) || targetFreqGhz <= 0) {
    throw new Error("A positive target_freq_ghz is required for impedance profile plots.");
  }

  const fastAdaptiveOptions = {
    ...baseAdaptiveOptions,
    max_iters: 1,
    energy_tol: 0.05,
    param_tol: 0.5,
    max_nodes: 12000,
    min_converged_passes: 1,
    ...(request.fast_adaptive_options ?? {}),
  };
  const initialGrid = Number(request.initial_grid ?? 14);
  const selectedWidthMil = (Number(baseRequest.geometry?.trace_width_mm ?? 0) / 0.0254) || 0;
  const selectedGapMil = (Number(baseRequest.geometry?.trace_spacing_mm ?? 0) / 0.0254) || 0;
  const targetFreqHz = targetFreqGhz * 1e9;

  async function solvePoint(widthMil, gapMil = null) {
    const solverOptions = {
      ...baseSolverOptions,
      freq: targetFreqHz,
      trace_width: milToMeters(widthMil),
      nx: initialGrid,
      ny: initialGrid,
    };
    if (isDifferential && gapMil !== null) {
      solverOptions.trace_spacing = milToMeters(gapMil);
    }
    const solver = new MicrostripSolver(solverOptions);
    solver.use_causal_materials = false;
    const cached = await solver.solve_adaptive(fastAdaptiveOptions);
    const result = await solver.computeAtFrequency(targetFreqHz, cached);
    return extractImpedance(result, isDifferential);
  }

  if (isDifferential) {
    const widthsMil = Array.isArray(request.widths_mil) ? request.widths_mil.map(Number) : [];
    const gapsMil = Array.isArray(request.gaps_mil) ? request.gaps_mil.map(Number) : [];
    const impedanceMatrixOhm = [];
    const usedWidthsMil = [];
    const minRowsBeforeStop = Number(request.min_width_rows_before_stop ?? 3);
    let consecutiveBelowTargetRows = 0;

    for (const widthMil of widthsMil) {
      const row = [];
      for (const gapMil of gapsMil) {
        try {
          row.push(await solvePoint(widthMil, gapMil));
        } catch (_error) {
          row.push(null);
        }
      }
      usedWidthsMil.push(widthMil);
      impedanceMatrixOhm.push(row);

      if (Number.isFinite(targetImpedanceOhm) && targetImpedanceOhm > 0 && usedWidthsMil.length >= minRowsBeforeStop) {
        const finiteRow = row.filter((value) => Number.isFinite(value));
        if (finiteRow.length) {
          const rowMax = Math.max(...finiteRow);
          if (rowMax < targetImpedanceOhm) {
            consecutiveBelowTargetRows += 1;
          } else {
            consecutiveBelowTargetRows = 0;
          }
          if (consecutiveBelowTargetRows >= 2) {
            break;
          }
        } else {
          consecutiveBelowTargetRows = 0;
        }
      }
    }

    const effectiveMaxWidthMil = usedWidthsMil.length
      ? usedWidthsMil[usedWidthsMil.length - 1]
      : (widthsMil[widthsMil.length - 1] ?? 0);
    return {
      kind: "differential",
      ready: true,
      target_freq_ghz: targetFreqGhz,
      widths_mil: usedWidthsMil,
      gaps_mil: gapsMil,
      impedance_matrix_ohm: impedanceMatrixOhm,
      selected_width_mil: selectedWidthMil,
      selected_gap_mil: selectedGapMil,
      effective_max_width_mil: effectiveMaxWidthMil,
    };
  }

  const widthsMil = Array.isArray(request.widths_mil) ? request.widths_mil.map(Number) : [];
  const impedancesOhm = [];
  for (const widthMil of widthsMil) {
    try {
      impedancesOhm.push(await solvePoint(widthMil));
    } catch (_error) {
      impedancesOhm.push(null);
    }
  }
  return {
    kind: "single_ended",
    ready: true,
    target_freq_ghz: targetFreqGhz,
    widths_mil: widthsMil,
    impedances_ohm: impedancesOhm,
    selected_width_mil: selectedWidthMil,
    selected_gap_mil: 0,
  };
}

async function runSweep(solver, sweepClass, baseResult, sweepConfig, referenceFreqHz) {
  const frequencies = withReferenceFrequency(
    logspace(sweepConfig.start_ghz * 1e9, sweepConfig.stop_ghz * 1e9, sweepConfig.points),
    referenceFreqHz,
  );
  let sampleCount = frequencies.length;
  let results = null;

  if (sweepConfig.interpolating) {
    const sweep = new sweepClass(solver, baseResult, {
      tolerance: sweepConfig.tolerance,
      initialPoints: sweepConfig.initial_points,
      maxPoints: sweepConfig.max_points,
      maxIterations: sweepConfig.max_iterations,
    });
    sampleCount = await sweep.run(frequencies[0], frequencies[frequencies.length - 1]);
    results = sweep.buildResults(frequencies);

    const exactResults = [];
    for (const frequency of frequencies) {
      const result = await solver.computeAtFrequency(frequency, baseResult);
      exactResults.push({ freq: frequency, result });
    }
    mergeExactSweepValues(results, exactResults);
  } else {
    results = [];
    for (const frequency of frequencies) {
      const result = await solver.computeAtFrequency(frequency, baseResult);
      results.push({ freq: frequency, result });
    }
  }

  return {
    frequencies_ghz: frequencies.map((frequency) => frequency / 1e9),
    exact_samples: sampleCount,
    start: summarizeResult(results[0].result, results[0].freq),
    stop: summarizeResult(results[results.length - 1].result, results[results.length - 1].freq),
    plot_data: buildPlotData(results, solver.is_differential),
  };
}

function configureLogging() {
  const writeProgress = (...args) => {
    process.stderr.write(`${util.format(...args)}\n`);
  };
  console.log = writeProgress;
  console.info = writeProgress;
  console.warn = writeProgress;
}

function formatError(error) {
  return error?.stack || error?.message || String(error);
}

async function handleRequest(request) {
  if (request.mode === "impedance_profile_plot") {
    return await runImpedanceProfileSweep(request);
  }

  const { MicrostripSolver } = await import("../js_2d_fields-master/src/microstrip.js");
  const solver = new MicrostripSolver(request.solver_options);
  solver.use_causal_materials = false;
  const responseOptions = request.response_options ?? {};
  const includeGeometry = responseOptions.include_geometry !== false;
  const includeMesh = responseOptions.include_mesh !== false;
  const includeVisualization = responseOptions.include_visualization !== false;
  const includeFieldView = responseOptions.include_field_view !== false;

  const baseResult = await solver.solve_adaptive(request.adaptive_options ?? {});
  const referenceFreqHz = request.reference_freq_ghz * 1e9;
  const referenceResult = await solver.computeAtFrequency(referenceFreqHz, baseResult);
  const response = {
    tl_type: request.tl_type,
    is_differential: request.tl_type.startsWith("diff_"),
    selected_copper: request.selected_copper,
    reference: {
      freq_hz: referenceFreqHz,
      freq_ghz: request.reference_freq_ghz,
    },
    solved: summarizeResult(referenceResult, referenceFreqHz),
  };
  if (includeGeometry) {
    response.geometry = request.geometry;
  }
  if (includeMesh) {
    response.mesh = {
      nx: solver.x?.length ?? 0,
      ny: solver.y?.length ?? 0,
    };
  }
  if (includeVisualization) {
    response.visualization = buildVisualizationData(solver);
  }
  if (includeFieldView) {
    response.field_view = buildFieldViewData(solver, referenceResult);
  }

  if (request.sweep?.enabled) {
    const { InterpolatingSweep } = await import("../js_2d_fields-master/src/interpolating_sweep.js");
    response.sweep = await runSweep(solver, InterpolatingSweep, baseResult, request.sweep, referenceFreqHz);
  }

  return response;
}

async function serveLoop() {
  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });

  for await (const line of rl) {
    const payload = line.trim();
    if (!payload) {
      continue;
    }

    let request;
    try {
      request = JSON.parse(payload);
    } catch (error) {
      process.stdout.write(`${JSON.stringify({ ok: false, error: `Invalid JSON request: ${formatError(error)}` })}\n`);
      continue;
    }

    try {
      const response = await handleRequest(request);
      process.stdout.write(`${JSON.stringify({ ok: true, result: response })}\n`);
    } catch (error) {
      process.stdout.write(`${JSON.stringify({ ok: false, error: formatError(error) })}\n`);
    }
  }
}

async function main() {
  configureLogging();

  if (process.argv.includes("--serve")) {
    await serveLoop();
    return;
  }

  const raw = await readStdin();
  const request = JSON.parse(raw);
  const response = await handleRequest(request);
  process.stdout.write(JSON.stringify(response));
}

main().catch((error) => {
  process.stderr.write(formatError(error));
  process.exit(1);
});
